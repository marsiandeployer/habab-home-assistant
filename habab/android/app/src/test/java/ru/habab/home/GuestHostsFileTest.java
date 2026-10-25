package ru.habab.home;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Set;
import java.util.stream.Stream;
import org.junit.Rule;
import org.junit.Test;
import org.junit.Assume;
import org.junit.rules.TemporaryFolder;

public final class GuestHostsFileTest {
    private static final byte[] EXPECTED =
            "127.0.0.1 localhost\n::1 localhost ip6-localhost ip6-loopback\n".getBytes(StandardCharsets.US_ASCII);

    @Rule public final TemporaryFolder temporaryFolder = new TemporaryFolder();

    @Test public void seedsMissingHostsFile() throws Exception {
        Path etc = Files.createDirectory(temporaryFolder.getRoot().toPath().resolve("etc"));

        assertTrue(GuestHostsFile.seedIfEmpty(etc));
        assertArrayEquals(EXPECTED, Files.readAllBytes(etc.resolve("hosts")));
    }

    @Test public void seedsEmptyRegularFile() throws Exception {
        Path etc = Files.createDirectory(temporaryFolder.getRoot().toPath().resolve("etc"));
        Path hosts = Files.createFile(etc.resolve("hosts"));

        assertTrue(GuestHostsFile.seedIfEmpty(etc));
        assertArrayEquals(EXPECTED, Files.readAllBytes(hosts));
    }

    @Test public void preservesNonemptyCustomHostsByteForByte() throws Exception {
        Path etc = Files.createDirectory(temporaryFolder.getRoot().toPath().resolve("etc"));
        byte[] custom = new byte[]{'#', ' ', (byte) 0xc3, (byte) 0xa9, '\r', '\n'};
        Path hosts = Files.write(etc.resolve("hosts"), custom);

        assertFalse(GuestHostsFile.seedIfEmpty(etc));
        assertArrayEquals(custom, Files.readAllBytes(hosts));
    }

    @Test public void repeatedSeedIsIdempotent() throws Exception {
        Path etc = Files.createDirectory(temporaryFolder.getRoot().toPath().resolve("etc"));

        assertTrue(GuestHostsFile.seedIfEmpty(etc));
        byte[] first = Files.readAllBytes(etc.resolve("hosts"));
        assertFalse(GuestHostsFile.seedIfEmpty(etc));
        assertArrayEquals(first, Files.readAllBytes(etc.resolve("hosts")));
    }

    @Test public void refusesHostFileAndEtcDirectorySymlinks() throws Exception {
        Path root = temporaryFolder.getRoot().toPath();
        Path etc = Files.createDirectory(root.resolve("etc"));
        Path outside = Files.write(root.resolve("outside-hosts"), "preserved".getBytes(StandardCharsets.UTF_8));
        Path hostsLink = etc.resolve("hosts");
        createSymlinkOrSkip(hostsLink, outside);

        assertThrows(IOException.class, () -> GuestHostsFile.seedIfEmpty(etc));
        assertTrue(Files.isSymbolicLink(hostsLink));
        assertEquals("preserved", Files.readString(outside, StandardCharsets.UTF_8));

        Path etcLink = root.resolve("etc-link");
        try {
            Files.createSymbolicLink(etcLink, etc);
        } catch (UnsupportedOperationException | IOException | SecurityException unavailable) {
            Assume.assumeNoException(unavailable);
        }
        assertThrows(IOException.class, () -> GuestHostsFile.seedIfEmpty(etcLink));
        assertTrue(Files.isSymbolicLink(etcLink));
    }

    @Test public void failedAtomicReplacementPreservesSentinelAndCleansTemporaryFile() throws Exception {
        Path root = temporaryFolder.getRoot().toPath();
        Path targetDirectory = Files.createDirectory(root.resolve("hosts"));
        Path sentinel = Files.write(targetDirectory.resolve("sentinel"), "preserved".getBytes(StandardCharsets.UTF_8));

        assertThrows(IOException.class, () -> GuestHostsFile.writeAtomically(targetDirectory, EXPECTED));

        assertEquals("preserved", Files.readString(sentinel, StandardCharsets.UTF_8));
        try (Stream<Path> files = Files.list(root)) {
            assertEquals(Set.of(targetDirectory), Set.copyOf(files.toList()));
        }
    }

    private static void createSymlinkOrSkip(Path link, Path target) throws IOException {
        try {
            Files.createSymbolicLink(link, target);
        } catch (UnsupportedOperationException | IOException | SecurityException unavailable) {
            Assume.assumeNoException(unavailable);
        }
    }
}
