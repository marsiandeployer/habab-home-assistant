package ru.habab.home;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.NoSuchFileException;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.BasicFileAttributes;

/** Seeds loopback names in the private guest root without following filesystem links. */
final class GuestHostsFile {
    private static final byte[] LOOPBACK_HOSTS =
            "127.0.0.1 localhost\n::1 localhost ip6-localhost ip6-loopback\n".getBytes(StandardCharsets.US_ASCII);

    private GuestHostsFile() {}

    static boolean seedIfEmpty(Path etcDirectory) throws IOException {
        BasicFileAttributes directory = attributesIfPresent(etcDirectory);
        if (directory == null || directory.isSymbolicLink() || !directory.isDirectory()) {
            throw new IOException("Guest /etc is not a regular directory");
        }

        Path hosts = etcDirectory.resolve("hosts");
        BasicFileAttributes current = attributesIfPresent(hosts);
        if (current != null) {
            if (current.isSymbolicLink()) throw new IOException("Guest /etc/hosts must not be a symbolic link");
            if (!current.isRegularFile()) throw new IOException("Guest /etc/hosts is not a regular file");
            if (current.size() > 0) return false;
        }

        writeAtomically(hosts, LOOPBACK_HOSTS);
        return true;
    }

    static void writeAtomically(Path target, byte[] contents) throws IOException {
        Path absoluteTarget = target.toAbsolutePath();
        Path directory = absoluteTarget.getParent();
        Path temporary = null;
        try {
            temporary = Files.createTempFile(directory, "hosts.", ".tmp");
            Files.write(temporary, contents, StandardOpenOption.TRUNCATE_EXISTING, StandardOpenOption.WRITE);
            Files.move(temporary, absoluteTarget, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
            temporary = null;
        } finally {
            if (temporary != null) Files.deleteIfExists(temporary);
        }
    }

    private static BasicFileAttributes attributesIfPresent(Path path) throws IOException {
        try {
            return Files.readAttributes(path, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
        } catch (NoSuchFileException missing) {
            return null;
        }
    }
}
