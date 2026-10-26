package ru.habab.home;

import java.util.List;
import java.util.Map;
import org.junit.Test;
import static org.junit.Assert.*;

public class VoiceLifecycleTraceTest {
    @Test public void traceKeepsOnlyTheLatest32ImmutableFixedVocabularyEvents() {
        VoiceLifecycleTrace trace = new VoiceLifecycleTrace();
        for (int i = 0; i < 40; i++) trace.record(VoiceLifecycleTrace.Event.CAPTURE_STARTED,
                VoiceLifecycleTrace.Reason.SETTINGS, i, i % 3, 1000L + i);
        List<Map<String, Object>> snapshot = trace.snapshot();
        assertEquals(32, snapshot.size());
        assertEquals(8L, snapshot.get(0).get("generation"));
        assertEquals("CAPTURE_STARTED", snapshot.get(0).get("event"));
        assertEquals("SETTINGS", snapshot.get(0).get("reason"));
        assertThrows(UnsupportedOperationException.class, () -> snapshot.clear());
        assertThrows(UnsupportedOperationException.class, () -> snapshot.get(0).put("event", "changed"));
        trace.record(VoiceLifecycleTrace.Event.DRAINED, VoiceLifecycleTrace.Reason.SERVICE, 41, 0, 1041L);
        assertEquals(8L, snapshot.get(0).get("generation"));
        assertEquals(9L, trace.snapshot().get(0).get("generation"));
    }
}
