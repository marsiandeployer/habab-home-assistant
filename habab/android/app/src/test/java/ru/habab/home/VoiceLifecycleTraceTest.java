package ru.habab.home;

import java.util.List;
import java.util.Map;
import org.junit.Test;
import static org.junit.Assert.*;

public class VoiceLifecycleTraceTest {
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
}
