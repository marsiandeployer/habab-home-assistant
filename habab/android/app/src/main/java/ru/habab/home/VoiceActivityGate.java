package ru.habab.home;

/** Acoustic gate only; phrase recognition is performed by the Vosk model. */
final class VoiceActivityGate {
    private double noise = 60;
    private int hangover;
    boolean speech(short[] samples, int count) {
        double power = 0;
        for (int i = 0; i < count; i++) power += (double) samples[i] * samples[i];
        double rms = Math.sqrt(power / Math.max(count, 1));
        boolean voiced = rms > Math.max(180, noise * 3);
        if (voiced) hangover = 60;
        else { noise = noise * 0.98 + rms * 0.02; if (hangover > 0) hangover--; }
        return voiced || hangover > 0;
    }
}
