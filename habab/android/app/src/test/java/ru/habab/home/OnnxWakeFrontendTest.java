package ru.habab.home;

import java.util.ArrayList;
import java.util.List;
import org.junit.Test;
import static org.junit.Assert.*;

public class OnnxWakeFrontendTest {
    private static final class FakeRunner implements OnnxWakeFrontend.Runner {
        final List<float[]> inputs = new ArrayList<>();
        final List<long[]> shapes = new ArrayList<>();
        final float result;
        final int longMelFrames;
        final boolean markedMel;
        FakeRunner(float result) { this(result, 8, false); }
        FakeRunner(float result, int longMelFrames, boolean markedMel) {
            this.result = result; this.longMelFrames = longMelFrames; this.markedMel = markedMel;
        }
        @Override public float[] run(float[] input, long... shape) {
            inputs.add(input.clone()); shapes.add(shape.clone());
            if (shape.length == 2) {
                int frames = shape[1] == 1280 ? 5 : shape[1] == 32000 ? 197 : longMelFrames;
                float[] output = new float[frames * 32];
                if (markedMel) for (int frame = 0; frame < frames; frame++)
                    java.util.Arrays.fill(output, frame * 32, (frame + 1) * 32, frame * 10f);
                return output;
            }
            if (shape.length == 4) return new float[(int) shape[0] * 96];
            return new float[]{result};
        }
    }

    @Test public void feedsExact80msWindowsAndWaitsForSixteenRealEmbeddings() throws Exception {
        FakeRunner mel = new FakeRunner(0), embedding = new FakeRunner(0), classifier = new FakeRunner(.75f);
        List<Float> scores = new ArrayList<>();
        OnnxWakeFrontend frontend = new OnnxWakeFrontend("openwakeword", mel, embedding,
                classifier, (score, millis) -> scores.add(score));
        assertEquals(80, frontend.cadenceMs());
        short[] hop = new short[OnnxWakeFrontend.HOP_SAMPLES];
        for (int i = 0; i < hop.length; i++) hop[i] = (short) (i - 640);
        for (int i = 0; i < 15; i++) frontend.processHop(hop, hop.length);
        assertTrue(scores.isEmpty());
        frontend.processHop(hop, hop.length);
        assertEquals(1, scores.size());
        assertEquals(.75f, scores.get(0), 0f);
        assertEquals(16, embedding.inputs.size());
        assertArrayEquals(new long[]{1, 76, 32, 1}, embedding.shapes.get(0));
        assertArrayEquals(new long[]{1, 16, 96}, classifier.shapes.get(0));
        assertEquals(1280, mel.shapes.get(0)[1]);
        assertEquals(1760, mel.shapes.get(1)[1]);
        assertEquals(1760, mel.inputs.get(1).length);
        assertEquals((float) hop[0], mel.inputs.get(1)[480], 0f);
        assertEquals((float) hop[1279], mel.inputs.get(1)[1759], 0f);
    }

    @Test public void livekitUsesNormalizedPcmAndResetClearsFeatureHistory() throws Exception {
        FakeRunner mel = new FakeRunner(0), embedding = new FakeRunner(0), classifier = new FakeRunner(.9f);
        List<Float> scores = new ArrayList<>();
        OnnxWakeFrontend frontend = new OnnxWakeFrontend("livekit", mel, embedding,
                classifier, (score, millis) -> scores.add(score));
        assertEquals(160, frontend.cadenceMs());
        short[] hop = new short[OnnxWakeFrontend.LIVEKIT_HOP_SAMPLES];
        java.util.Arrays.fill(hop, (short) 16384);
        for (int i = 0; i < 12; i++) frontend.processHop(hop, hop.length);
        assertTrue(scores.isEmpty());
        frontend.processHop(hop, hop.length);
        assertEquals(1, scores.size());
        assertEquals(.5f, mel.inputs.get(0)[0], 0f);
        assertArrayEquals(new long[]{1, 32000}, mel.shapes.get(0));
        assertArrayEquals(new long[]{16, 76, 32, 1}, embedding.shapes.get(0));
        assertArrayEquals(new long[]{1, 16, 96}, classifier.shapes.get(0));
        assertEquals(2f, embedding.inputs.get(0)[0], 0f); // Python mel normalization: x/10 + 2.
        assertEquals(2f, embedding.inputs.get(0)[embedding.inputs.get(0).length - 1], 0f);
        frontend.reset();
        for (int i = 0; i < 12; i++) frontend.processHop(hop, hop.length);
        assertEquals(1, scores.size());
        frontend.processHop(hop, hop.length);
        assertEquals(2, scores.size());
        assertEquals(32000, mel.inputs.get(1).length);
        assertEquals(.5f, mel.inputs.get(1)[0], 0f);
    }

    @Test public void openWakeWordDiscardsMelContextOlderThanTheEightFrameHop() throws Exception {
        FakeRunner mel = new FakeRunner(0, 11, true), embedding = new FakeRunner(0), classifier = new FakeRunner(.2f);
        OnnxWakeFrontend frontend = new OnnxWakeFrontend("openwakeword", mel, embedding,
                classifier, (score, millis) -> {});
        short[] hop = new short[1280];
        frontend.processHop(hop, hop.length);
        frontend.processHop(hop, hop.length);
        float[] window = embedding.inputs.get(1);
        assertEquals(2f, window[63 * 32], 0f); // First hop's first mel row remains.
        assertEquals(6f, window[67 * 32], 0f);
        assertEquals(5f, window[68 * 32], 0f); // Second hop begins at mel row 3, not row 0.
        assertEquals(12f, window[75 * 32], 0f); // Only the newest eight of eleven rows were kept.
    }

    @Test public void flattenHandlesOnnxNestedOutputsAndRejectsNonNumbers() throws Exception {
        assertArrayEquals(new float[]{1, 2, 3}, OnnxWakeFrontend.flatten(new float[][]{{1, 2}, {3}}), 0f);
        try {
            OnnxWakeFrontend.flatten(new Object[]{"bad"});
            fail("expected invalid tensor");
        } catch (java.io.IOException expected) { assertEquals("invalid_onnx_tensor", expected.getMessage()); }
    }
}
