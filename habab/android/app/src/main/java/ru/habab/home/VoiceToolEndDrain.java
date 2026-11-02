package ru.habab.home;

import java.util.HashSet;
import java.util.Set;

/** Tracks provider responses and queued audio that must drain before a tool-triggered end. */
final class VoiceToolEndDrain {
    private final Set<String> responses = new HashSet<>();
    private final Set<String> audioItems = new HashSet<>();
    private long deadline = -1;

    synchronized void begin(long now, long timeoutMs) {
        if (deadline < 0) deadline = now + timeoutMs;
    }

    synchronized void responseStarted(String id) { responses.add(id); }
    synchronized void responseFinished(String id) { responses.remove(id); }
    synchronized void audioStarted(String id) { audioItems.add(id); }
    synchronized void audioFinished(String id) { audioItems.remove(id); }

    synchronized void interrupted() { responses.clear(); audioItems.clear(); }

    synchronized boolean ready(long now) {
        return deadline >= 0 && (now >= deadline || (responses.isEmpty() && audioItems.isEmpty()));
    }
}
