package ru.habab.home;

import java.util.Arrays;

/** Stateful mono PCM16 linear resampler that preserves phase across audio chunks. */
final class PcmRateConverter {
    private final int inputRate;
    private final int outputRate;
    private long inputFrames;
    private long nextOutputFrame;
    private short previousSample;
    private boolean hasPreviousSample;

    PcmRateConverter(int inputRate, int outputRate) {
        if (inputRate <= 0 || outputRate <= 0) throw new IllegalArgumentException("invalid_sample_rate");
        this.inputRate = inputRate;
        this.outputRate = outputRate;
    }

    synchronized short[] convert(short[] input, int count) {
        if (count < 0 || count > input.length) throw new IllegalArgumentException("invalid_sample_count");
        if (count == 0) return new short[0];
        int capacity = (int) Math.min(Integer.MAX_VALUE,
                ((long) count * outputRate + inputRate - 1) / inputRate + 2);
        short[] output = new short[capacity];
        int outputCount = 0;
        for (int i = 0; i < count; i++) {
            short sample = input[i];
            long currentTime = inputFrames * (long) outputRate;
            while (nextOutputFrame * (long) inputRate <= currentTime) {
                short converted;
                if (!hasPreviousSample) converted = sample;
                else {
                    long previousTime = (inputFrames - 1) * (long) outputRate;
                    long outputTime = nextOutputFrame * (long) inputRate;
                    long fraction = outputTime - previousTime;
                    converted = (short) (((outputRate - fraction) * previousSample + fraction * sample) / outputRate);
                }
                if (outputCount == output.length) output = Arrays.copyOf(output, output.length + 2);
                output[outputCount++] = converted;
                nextOutputFrame++;
            }
            previousSample = sample;
            hasPreviousSample = true;
            inputFrames++;
        }
        return Arrays.copyOf(output, outputCount);
    }

    synchronized byte[] convertPcm16Le(byte[] input) {
        if ((input.length & 1) != 0) throw new IllegalArgumentException("invalid_pcm16_length");
        short[] samples = new short[input.length / 2];
        for (int i = 0; i < samples.length; i++)
            samples[i] = (short) ((input[i * 2] & 0xff) | (input[i * 2 + 1] << 8));
        short[] converted = convert(samples, samples.length);
        byte[] output = new byte[converted.length * 2];
        for (int i = 0; i < converted.length; i++) {
            output[i * 2] = (byte) converted[i];
            output[i * 2 + 1] = (byte) (converted[i] >> 8);
        }
        return output;
    }
}
