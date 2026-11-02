package ru.habab.home;

/** Per-engine sensitivity mapped to the confidence/model threshold used by its detector. */
final class WakeSensitivity {
    static final int MIN_PROGRESS = 0;
    static final int DEFAULT_PROGRESS = 50;
    static final int MAX_PROGRESS = 100;

    private WakeSensitivity() {}

    static String preferenceKey(String engineId) {
        if (!WakeEngineFactory.known(engineId)) throw new IllegalArgumentException("wake_engine");
        return "wake_sensitivity_" + engineId;
    }

    static int clampProgress(int progress) {
        return Math.max(MIN_PROGRESS, Math.min(MAX_PROGRESS, progress));
    }

    static double defaultThreshold(String engineId) {
        if ("vosk".equals(engineId)) return 0.800;
        if ("openwakeword".equals(engineId) || "livekit".equals(engineId)) return 0.500;
        throw new IllegalArgumentException("wake_engine");
    }

    /** Progress 0 raises the threshold (fewer false triggers); 100 lowers it (easier to wake). */
    static double threshold(String engineId, int progress, double modelDefault) {
        double minimum, maximum;
        if ("vosk".equals(engineId)) { minimum = 0.500; maximum = 0.950; }
        else if ("openwakeword".equals(engineId) || "livekit".equals(engineId)) {
            minimum = 0.100; maximum = 0.900;
        } else throw new IllegalArgumentException("wake_engine");

        double base = Math.max(minimum, Math.min(maximum, modelDefault));
        int position = clampProgress(progress);
        double adjusted = position <= DEFAULT_PROGRESS
                ? maximum - (maximum - base) * position / DEFAULT_PROGRESS
                : base - (base - minimum) * (position - DEFAULT_PROGRESS) / DEFAULT_PROGRESS;
        return Math.round(adjusted * 1000.0) / 1000.0;
    }
}
