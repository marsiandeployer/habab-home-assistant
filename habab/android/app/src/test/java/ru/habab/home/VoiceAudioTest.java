package ru.habab.home;

import org.junit.Test;
import static org.junit.Assert.*;

public class VoiceAudioTest {
    private short[] decode(byte[] data) {
        short[] result = new short[data.length / 2];
        for (int i = 0; i < result.length; i++) result[i] = (short) ((data[i * 2] & 255) | data[i * 2 + 1] << 8);
        return result;
    }

    @Test public void resamplingInterpolatesAndKeepsPhaseAcrossAudioChunks() {
        PcmResampler whole = new PcmResampler();
        short[] ramp = new short[]{0, 300, 600, 900};
        assertArrayEquals(new short[]{0, 200, 400, 600, 800}, decode(whole.convert(ramp, ramp.length)));
        PcmResampler chunks = new PcmResampler();
        byte[] first = chunks.convert(new short[]{0, 300}, 2);
        byte[] second = chunks.convert(new short[]{600, 900}, 2);
        byte[] joined = new byte[first.length + second.length];
        System.arraycopy(first, 0, joined, 0, first.length); System.arraycopy(second, 0, joined, first.length, second.length);
        assertArrayEquals(new PcmResampler().convert(ramp, ramp.length), joined);
    }

    @Test public void resamplingDoesNotDriftOverContinuousMicrophoneFrames() {
        PcmResampler stream = new PcmResampler(); long output = 0;
        for (int frame = 0; frame < 500; frame++) output += stream.convert(new short[320], 320).length / 2;
        assertEquals(240000 - 1, output);
    }

    @Test public void gateIgnoresQuietNoiseAndRearmsAfterSpeech() {
        VoiceActivityGate gate = new VoiceActivityGate();
        short[] quiet = new short[320]; java.util.Arrays.fill(quiet, (short) 35);
        for (int i = 0; i < 100; i++) assertFalse(gate.speech(quiet, quiet.length));
        short[] speech = new short[320]; java.util.Arrays.fill(speech, (short) 1500);
        assertTrue(gate.speech(speech, speech.length));
        assertTrue(gate.speech(quiet, quiet.length));
        for (int i = 0; i < 120; i++) gate.speech(quiet, quiet.length);
        assertFalse(gate.speech(quiet, quiet.length));
    }
}
