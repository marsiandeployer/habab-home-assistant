package ru.habab.home;

/** Bounded, transcript-free reason labels for voice-session completion diagnostics. */
final class VoiceFinishTrigger {
    static final String TOOL = "tool";
    static final String IDLE_20S = "idle-20s";
    static final String AUDIO_LIMIT = "audio-limit";
    static final String TIMEOUT = "timeout";
    static final String ERROR = "error";
    static final String SERVICE_STOP = "service-stop";
    static final String NORMAL = "normal";

    private VoiceFinishTrigger() {}

    static String fromFailureCode(String code) {
        return code != null && code.endsWith("_timeout") ? TIMEOUT : ERROR;
    }
}
