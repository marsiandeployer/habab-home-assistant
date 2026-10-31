package ru.habab.home;

import static org.junit.Assert.*;
import java.nio.file.*;
import java.util.stream.Stream;
import org.json.JSONObject;
import org.junit.Test;

public class VoiceTimingJournalTest {
    private static WakeEngineInfo engine(String id, String version, char hash, String phrase) {
        char[] digest = new char[64]; java.util.Arrays.fill(digest, hash);
        char[] frontend = new char[64]; java.util.Arrays.fill(frontend, 'c');
        int contextFrames = "onnx".equals(id) ? 16 : 204;
        int hopMs = "onnx".equals(id) ? 20 : 10;
        return new WakeEngineInfo(id, "model", version, new String(digest), new String(frontend),
                0.37, 5, phrase, contextFrames, hopMs);
    }

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

    @Test public void rowsKeepExplicitEngineSnapshotWithoutPhraseOrAudio() throws Exception {
        Path dir = Files.createTempDirectory("voice-journal-engine");
        try {
            WakeEngineInfo captured = engine("vosk", "v1", 'a', "private phrase");
            WakeEngineInfo laterSelection = engine("onnx", "v2", 'b', "other phrase");
            VoiceTimingJournal.append(dir.toFile(), "wake_to_ready_ms", 31, 100, 200, 300, 25, 24, captured);
            VoiceTimingJournal.append(dir.toFile(), "wake_to_ready_ms", 32, 101, 201, 301, 26, 25, laterSelection);
            java.util.List<String> lines = Files.readAllLines(dir.resolve("voice-timing.jsonl"));
            JSONObject first = new JSONObject(lines.get(0)).getJSONObject("wake_engine");
            JSONObject second = new JSONObject(lines.get(1)).getJSONObject("wake_engine");
            assertEquals("vosk", first.getString("engine"));
            assertEquals("v1", first.getString("model_version"));
            assertEquals("a".repeat(64), first.getString("model_sha256"));
            assertEquals("c".repeat(64), first.getString("frontend_sha256"));
            assertEquals(204, first.getInt("context_frames"));
            assertEquals(10, first.getInt("hop_ms"));
            assertEquals(0.37, first.getDouble("threshold"), 0.0001);
            assertEquals(5, first.getInt("sliding_window"));
            assertEquals("onnx", second.getString("engine"));
            assertEquals("v2", second.getString("model_version"));
            assertEquals(16, second.getInt("context_frames"));
            assertFalse(lines.get(0).contains("private phrase"));
            assertFalse(lines.get(0).contains("audio"));
            assertFalse(first.has("phrase"));
        } finally {
            try (Stream<Path> files = Files.list(dir)) { files.forEach(p -> p.toFile().delete()); }
            Files.delete(dir);
        }
    }
}
