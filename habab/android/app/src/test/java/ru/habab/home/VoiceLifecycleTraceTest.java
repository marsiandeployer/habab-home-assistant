package ru.habab.home;

import java.util.List;
import java.util.Map;
import org.junit.Test;
import static org.junit.Assert.*;

public class VoiceLifecycleTraceTest {
    private static WakeEngineInfo engine(String id, String version, char hash) {
        char[] digest = new char[64]; java.util.Arrays.fill(digest, hash);
        char[] frontend = new char[64]; java.util.Arrays.fill(frontend, 'c');
        int contextFrames = "onnx".equals(id) ? 16 : 204;
        int hopMs = "onnx".equals(id) ? 20 : 10;
        return new WakeEngineInfo(id, "model", version, new String(digest), new String(frontend),
                0.5, 3, "phrase", contextFrames, hopMs);
    }

    @Test public void traceKeepsOnlyTheLatest64ImmutableFixedVocabularyEvents() {
        VoiceLifecycleTrace trace = new VoiceLifecycleTrace();
        for (int i = 0; i < 80; i++) trace.record(VoiceLifecycleTrace.Event.CAPTURE_STARTED,
                VoiceLifecycleTrace.Reason.SETTINGS, i, i % 3, 1000L + i);
        List<Map<String, Object>> snapshot = trace.snapshot();
        assertEquals(64, snapshot.size());
        assertEquals(16L, snapshot.get(0).get("generation"));
        assertEquals("CAPTURE_STARTED", snapshot.get(0).get("event"));
        assertEquals("SETTINGS", snapshot.get(0).get("reason"));
        assertThrows(UnsupportedOperationException.class, () -> snapshot.clear());
        assertThrows(UnsupportedOperationException.class, () -> snapshot.get(0).put("event", "changed"));
        trace.record(VoiceLifecycleTrace.Event.DRAINED, VoiceLifecycleTrace.Reason.SERVICE, 41, 0, 1041L);
        assertEquals(16L, snapshot.get(0).get("generation"));
        assertEquals(17L, trace.snapshot().get(0).get("generation"));
    }

    @Test public void wakeTraceStoresOnlyBoundedMetricsAndMatchFlags() {
        VoiceLifecycleTrace trace = new VoiceLifecycleTrace();
        trace.recordWake(VoiceLifecycleTrace.Event.WAKE_FINAL, VoiceLifecycleTrace.Reason.VAD_END,
                7, 12, 2, 7, 7, 16000, -1, 2, 1000, 1100, 2400, 0, -1, 1, 910, 100);
        Map<String, Object> event = trace.snapshot().get(0);
        assertEquals("WAKE_FINAL", event.get("event"));
        assertEquals(12L, event.get("attempt"));
        assertEquals(2, event.get("route"));
        assertEquals(7, event.get("inputDeviceType"));
        assertEquals(7, event.get("audioSource"));
        assertEquals(16000, event.get("sampleRate"));
        assertEquals(-1, event.get("captureSilenced"));
        assertEquals(2, event.get("recordingEffects"));
        assertEquals(2400, event.get("rmsPcm16"));
        assertEquals(0, event.get("vadSpeech"));
        assertEquals(-1, event.get("partialPresent"));
        assertEquals(1, event.get("phraseMatch"));
        assertEquals(910, event.get("confidencePermille"));
        assertEquals(100L, event.get("elapsedFromWakeMs"));
        assertFalse(event.containsKey("text"));
        assertFalse(event.containsKey("audio"));
    }

    @Test public void engineIdentityIsCopiedIntoEachBoundedTraceRecord() {
        VoiceLifecycleTrace trace = new VoiceLifecycleTrace();
        WakeEngineInfo first = engine("vosk", "one", 'a');
        WakeEngineInfo second = engine("onnx", "two", 'b');
        trace.setWakeEngine(first);
        trace.recordWake(VoiceLifecycleTrace.Event.WAKE_FINAL, VoiceLifecycleTrace.Reason.VAD_END,
                1, 10, 0, 1, 1, 16000, -1, 0, 100, 110, 10, 1, 1, 1, 900, 10);
        trace.setWakeEngine(second);
        trace.recordWake(VoiceLifecycleTrace.Event.WAKE_REJECTED, VoiceLifecycleTrace.Reason.VERIFIER_REJECTED,
                1, 11, 0, 1, 1, 16000, -1, 0, 120, 130, 10, 1, 1, 0, 0, 20);
        // An asynchronous record can still use the immutable snapshot from the original attempt.
        trace.recordWake(VoiceLifecycleTrace.Event.WAKE_ACCEPTED, VoiceLifecycleTrace.Reason.EXACT_FINAL,
                1, 10, 0, 1, 1, 16000, -1, 0, 140, 150, 12, 1, 1, 1, 900, 40, first);

        List<Map<String, Object>> snapshot = trace.snapshot();
        Map<String, Object> firstIdentity = (Map<String, Object>) snapshot.get(0).get("wake_engine");
        Map<String, Object> secondIdentity = (Map<String, Object>) snapshot.get(1).get("wake_engine");
        Map<String, Object> asyncIdentity = (Map<String, Object>) snapshot.get(2).get("wake_engine");
        assertEquals("vosk", firstIdentity.get("engine"));
        assertEquals("one", firstIdentity.get("model_version"));
        assertEquals("c".repeat(64), firstIdentity.get("frontend_sha256"));
        assertEquals(204, firstIdentity.get("context_frames"));
        assertEquals(10, firstIdentity.get("hop_ms"));
        assertEquals("onnx", secondIdentity.get("engine"));
        assertEquals("two", secondIdentity.get("model_version"));
        assertEquals(16, secondIdentity.get("context_frames"));
        assertEquals("vosk", asyncIdentity.get("engine"));
        assertEquals("one", asyncIdentity.get("model_version"));
        assertEquals("a".repeat(64), firstIdentity.get("model_sha256"));
        assertThrows(UnsupportedOperationException.class, () -> firstIdentity.put("engine", "changed"));
        trace.setWakeEngine(second);
        assertEquals("vosk", ((Map<String, Object>) trace.snapshot().get(0).get("wake_engine")).get("engine"));
        assertFalse(snapshot.get(0).containsKey("phrase"));
    }
}
