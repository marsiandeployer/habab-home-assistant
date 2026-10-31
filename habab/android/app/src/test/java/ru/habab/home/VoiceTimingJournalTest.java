package ru.habab.home;

import static org.junit.Assert.*;
import java.nio.file.*;
import java.util.stream.Stream;
import org.json.JSONObject;
import org.junit.Test;

public class VoiceTimingJournalTest {
    @Test public void correlatesMetadataAndBoundsStorage() throws Exception {
        Path dir = Files.createTempDirectory("voice-journal");
        try {
            VoiceTimingJournal.append(dir.toFile(), "wake_to_ready_ms", 17, 100, 200, 300, 25, 24);
            JSONObject event = new JSONObject(new String(Files.readAllBytes(dir.resolve("voice-timing.jsonl")), java.nio.charset.StandardCharsets.UTF_8));
            assertEquals(17, event.getLong("attempt"));
            assertEquals(100, event.getLong("captured_at_ms"));
            assertEquals(25, event.getLong("from_wake_ms"));
            assertEquals(24, event.getLong("value"));
            Files.write(dir.resolve("voice-timing.jsonl"), new byte[(int) VoiceTimingJournal.MAX_BYTES]);
            VoiceTimingJournal.append(dir.toFile(), "START_CUE_QUEUED", 18, 400, 500, 600, 0, 0);
            assertEquals(VoiceTimingJournal.MAX_BYTES, Files.size(dir.resolve("voice-timing.previous.jsonl")));
            assertTrue(Files.size(dir.resolve("voice-timing.jsonl")) < 1024);
            VoiceTimingJournal.append(dir.toFile(), "wake_to_upload_ms", 18, 400, 500, 600, 10, 10);
            assertEquals(2, Files.readAllLines(dir.resolve("voice-timing.jsonl")).size());
            try {
                VoiceTimingJournal.append(dir.toFile(), "user text\nsecret", 0, 0, 0, 0, 0, 0);
                fail("Text is not a diagnostic event name");
            } catch (IllegalArgumentException expected) { }
        } finally {
            try (Stream<Path> files = Files.list(dir)) { files.forEach(p -> p.toFile().delete()); }
            Files.delete(dir);
        }
    }
}
