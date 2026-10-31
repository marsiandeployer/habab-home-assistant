package ru.habab.home;

import org.json.JSONObject;

/** Immutable model identity, captured with each diagnostic rather than read later from preferences. */
final class WakeEngineInfo {
    final String engineId, modelId, modelVersion, modelSha256, frontendSha256, phrase;
    final double threshold;
    final int slidingWindow;
    final int contextFrames, hopMs;

    WakeEngineInfo(String engineId, String modelId, String modelVersion, String modelSha256,
            String frontendSha256, double threshold, int slidingWindow, String phrase) {
        this(engineId, modelId, modelVersion, modelSha256, frontendSha256,
                threshold, slidingWindow, phrase, 0, 0);
    }

    WakeEngineInfo(String engineId, String modelId, String modelVersion, String modelSha256,
            String frontendSha256, double threshold, int slidingWindow, String phrase,
            int contextFrames, int hopMs) {
        if (engineId == null || !engineId.matches("[a-z][a-z0-9_]{0,31}")
                || modelSha256 == null || !modelSha256.matches("[a-f0-9]{64}")
                || !Double.isFinite(threshold) || threshold < 0 || threshold > 1
                || slidingWindow < 1 || slidingWindow > 100)
            throw new IllegalArgumentException("wake_engine_identity");
        this.engineId = engineId; this.modelId = modelId; this.modelVersion = modelVersion;
        this.modelSha256 = modelSha256; this.frontendSha256 = frontendSha256;
        this.threshold = threshold; this.slidingWindow = slidingWindow; this.phrase = phrase;
        this.contextFrames = contextFrames; this.hopMs = hopMs;
    }

    JSONObject toJson() {
        try {
            return new JSONObject().put("engine", engineId).put("model_id", modelId)
                    .put("model_version", modelVersion).put("model_sha256", modelSha256)
                    .put("frontend_sha256", frontendSha256).put("threshold", threshold)
                    .put("sliding_window", slidingWindow)
                    .put("context_frames", contextFrames).put("hop_ms", hopMs)
                    .put("context_unit", contextUnit()).put("app_version_code", BuildConfig.VERSION_CODE)
                    .put("threshold_kind", "vosk".equals(engineId) ? "min_word_confidence" : "model_score")
                    .put("audio_window_ms", "livekit".equals(engineId) ? 2000 : 0)
                    .put("sample_rate", 16000);
        } catch (org.json.JSONException error) { throw new IllegalStateException("wake_engine_identity", error); }
    }

    String contextUnit() {
        return "microwakeword".equals(engineId) ? "mel_frames"
                : ("openwakeword".equals(engineId) || "livekit".equals(engineId)) ? "embedding_frames" : "not_fixed";
    }
}
