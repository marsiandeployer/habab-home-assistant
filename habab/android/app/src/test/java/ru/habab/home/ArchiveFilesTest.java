package ru.habab.home;

import java.io.*;
import java.nio.file.*;
import java.security.*;
import java.util.Arrays;
import org.junit.*;
import static org.junit.Assert.*;
import org.apache.commons.compress.archivers.tar.*;
import org.apache.commons.compress.compressors.gzip.*;

public class ArchiveFilesTest {
    private Path root;
    @Before public void setup() throws IOException { root = Files.createTempDirectory("habab-extract-test"); }
    @After public void cleanup() throws IOException { ArchiveFiles.delete(root.toFile()); }

    @Test public void rejectsParentTraversal() throws Exception {
        assertThrows(IOException.class, () -> ArchiveFiles.destination(root, "../../outside"));
        assertThrows(IOException.class, () -> ArchiveFiles.destination(root, "/etc/passwd"));
        assertThrows(IOException.class, () -> ArchiveFiles.destination(root, "usr/../../outside"));
        assertEquals(root.resolve("usr/bin/python3"), ArchiveFiles.destination(root, "usr/bin/python3"));
    }

    @Test public void neverFollowsExistingGuestSymlink() throws Exception {
        Files.createSymbolicLink(root.resolve("escape"), root.getParent());
        assertThrows(IOException.class, () -> ArchiveFiles.destination(root, "escape/overwritten"));
        assertThrows(IOException.class, () -> ArchiveFiles.destination(root, "escape"));
    }

    @Test public void deletesModeZeroPlaceholdersWithoutFollowingLinks() throws Exception {
        Path runtime = Files.createDirectory(root.resolve("runtime-previous"));
        Path placeholder = Files.createDirectory(runtime.resolve("system"));
        Files.write(placeholder.resolve("owned"), new byte[]{1});
        Path outside = Files.createDirectory(root.resolve("outside"));
        Path preserved = Files.write(outside.resolve("preserved"), new byte[]{2});
        Files.createSymbolicLink(runtime.resolve("android-link"), outside);
        Files.setPosixFilePermissions(placeholder, java.util.Collections.emptySet());
        ArchiveFiles.delete(runtime.toFile());
        assertFalse(Files.exists(runtime, LinkOption.NOFOLLOW_LINKS));
        assertArrayEquals(new byte[]{2}, Files.readAllBytes(preserved));
    }

    @Test public void rejectsOversizeBeforeWritingFile() throws Exception {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        try (TarArchiveOutputStream tar = new TarArchiveOutputStream(new GzipCompressorOutputStream(output))) {
            TarArchiveEntry entry = new TarArchiveEntry("too-large"); entry.setSize(5);
            tar.putArchiveEntry(entry); tar.write(new byte[5]); tar.closeArchiveEntry();
        }
        assertThrows(IOException.class, () -> ArchiveFiles.extract(new ByteArrayInputStream(output.toByteArray()), root.toFile(), 4));
        assertFalse(Files.exists(root.resolve("too-large")));
    }

    @Test public void rejectsTraversalInActualTar() throws Exception {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        try (TarArchiveOutputStream tar = new TarArchiveOutputStream(new GzipCompressorOutputStream(output))) {
            TarArchiveEntry entry = new TarArchiveEntry("../../escape"); entry.setSize(0);
            tar.putArchiveEntry(entry); tar.closeArchiveEntry();
        }
        assertThrows(IOException.class, () -> ArchiveFiles.extract(new ByteArrayInputStream(output.toByteArray()), root.toFile(), 10));
    }

    @Test public void roundtripKeepsFilesModesAndSymlinks() throws Exception {
        Path source = root.resolve("source"); Files.createDirectories(source.resolve("sub"));
        Path program = source.resolve("sub/tool"); Files.write(program, "hello".getBytes());
        Files.setPosixFilePermissions(program, java.nio.file.attribute.PosixFilePermissions.fromString("rwx------"));
        Files.createSymbolicLink(source.resolve("alias"), Paths.get("sub/tool"));
        ByteArrayOutputStream archive = new ByteArrayOutputStream(); ArchiveFiles.pack(source.toFile(), archive);
        Path destination = root.resolve("restored");
        ArchiveFiles.extract(new ByteArrayInputStream(archive.toByteArray()), destination.toFile(), 1024);
        assertEquals("hello", new String(Files.readAllBytes(destination.resolve("sub/tool"))));
        assertEquals(Paths.get("sub/tool"), Files.readSymbolicLink(destination.resolve("alias")));
        assertEquals(Files.getPosixFilePermissions(program), Files.getPosixFilePermissions(destination.resolve("sub/tool")));
    }

    @Test public void actualRuntimeArchiveUsesTheSameInstaller() throws Exception {
        String archive = System.getenv("HABAB_RUNTIME_ARCHIVE");
        Assume.assumeTrue(archive != null && !archive.isBlank());
        assertTrue("Full-runtime extraction needs at least 4 GiB free", Files.getFileStore(root).getUsableSpace() >= 4L * 1024 * 1024 * 1024);
        try (InputStream input = new FileInputStream(archive)) { ArchiveFiles.extract(input, root.toFile(), 8L * 1024 * 1024 * 1024, true); }
        assertTrue(Files.exists(root.resolve("usr/local/bin/python3")));
        assertTrue(Files.isExecutable(root.resolve("opt/habab-opencode/opencode")));
        assertTrue(Files.exists(root.resolve("usr/bin/node")));
        assertTrue(Files.exists(root.resolve("usr/src/homeassistant/homeassistant/components/habab_home/frontend/setup-chat.js")));
    }

    @Test public void hardlinksBecomeIndependentCopiesWithModesAndSizeAccounting() throws Exception {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        try (TarArchiveOutputStream tar = new TarArchiveOutputStream(new GzipCompressorOutputStream(output))) {
            TarArchiveEntry original = new TarArchiveEntry("tool"); original.setSize(3); original.setMode(0700);
            tar.putArchiveEntry(original); tar.write("run".getBytes()); tar.closeArchiveEntry();
            TarArchiveEntry alias = new TarArchiveEntry("alias", TarConstants.LF_LINK); alias.setLinkName("tool");
            tar.putArchiveEntry(alias); tar.closeArchiveEntry();
        }
        Path copied = root.resolve("copied");
        ArchiveFiles.extract(new ByteArrayInputStream(output.toByteArray()), copied.toFile(), 6);
        assertEquals("run", new String(Files.readAllBytes(copied.resolve("alias"))));
        assertFalse(Files.isSameFile(copied.resolve("tool"), copied.resolve("alias")));
        assertEquals(Files.getPosixFilePermissions(copied.resolve("tool")), Files.getPosixFilePermissions(copied.resolve("alias")));
        Path denied = root.resolve("denied");
        assertThrows(IOException.class, () -> ArchiveFiles.extract(new ByteArrayInputStream(output.toByteArray()), denied.toFile(), 5));
        assertFalse(Files.exists(denied.resolve("alias")));
    }

    @Test public void signedReleaseRejectsWrongKeyAndTampering() throws Exception {
        KeyPairGenerator generator = KeyPairGenerator.getInstance("RSA"); generator.initialize(2048);
        KeyPair key = generator.generateKeyPair(), other = generator.generateKeyPair();
        byte[] manifest = "{\"architecture\":\"aarch64\"}".getBytes(java.nio.charset.StandardCharsets.UTF_8);
        Signature signer = Signature.getInstance("SHA256withRSA"); signer.initSign(key.getPrivate()); signer.update(manifest);
        byte[] signature = signer.sign();
        ReleaseVerifier.verify(manifest, signature, key.getPublic().getEncoded());
        assertThrows(GeneralSecurityException.class, () -> ReleaseVerifier.verify(manifest, signature, other.getPublic().getEncoded()));
        byte[] modified = Arrays.copyOf(manifest, manifest.length); modified[2] ^= 1;
        assertThrows(GeneralSecurityException.class, () -> ReleaseVerifier.verify(modified, signature, key.getPublic().getEncoded()));
    }
}
