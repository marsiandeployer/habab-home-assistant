package ru.habab.home;

import org.junit.Test;
import static org.junit.Assert.*;

public class VoiceCuePcmTest {
    @Test public void errorHasThreeLowPulsesDistinctFromNormalEnd() {
        short[] error = VoiceCuePcm.create(VoiceSessionSignals.Cue.ERROR);
        assertEquals(8640, error.length);
        for (int begin : new int[]{0, 3360, 6720}) {
            long energy = 0;
            for (int i = begin; i < begin + 1920; i++) energy += Math.abs((int) error[i]);
            assertTrue(energy > 100000);
        }
        for (int i = 1920; i < 3360; i++) assertEquals(0, error[i]);
        for (int i = 5280; i < 6720; i++) assertEquals(0, error[i]);
        assertTrue(error.length != VoiceCuePcm.create(VoiceSessionSignals.Cue.END).length);
    }
}
