package ru.habab.home;

import java.util.*;

/** Joins asynchronous ASR, generated responses and actual playback without guessing text. */
final class VoiceTranscriptLedger implements AutoCloseable {
    static final class Turn {
        final String eventId, userText, assistantText;
        Turn(String eventId, String userText, String assistantText) {
            this.eventId = eventId; this.userText = userText; this.assistantText = assistantText;
        }
    }
    private static final class Pending {
        final String user;
        final Set<String> audio = new HashSet<>();
        String text;
        boolean complete, heard;
        Pending(String user) { this.user = user; }
    }
    private final Map<String, String> users = new LinkedHashMap<>();
    private final Map<String, Pending> responses = new LinkedHashMap<>();
    private final Set<String> canonicalUsers = new HashSet<>();
    private String currentUser;
    private boolean closed, independentOutput;

    synchronized void independentOutput() {
        if (!users.isEmpty() || !responses.isEmpty()) throw new IllegalStateException("voice_history_already_started");
        independentOutput = true;
    }

    synchronized void userStarted(String id) { if (!closed) currentUser = id; }
    synchronized void userTranscript(String id, String text) {
        if (closed || canonicalUsers.contains(id) || text.trim().isEmpty()) return;
        if (text.length() > 4000 || users.size() >= 128 && !users.containsKey(id)) throw new IllegalStateException("voice_history_backpressure");
        users.put(id, text);
    }
    synchronized void created(String id) {
        if (closed || (!independentOutput && (currentUser == null || canonicalUsers.contains(currentUser))) || responses.containsKey(id)) return;
        if (responses.size() >= 64) throw new IllegalStateException("voice_history_backpressure");
        responses.put(id, new Pending(currentUser));
    }
    synchronized void assistantTranscript(String id, String text) {
        Pending response = responses.get(id);
        if (response != null) {
            if (text.length() > 12000) throw new IllegalStateException("voice_history_backpressure");
            response.text = text;
        }
    }
    synchronized void canonicalTool(String id) {
        Pending response = responses.get(id);
        if (response == null) return;
        if (independentOutput) { responses.remove(id); return; }
        canonicalUsers.add(response.user); users.remove(response.user);
        responses.entrySet().removeIf(entry -> entry.getValue().user.equals(response.user));
    }
    synchronized void queued(String responseId, String itemId) {
        Pending response = responses.get(responseId); if (response != null) response.audio.add(itemId);
    }
    synchronized void played(String responseId, String itemId) {
        Pending response = responses.get(responseId);
        if (response != null) { response.audio.remove(itemId); response.heard = true; }
    }
    synchronized void done(String id, boolean complete) {
        Pending response = responses.get(id);
        if (response != null) { if (complete) response.complete = true; else responses.remove(id); }
    }
    synchronized void interrupted(String id) { responses.remove(id); }
    synchronized List<Turn> drain() {
        List<Turn> result = new ArrayList<>();
        if (closed) return result;
        Set<String> consumed = new HashSet<>();
        Iterator<Map.Entry<String, Pending>> entries = responses.entrySet().iterator();
        while (entries.hasNext()) {
            Map.Entry<String, Pending> entry = entries.next(); Pending response = entry.getValue();
            String user = users.get(response.user);
            if (response.complete && response.heard && response.audio.isEmpty() && (independentOutput || user != null)
                    && response.text != null && !response.text.trim().isEmpty()) {
                result.add(new Turn(entry.getKey(), independentOutput ? null : user, response.text));
                if (!independentOutput) consumed.add(response.user);
                entries.remove();
            }
        }
        for (String user : consumed) if (responses.values().stream().noneMatch(response -> response.user.equals(user))) users.remove(user);
        return result;
    }
    @Override public synchronized void close() { closed = true; users.clear(); responses.clear(); canonicalUsers.clear(); currentUser = null; }
}
