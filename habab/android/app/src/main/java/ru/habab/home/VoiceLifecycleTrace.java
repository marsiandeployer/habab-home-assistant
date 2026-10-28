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
        WAKE_PARTIAL_STATE, WAKE_FINAL, WAKE_ACCEPTED, WAKE_REJECTED,
        RECOGNIZER_RESET, VOICE_SESSION_BEGIN, START_CUE_QUEUED,
        REALTIME_CONNECT_START, ASR_GATE_OPEN }
    enum Reason { AUTOMATIC, SETTINGS, STOP, SERVICE, ROUTE, VAD, PARTIAL,
        ENDPOINT, VAD_END, EXACT_FINAL, TIME_LIMIT, SESSION_RESET, ERROR_RESET, CUE }
    private static final int LIMIT = 64;
    private final ArrayDeque<Map<String, Object>> events = new ArrayDeque<>();

    synchronized void record(Event event, Reason reason, long generation, int route, long at) {
        Map<String, Object> item = new LinkedHashMap<>();
        item.put("event", event.name()); item.put("reason", reason.name());
        item.put("generation", generation); item.put("route", route); item.put("at", at);
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
        events.addLast(Collections.unmodifiableMap(item));
        while (events.size() > LIMIT) events.removeFirst();
    }

    synchronized List<Map<String, Object>> snapshot() {
        return Collections.unmodifiableList(new ArrayList<>(events));
    }
}
