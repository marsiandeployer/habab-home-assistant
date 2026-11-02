package ru.habab.home;

import org.junit.Test;
import static org.junit.Assert.*;

public class WakeSensitivityTest {
    @Test public void midpointPreservesCurrentVoskAndModelThresholds() {
        assertEquals(0.800, WakeSensitivity.threshold("vosk", 50, 0.800), 0.0001);
        assertEquals(0.500, WakeSensitivity.threshold("openwakeword", 50, 0.500), 0.0001);
        assertEquals(0.500, WakeSensitivity.threshold("livekit", 50, 0.500), 0.0001);
    }

    @Test public void lowerSensitivityRaisesAndHigherSensitivityLowersThreshold() {
        assertEquals(0.950, WakeSensitivity.threshold("vosk", 0, 0.800), 0.0001);
        assertEquals(0.500, WakeSensitivity.threshold("vosk", 100, 0.800), 0.0001);
        assertEquals(0.900, WakeSensitivity.threshold("openwakeword", 0, 0.500), 0.0001);
        assertEquals(0.100, WakeSensitivity.threshold("openwakeword", 100, 0.500), 0.0001);
    }

    @Test public void progressIsClampedAndPreferencesArePerEngine() {
        assertEquals(0, WakeSensitivity.clampProgress(-1));
        assertEquals(100, WakeSensitivity.clampProgress(101));
        assertNotEquals(WakeSensitivity.preferenceKey("vosk"), WakeSensitivity.preferenceKey("livekit"));
    }
}
