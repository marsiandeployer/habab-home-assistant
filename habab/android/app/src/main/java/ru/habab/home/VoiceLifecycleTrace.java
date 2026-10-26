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
        CAPTURE_STARTED, CAPTURE_CLEANED, DESTROY_REQUESTED, ROUTE_RELEASED, DRAINED }
    enum Reason { AUTOMATIC, SETTINGS, STOP, SERVICE }
    private static final int LIMIT = 32;
    private final ArrayDeque<Map<String, Object>> events = new ArrayDeque<>();

    synchronized void record(Event event, Reason reason, long generation, int route, long at) {
        Map<String, Object> item = new LinkedHashMap<>();
        item.put("event", event.name()); item.put("reason", reason.name());
        item.put("generation", generation); item.put("route", route); item.put("at", at);
        events.addLast(Collections.unmodifiableMap(item));
        while (events.size() > LIMIT) events.removeFirst();
    }

    synchronized List<Map<String, Object>> snapshot() {
        return Collections.unmodifiableList(new ArrayList<>(events));
    }
}
