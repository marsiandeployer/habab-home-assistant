package ru.habab.home;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;

/** Publishes complete voice status snapshots without exposing truncated files to readers. */
final class VoiceStatusFile {
    private VoiceStatusFile() {}

    static void write(Path target, byte[] contents) throws IOException {
        Path directory = target.toAbsolutePath().getParent();
        Path temporary = null;
        try {
            temporary = Files.createTempFile(directory, target.getFileName().toString() + ".", ".tmp");
            Files.write(temporary, contents);
            Files.move(temporary, target, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
            temporary = null;
        } finally {
            if (temporary != null) Files.deleteIfExists(temporary);
        }
    }
}
