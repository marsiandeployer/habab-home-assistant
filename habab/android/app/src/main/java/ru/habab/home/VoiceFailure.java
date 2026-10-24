package ru.habab.home;

import java.io.IOException;
import java.net.SocketTimeoutException;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;

/** Only known codes cross from HTTP/native exceptions into status, UI or disk. */
final class VoiceFailure extends IOException {
    private static final Set<String> CODES = new HashSet<>(Arrays.asList(
            "openai_required", "provider_required", "realtime_provider_required", "realtime_provider_rejected", "realtime_unavailable",
            "invalid_realtime_response", "unsupported_voice_protocol", "voice_history_protocol_required", "not_allowed", "invalid_voice_settings", "voice_rate_limited",
            "invalid_voice_turn", "empty_assistant_reply", "voice_authorization_required",
            "voice_authorization_expired", "voice_authorization_not_saved", "invalid_voice_authorization",
            "voice_assistant_failed", "voice_connection_failed", "voice_timeout", "invalid_voice_endpoint",
            "invalid_realtime_event", "realtime_connection_failed", "realtime_connection_closed",
            "realtime_upload_backpressure", "realtime_send_failed", "invalid_voice_tool", "invalid_voice_command",
            "voice_playback_timeout", "voice_audio_busy", "voice_audio_interrupted", "voice_history_failed", "realtime_response_failed", "realtime_provider_error",
            "realtime_playback_backpressure", "voice_playback_failed", "voice_playback_unavailable",
            "voice_connection_too_slow", "voice_session_ended", "voice_signal_unavailable", "microphone_permission_revoked", "microphone_unavailable",
            "microphone_read_failed", "invalid_wake_phrase", "unsupported_wake_word",
            "voice_model_checksum_mismatch", "invalid_voice_model", "voice_model_too_large",
            "incomplete_voice_model", "voice_model_unavailable"));

    VoiceFailure(String code) { super(normalize(code)); }

    static String normalize(String code) { return CODES.contains(code) ? code : "voice_connection_failed"; }

    static VoiceFailure fromHttp(int status, String message) {
        if (CODES.contains(message)) return new VoiceFailure(message);
        if (status == 401 || status == 403) return new VoiceFailure("voice_authorization_expired");
        if (status == 429) return new VoiceFailure("voice_rate_limited");
        return new VoiceFailure(status >= 500 ? "realtime_unavailable" : "voice_connection_failed");
    }

    static String code(Throwable error) {
        if (error instanceof SocketTimeoutException) return "voice_timeout";
        if (error instanceof java.security.GeneralSecurityException) return "voice_authorization_required";
        if (error instanceof LinkageError) return "voice_model_unavailable";
        return normalize(error.getMessage());
    }
}
