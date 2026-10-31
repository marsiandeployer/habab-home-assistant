package ru.habab.home;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Fixed vocabulary and numbers only; no audio, user text, names, or credentials. */
final class VoiceLifecycleTrace {
    enum Event { START_QUEUED, START_RELEASED, START_CANCELLED, DRAIN_FAILED, CREATED,
        CAPTURE_STARTED, CAPTURE_CLEANED, DESTROY_REQUESTED, ROUTE_RELEASED, DRAINED,
        AUDIO_CAPTURE_READY, AUDIO_CAPTURE_CONFIG_CHANGED, AUDIO_CALLBACK_UNAVAILABLE, ROUTE_CHANGED,
        WAKE_VAD_START, WAKE_VAD_END,
        WAKE_PARTIAL_STATE, WAKE_MODEL_SCORE, WAKE_FINAL, WAKE_ACCEPTED, WAKE_REJECTED,
        RECOGNIZER_RESET, VOICE_SESSION_BEGIN, START_CUE_QUEUED,
        REALTIME_CONNECT_START, ASR_GATE_OPEN }
    enum Reason { AUTOMATIC, SETTINGS, STOP, SERVICE, ROUTE, VAD, PARTIAL,
        ENDPOINT, VAD_END, EXACT_FINAL, LOW_CONFIDENCE, VERIFIER_REJECTED, VERIFIER_ERROR,
        TIME_LIMIT, SESSION_RESET, ERROR_RESET, CUE }
    private static final int LIMIT = 64;
    private final ArrayDeque<Map<String, Object>> events = new ArrayDeque<>();
    private Map<String, Object> activeWakeEngine;

    /** Sets the identity used by subsequently recorded events; existing ring entries never change. */
    synchronized void setWakeEngine(WakeEngineInfo wakeEngine) {
        activeWakeEngine = wakeEngine == null ? null : engineIdentity(wakeEngine);
    }

    synchronized void record(Event event, Reason reason, long generation, int route, long at) {
        recordWithIdentity(event, reason, generation, route, at, activeWakeEngine);
    }

    /** Records an event against the immutable engine snapshot captured by its caller. */
    synchronized void record(Event event, Reason reason, long generation, int route, long at,
            WakeEngineInfo wakeEngine) {
        recordWithIdentity(event, reason, generation, route, at,
                wakeEngine == null ? null : engineIdentity(wakeEngine));
    }

    private void recordWithIdentity(Event event, Reason reason, long generation, int route, long at,
            Map<String, Object> wakeEngine) {
        Map<String, Object> item = new LinkedHashMap<>();
        item.put("event", event.name()); item.put("reason", reason.name());
        item.put("generation", generation); item.put("route", route); item.put("at", at);
        if (wakeEngine != null) item.put("wake_engine", wakeEngine);
        events.addLast(Collections.unmodifiableMap(item));
        while (events.size() > LIMIT) events.removeFirst();
    }

    /** Fixed numeric/boolean wake telemetry. -1 means unknown/not applicable; no text or audio. */
    synchronized void recordWake(Event event, Reason reason, long generation, long attempt,
            int requestedRoute, int inputDeviceType, int audioSource, int sampleRate,
            int captureSilenced, int recordingEffects,
            long at, long elapsedAt,
            int rmsPcm16, int vadSpeech, int partialPresent, int phraseMatch, int confidencePermille,
            long elapsedFromWakeMs) {
        recordWake(event, reason, generation, attempt, requestedRoute, inputDeviceType, audioSource,
                sampleRate, captureSilenced, recordingEffects, at, elapsedAt, rmsPcm16, vadSpeech,
                partialPresent, phraseMatch, confidencePermille, elapsedFromWakeMs, activeWakeEngine);
    }

    /** Records wake telemetry against the identity snapshot captured for that attempt. */
    synchronized void recordWake(Event event, Reason reason, long generation, long attempt,
            int requestedRoute, int inputDeviceType, int audioSource, int sampleRate,
            int captureSilenced, int recordingEffects,
            long at, long elapsedAt,
            int rmsPcm16, int vadSpeech, int partialPresent, int phraseMatch, int confidencePermille,
            long elapsedFromWakeMs, WakeEngineInfo wakeEngine) {
        recordWake(event, reason, generation, attempt, requestedRoute, inputDeviceType, audioSource,
                sampleRate, captureSilenced, recordingEffects, at, elapsedAt, rmsPcm16, vadSpeech,
                partialPresent, phraseMatch, confidencePermille, elapsedFromWakeMs,
                wakeEngine == null ? null : engineIdentity(wakeEngine));
    }

    private void recordWake(Event event, Reason reason, long generation, long attempt,
            int requestedRoute, int inputDeviceType, int audioSource, int sampleRate,
            int captureSilenced, int recordingEffects,
            long at, long elapsedAt,
            int rmsPcm16, int vadSpeech, int partialPresent, int phraseMatch, int confidencePermille,
            long elapsedFromWakeMs, Map<String, Object> wakeEngine) {
        Map<String, Object> item = new LinkedHashMap<>();
        item.put("event", event.name()); item.put("reason", reason.name());
        item.put("generation", generation); item.put("attempt", attempt);
        item.put("route", requestedRoute); item.put("inputDeviceType", inputDeviceType);
        item.put("audioSource", audioSource); item.put("sampleRate", sampleRate);
        item.put("captureSilenced", captureSilenced); item.put("recordingEffects", recordingEffects);
        item.put("at", at); item.put("elapsedAt", elapsedAt);
        item.put("rmsPcm16", rmsPcm16); item.put("vadSpeech", vadSpeech);
        item.put("partialPresent", partialPresent);
        item.put("phraseMatch", phraseMatch); item.put("confidencePermille", confidencePermille);
        item.put("elapsedFromWakeMs", elapsedFromWakeMs);
        if (wakeEngine != null) item.put("wake_engine", wakeEngine);
        events.addLast(Collections.unmodifiableMap(item));
        while (events.size() > LIMIT) events.removeFirst();
    }

    private static Map<String, Object> engineIdentity(WakeEngineInfo info) {
        Map<String, Object> identity = new LinkedHashMap<>();
        identity.put("engine", info.engineId); identity.put("model_id", info.modelId);
        identity.put("model_version", info.modelVersion); identity.put("model_sha256", info.modelSha256);
        identity.put("frontend_sha256", info.frontendSha256);
        identity.put("context_frames", info.contextFrames); identity.put("hop_ms", info.hopMs);
        identity.put("threshold", info.threshold); identity.put("sliding_window", info.slidingWindow);
        identity.put("context_unit", info.contextUnit());
        identity.put("app_version_code", BuildConfig.VERSION_CODE);
        return Collections.unmodifiableMap(identity);
    }

    synchronized List<Map<String, Object>> snapshot() {
        return Collections.unmodifiableList(new ArrayList<>(events));
    }
}
