package ru.habab.home;

import org.junit.Test;
import static org.junit.Assert.*;

public class OnnxWakeEngineTest {
    @Test public void scoreTelemetryIsSpeechOnlyAndAtMostOncePerTwoSeconds() {
        assertFalse(OnnxWakeEngine.scoreTelemetryDue(-1, 100, 0));
        assertTrue(OnnxWakeEngine.scoreTelemetryDue(-1, 100, 1));
        assertFalse(OnnxWakeEngine.scoreTelemetryDue(100, 2099, 1));
        assertTrue(OnnxWakeEngine.scoreTelemetryDue(100, 2100, 1));
        assertFalse(OnnxWakeEngine.scoreTelemetryDue(2100, 1000, 1));
    }
}
