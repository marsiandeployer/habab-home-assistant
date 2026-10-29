package ru.habab.home;

/** Local, bounded cues; no recording or network is involved. */
final class VoiceCuePcm {
    static final int RATE = 24000;
    private VoiceCuePcm() {}
    static short[] create(VoiceSessionSignals.Cue cue) {
        int duration = cue == VoiceSessionSignals.Cue.START ? 120 : cue == VoiceSessionSignals.Cue.END ? 220 : 360;
        short[] samples = new short[RATE * duration / 1000];
        for (int i = 0; i < samples.length; i++) {
            double ms = i * 1000.0 / RATE;
            double local = ms, length = duration, frequency = cue == VoiceSessionSignals.Cue.START ? 1100 : 330;
            if (cue == VoiceSessionSignals.Cue.END) {
                if (ms >= 80 && ms < 140) continue;
                local = ms < 80 ? ms : ms - 140;
                length = 80; frequency = ms < 80 ? 880 : 660;
            }
            if (cue == VoiceSessionSignals.Cue.ERROR) {
                local = ms % 140;
                if (local >= 80) continue;
                length = 80; frequency = 330;
            }
            double fade = Math.max(0, Math.min(1, Math.min(local, length - local - 1000.0 / RATE) / 5));
            samples[i] = (short) (8192 * fade * Math.sin(2 * Math.PI * frequency * local / 1000));
        }
        return samples;
    }
}
