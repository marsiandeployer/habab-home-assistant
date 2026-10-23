package ru.habab.home;

/** Continuous 16-to-24 kHz PCM16 resampling for the Realtime audio contract. */
final class PcmResampler {
    private short previous;
    private long inputIndex, nextTick;
    byte[] convert(short[] source, int count) {
        byte[] target = new byte[(count * 3 / 2 + 2) * 2]; int position = 0;
        for (int i = 0; i < count; i++) {
            short sample = source[i];
            while (nextTick <= inputIndex * 3) {
                long fraction = nextTick - (inputIndex - 1) * 3;
                short interpolated = inputIndex == 0 ? sample
                        : (short) (((3 - fraction) * previous + fraction * sample) / 3);
                position = write(target, position, interpolated);
                nextTick += 2;
            }
            previous = sample; inputIndex++;
        }
        return java.util.Arrays.copyOf(target, position);
    }
    private int write(byte[] target, int position, short sample) {
        target[position++] = (byte) sample; target[position++] = (byte) (sample >> 8); return position;
    }
}
