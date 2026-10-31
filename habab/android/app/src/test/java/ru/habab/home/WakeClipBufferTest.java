package ru.habab.home;

import static org.junit.Assert.*;
import java.nio.file.*;
import java.util.stream.Stream;
import org.json.JSONObject;
import org.junit.Test;

public class WakeClipBufferTest {
    private static WakeEngineInfo engine(String id, String version, char hash) {
        char[] digest = new char[64]; java.util.Arrays.fill(digest, hash);
        return new WakeEngineInfo(id, "wake-model", version, new String(digest), null, 0.42, 4, "secret phrase");
    }

    @Test public void wrapsAndSavesValidPcmWithoutPersistingBeforeWake() throws Exception {
        WakeClipBuffer ring = new WakeClipBuffer(1);
        short[] input = new short[17000];
        for (int i = 0; i < input.length; i++) input[i] = (short) i;
        Path dir = Files.createTempDirectory("wake-clips-test");
        try {
            ring.append(input, input.length);
            try (Stream<Path> files = Files.list(dir)) { assertEquals(0, files.count()); }
            short[] clip = ring.snapshot();
            assertEquals(16000, clip.length); assertEquals(1000, clip[0]); assertEquals(16999, clip[15999]);
            WakeClipBuffer.save(dir.toFile(), clip, 1234567890123L, 1);
            byte[] wav = Files.readAllBytes(dir.resolve("wake-1234567890123-1.wav"));
            assertEquals(32044, wav.length); assertEquals('R', wav[0]); assertEquals('W', wav[8]);
            assertEquals(0xe8, wav[44] & 0xff); assertEquals(0x03, wav[45] & 0xff);
            WakeClipBuffer.markForReview(dir.toFile(), 1234567890123L, 1);
            WakeClipBuffer.markForReview(dir.toFile(), 1234567890123L, 1);
            assertTrue(Files.exists(dir.resolve("wake-1234567890123-1.wav.review")));
            try { WakeClipBuffer.markForReview(dir.toFile(), 1234567890123L, 2); fail("missing clip"); }
            catch (java.io.IOException expected) { assertEquals("wake_clip_missing", expected.getMessage()); }
            ring.clear(); assertEquals(0, ring.snapshot().length);
            for (int i = 0; i < 55; i++) WakeClipBuffer.save(dir.toFile(), new short[]{1}, 1234567891000L + i, i);
            try (Stream<Path> files = Files.list(dir)) { assertEquals(50, files.count()); }
            assertFalse(Files.exists(dir.resolve("wake-1234567890123-1.wav.review")));
        } finally {
            try (Stream<Path> files = Files.list(dir)) { files.forEach(p -> p.toFile().delete()); }
            Files.deleteIfExists(dir);
        }
    }

    @Test public void savesRejectedResultWithSidecarAndBoundsCombinedRetention() throws Exception {
        Path dir = Files.createTempDirectory("wake-diagnostic-test");
        try {
            short[] pcm = new short[]{1, -2, 3};
            for (int i = 0; i < 52; i++) {
                WakeVerifier.Context context = new WakeVerifier.Context(i, 10,
                        WakeMatchGate.MatchSource.VAD_END_FINAL, 123, 1, 876);
                WakeVerifier.Result rejected = new WakeVerifier.Result(context,
                        WakeVerifier.Outcome.REJECTED, 42, 640, null, pcm, 1700000000000L + i);
                String name = WakeClipBuffer.saveDiagnostic(dir.toFile(), pcm, 1700000000000L + i, i, rejected);
                if (i == 0) assertEquals("wake-rejected-1700000000000-0.wav", name);
            }
            try (Stream<Path> files = Files.list(dir)) {
                assertEquals(50, files.filter(p -> p.getFileName().toString().endsWith(".wav")).count());
            }
            assertFalse(Files.exists(dir.resolve("wake-rejected-1700000000000-0.wav")));
            assertFalse(Files.exists(dir.resolve("wake-rejected-1700000000000-0.wav.json")));
            Path wav = dir.resolve("wake-rejected-1700000000051-51.wav");
            Path metadata = dir.resolve(wav.getFileName().toString() + ".json");
            assertTrue(Files.exists(wav)); assertTrue(Files.exists(metadata));
            byte[] bytes = Files.readAllBytes(wav);
            assertEquals(50, bytes.length); assertEquals(1, bytes[44]);
            org.json.JSONObject json = new org.json.JSONObject(new String(Files.readAllBytes(metadata),
                    java.nio.charset.StandardCharsets.UTF_8));
            assertTrue(json.getBoolean("recording_result"));
            assertFalse(json.getBoolean("ground_truth"));
            assertEquals("REJECTED", json.getString("outcome"));
            assertEquals(51, json.getLong("attempt"));
            assertEquals(10, json.getLong("attempt_started_elapsed_ms"));
            assertEquals(42, json.getLong("verify_elapsed_ms"));
            assertEquals(2, json.getLong("decoded_frames"));
            assertEquals(123, json.getInt("rms_pcm16"));
            assertEquals(876, json.getInt("confidence_permille"));
            assertEquals(1700000000051L, json.getLong("wallclock_ms"));
            long total = 0;
            try (Stream<Path> files = Files.list(dir)) {
                for (Path path : (Iterable<Path>) files.filter(p -> p.getFileName().toString().endsWith(".wav"))::iterator)
                    total += Files.size(path);
            }
            assertTrue(total <= WakeClipBuffer.MAX_RETAINED_BYTES);

            WakeVerifier.Result accepted = new WakeVerifier.Result(
                    new WakeVerifier.Context(99, 10, WakeMatchGate.MatchSource.VAD_END_FINAL, 1, 1, 2),
                    WakeVerifier.Outcome.ACCEPTED, 5, 320, pcm, pcm, 1700000000099L);
            assertEquals("wake-1700000000099-99.wav",
                    WakeClipBuffer.saveDiagnostic(dir.toFile(), pcm, 1700000000099L, 99, accepted));
        } finally {
            try (Stream<Path> files = Files.list(dir)) { files.forEach(p -> p.toFile().delete()); }
            Files.deleteIfExists(dir);
        }
    }

    @Test public void acceptedAndRejectedSidecarsKeepTheirCapturedEngineIdentity() throws Exception {
        Path dir = Files.createTempDirectory("wake-engine-sidecar");
        try {
            short[] pcm = new short[]{4, 5, 6};
            WakeEngineInfo acceptedEngine = engine("vosk", "1", 'a');
            WakeEngineInfo rejectedEngine = engine("onnx", "2", 'b');
            WakeClipBuffer.save(dir.toFile(), pcm, 1700000000123L, 21, acceptedEngine);
            WakeVerifier.Result rejected = new WakeVerifier.Result(
                    new WakeVerifier.Context(22, 10, WakeMatchGate.MatchSource.VAD_END_FINAL, 120, 1, 800),
                    WakeVerifier.Outcome.REJECTED, 30, 320, null, pcm, 1700000000456L);
            String rejectedName = WakeClipBuffer.saveDiagnostic(dir.toFile(), pcm, 1700000000456L,
                    22, rejected, rejectedEngine);

            JSONObject acceptedJson = readSidecar(dir, "wake-1700000000123-21.wav");
            assertTrue(acceptedJson.getBoolean("recording_result"));
            assertFalse(acceptedJson.getBoolean("ground_truth"));
            assertEquals("ACCEPTED", acceptedJson.getString("outcome"));
            assertEquals(21, acceptedJson.getLong("attempt"));
            assertEquals(1700000000123L, acceptedJson.getLong("timestamp_ms"));
            assertEquals("vosk", acceptedJson.getJSONObject("wake_engine").getString("engine"));
            assertEquals("1", acceptedJson.getJSONObject("wake_engine").getString("model_version"));
            assertFalse(acceptedJson.getJSONObject("wake_engine").has("phrase"));
            assertFalse(acceptedJson.toString().contains("secret phrase"));

            JSONObject rejectedJson = readSidecar(dir, rejectedName);
            assertEquals("REJECTED", rejectedJson.getString("outcome"));
            assertEquals("onnx", rejectedJson.getJSONObject("wake_engine").getString("engine"));
            assertEquals("2", rejectedJson.getJSONObject("wake_engine").getString("model_version"));
            assertEquals("b".repeat(64), rejectedJson.getJSONObject("wake_engine").getString("model_sha256"));
        } finally {
            try (Stream<Path> files = Files.list(dir)) { files.forEach(p -> p.toFile().delete()); }
            Files.delete(dir);
        }
    }

    private static JSONObject readSidecar(Path dir, String wavName) throws Exception {
        return new JSONObject(new String(Files.readAllBytes(dir.resolve(wavName + ".json")),
                java.nio.charset.StandardCharsets.UTF_8));
    }
}
