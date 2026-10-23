package ru.habab.home;

import java.io.*;
import java.nio.file.*;
import java.nio.file.attribute.PosixFilePermission;
import java.util.*;
import org.apache.commons.compress.archivers.tar.*;
import org.apache.commons.compress.compressors.gzip.*;

/** Archive extraction never follows a link created by an earlier entry. */
final class ArchiveFiles {
    private ArchiveFiles() {}

    static Path destination(Path root, String name) throws IOException {
        Path base = root.toAbsolutePath().normalize();
        Path result = base.resolve(name).normalize();
        if (name.startsWith("/") || !result.startsWith(base) || name.indexOf('\0') >= 0)
            throw new IOException("Unsafe archive path");
        for (Path p = result; p != null && !p.equals(base); p = p.getParent())
            if (Files.isSymbolicLink(p)) throw new IOException("Archive path traverses a symlink");
        return result;
    }

    static void extract(InputStream source, File directory, long limit) throws Exception {
        extract(source, directory, limit, false);
    }

    static void extract(InputStream source, File directory, long limit, boolean guestLinks) throws Exception {
        Path root = directory.toPath();
        Files.createDirectories(root);
        long total = 0;
        int count = 0;
        List<TarArchiveEntry> hardlinks = new ArrayList<>();
        try (TarArchiveInputStream tar = new TarArchiveInputStream(new GzipCompressorInputStream(source))) {
            TarArchiveEntry entry;
            byte[] buffer = new byte[128 * 1024];
            while ((entry = tar.getNextEntry()) != null) {
                if (Thread.currentThread().isInterrupted()) throw new InterruptedIOException();
                if (++count > 300000) throw new IOException("Too many archive entries");
                Path target = destination(root, entry.getName());
                if (entry.isDirectory()) { Files.createDirectories(target); continue; }
                Files.createDirectories(target.getParent());
                if (entry.isSymbolicLink()) {
                    if (entry.getLinkName().indexOf('\0') >= 0) throw new IOException("Invalid symlink");
                    Path link = Paths.get(entry.getLinkName());
                    if (!guestLinks && (link.isAbsolute() || !target.getParent().resolve(link).normalize().startsWith(root.toAbsolutePath().normalize())))
                        throw new IOException("Backup symlink escapes configuration");
                    Files.createSymbolicLink(target, link);
                } else if (entry.isLink()) {
                    hardlinks.add(entry);
                } else if (entry.isFile()) {
                    if (entry.getSize() < 0 || entry.getSize() > limit - total)
                        throw new IOException("Archive exceeds its declared size");
                    total += entry.getSize();
                    try (OutputStream out = Files.newOutputStream(target, StandardOpenOption.CREATE_NEW)) {
                        int n;
                        while ((n = tar.read(buffer)) != -1) out.write(buffer, 0, n);
                    }
                    Set<PosixFilePermission> permissions = EnumSet.noneOf(PosixFilePermission.class);
                    PosixFilePermission[] bits = PosixFilePermission.values();
                    for (int i = 0; i < bits.length; i++)
                        if ((entry.getMode() & (1 << (8 - i))) != 0) permissions.add(bits[i]);
                    Files.setPosixFilePermissions(target, permissions);
                } else throw new IOException("Unsupported special archive entry");
            }
        }
        for (TarArchiveEntry entry : hardlinks) {
            Path target = destination(root, entry.getName());
            Path original = destination(root, entry.getLinkName());
            if (!Files.isRegularFile(original, LinkOption.NOFOLLOW_LINKS)) throw new IOException("Invalid hardlink");
            // Android can deny link(2) inside an app's own private directory.
            // Materialize the same bytes and permissions without requiring it.
            long bytes = Files.size(original);
            if (bytes > limit - total) throw new IOException("Archive exceeds its declared size");
            total += bytes;
            Files.copy(original, target);
            Files.setPosixFilePermissions(target, Files.getPosixFilePermissions(original, LinkOption.NOFOLLOW_LINKS));
        }
    }

    static void pack(File directory, OutputStream destination) throws Exception {
        try (TarArchiveOutputStream tar = new TarArchiveOutputStream(new GzipCompressorOutputStream(destination))) {
            tar.setLongFileMode(TarArchiveOutputStream.LONGFILE_POSIX);
            tar.setBigNumberMode(TarArchiveOutputStream.BIGNUMBER_POSIX);
            try (java.util.stream.Stream<Path> paths = Files.walk(directory.toPath())) {
                Iterator<Path> iterator = paths.iterator();
                while (iterator.hasNext()) {
                    Path path = iterator.next();
                    if (path.equals(directory.toPath())) continue;
                    String name = directory.toPath().relativize(path).toString();
                    TarArchiveEntry entry;
                    if (Files.isSymbolicLink(path)) {
                        entry = new TarArchiveEntry(name, TarConstants.LF_SYMLINK);
                        entry.setLinkName(Files.readSymbolicLink(path).toString());
                        entry.setSize(0);
                    } else entry = new TarArchiveEntry(path, name, LinkOption.NOFOLLOW_LINKS);
                    if (!Files.isSymbolicLink(path)) {
                        Set<PosixFilePermission> permissions = Files.getPosixFilePermissions(path, LinkOption.NOFOLLOW_LINKS);
                        int mode = 0; PosixFilePermission[] bits = PosixFilePermission.values();
                        for (int i = 0; i < bits.length; i++) if (permissions.contains(bits[i])) mode |= 1 << (8 - i);
                        entry.setMode(mode);
                    }
                    tar.putArchiveEntry(entry);
                    if (Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS)) Files.copy(path, tar);
                    tar.closeArchiveEntry();
                }
            }
        }
    }

    static void delete(File directory) throws IOException {
        Path path = directory.toPath();
        if (!Files.exists(path, LinkOption.NOFOLLOW_LINKS)) return;
        if (Files.isDirectory(path, LinkOption.NOFOLLOW_LINKS)) {
            // PRoot creates mode-000 placeholders for Android bind mounts.
            // Restore owner traversal before opening the directory; never
            // follow a symlink into Android or another runtime.
            Set<PosixFilePermission> permissions = Files.getPosixFilePermissions(path, LinkOption.NOFOLLOW_LINKS);
            permissions.add(PosixFilePermission.OWNER_READ);
            permissions.add(PosixFilePermission.OWNER_WRITE);
            permissions.add(PosixFilePermission.OWNER_EXECUTE);
            Files.setPosixFilePermissions(path, permissions);
            try (DirectoryStream<Path> children = Files.newDirectoryStream(path)) {
                for (Path child : children) delete(child.toFile());
            }
        }
        Files.delete(path);
    }
}
