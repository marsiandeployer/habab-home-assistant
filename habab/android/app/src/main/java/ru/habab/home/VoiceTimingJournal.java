package ru.habab.home;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;

/** Local bounded metadata only. Call from the existing diagnostic writer, never capture. */
final class VoiceTimingJournal {
    static final long MAX_BYTES = 512 * 1024;

    static synchronized void append(File directory, String event, long attempt, long capturedAt,
            long wallclock, long elapsedAt, long fromWakeMs, long value) throws IOException {
        if (event == null || !event.matches("[A-Za-z][A-Za-z0-9_]{0,63}"))
            throw new IllegalArgumentException("diagnostic_event");
        if (!directory.isDirectory() && !directory.mkdirs()) throw new IOException("diagnostic_directory");
        File current = new File(directory, "voice-timing.jsonl");
        byte[] line = ("{\"event\":\"" + event + "\",\"attempt\":" + attempt
                + ",\"captured_at_ms\":" + capturedAt + ",\"wallclock_ms\":" + wallclock
                + ",\"elapsed_at_ms\":" + elapsedAt + ",\"from_wake_ms\":" + fromWakeMs
                + ",\"value\":" + value + "}\n").getBytes(StandardCharsets.UTF_8);
        if (current.length() + line.length > MAX_BYTES) {
            File previous = new File(directory, "voice-timing.previous.jsonl");
            if (previous.exists() && !previous.delete()) throw new IOException("diagnostic_rotation");
            if (!current.renameTo(previous)) throw new IOException("diagnostic_rotation");
        }
        try (FileOutputStream output = new FileOutputStream(current, true)) { output.write(line); }
    }
}
