package ru.habab.home;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertThrows;

import java.io.IOException;
import org.junit.Test;

public final class MicroWakeEngineTest {
    @Test public void quantizesFeaturesUsingTensorScaleAndZeroPoint() throws Exception {
        assertEquals((byte) 7, MicroWakeEngine.quantizeFeature(0.5f, 0.125, 3));
        assertEquals((byte) -5, MicroWakeEngine.quantizeFeature(-1f, 0.125, 3));
    }

    @Test public void quantizationClipsToSignedInt8Range() throws Exception {
        assertEquals(Byte.MAX_VALUE, MicroWakeEngine.quantizeFeature(10_000f, 0.001, 0));
        assertEquals(Byte.MIN_VALUE, MicroWakeEngine.quantizeFeature(-10_000f, 0.001, 0));
    }

    @Test public void dequantizesUnsignedOutputAndRejectsInvalidScore() throws Exception {
        assertEquals(0.5f, MicroWakeEngine.dequantizeScore(132, 0.01, 82), 0.00001f);
        assertThrows(IOException.class, () -> MicroWakeEngine.dequantizeScore(255, 0.01, 0));
    }

    @Test public void computesFiveInferenceSlidingMean() {
        assertEquals(0.98f, MicroWakeEngine.meanScore(new float[]{0.96f, 0.98f, 1f, 0.97f, 0.99f}, 5),
                0.00001f);
    }
}
