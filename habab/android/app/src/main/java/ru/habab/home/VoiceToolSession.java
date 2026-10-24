package ru.habab.home;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.HashMap;
import java.util.Map;

/** Keeps an in-flight Gemini tool reply alive briefly after microphone capture stops. */
final class VoiceToolSession {
    static final long AUDIO_LIMIT_MS = 120_000;
    static final long RESULT_GRACE_MS = 45_000;

    enum Delivery { SENT, SENT_STALE, SUPPRESSED, FAILED }
    enum DeadlineAction { CAPTURE_AUDIO, DRAIN_RESULT, FINISH, TIMEOUT }
    interface Sender { boolean send(boolean silent); }

    private static final class Tool {
        final String callId;
        final long epoch;
        boolean stale;
        String responseId;
        boolean audioStarted, responseDone, audioPlayed;

        Tool(String callId, long epoch) { this.callId = callId; this.epoch = epoch; }
    }

    private final long audioDeadline;
    private final long graceDeadline;
    private final Map<String, Tool> tools = new HashMap<>();
    private final Map<String, Tool> responses = new HashMap<>();
    private final Deque<Tool> awaitingFollowup = new ArrayDeque<>();
    private long currentEpoch;
    private boolean ended;

    VoiceToolSession(long activatedAt) {
        audioDeadline = activatedAt + AUDIO_LIMIT_MS;
        graceDeadline = audioDeadline + RESULT_GRACE_MS;
    }

    synchronized boolean audioLimitReached(long now) { return now >= audioDeadline; }

    synchronized DeadlineAction deadlineAction(long now) {
        if (ended) return DeadlineAction.FINISH;
        if (now < audioDeadline) return DeadlineAction.CAPTURE_AUDIO;
        if (!hasPendingCurrentTool()) return DeadlineAction.FINISH;
        return now < graceDeadline ? DeadlineAction.DRAIN_RESULT : DeadlineAction.TIMEOUT;
    }

    synchronized boolean graceExpired(long now) {
        return !ended && now >= graceDeadline && hasPendingCurrentTool();
    }

    synchronized boolean hasPendingCurrentTool() {
        for (Tool tool : tools.values()) if (!tool.stale && tool.epoch == currentEpoch) return true;
        return false;
    }

    synchronized boolean beginTool(String callId, long epoch, long now) {
        if (ended || callId == null || callId.isEmpty() || tools.containsKey(callId)
                || epoch != currentEpoch || now >= graceDeadline) return false;
        tools.put(callId, new Tool(callId, epoch));
        return true;
    }

    Delivery deliverResult(String callId, long epoch, long epochNow,
            boolean explicitlyCancelled, Sender sender) {
        Tool tool;
        boolean stale;
        synchronized (this) {
            tool = tools.get(callId);
            if (ended || explicitlyCancelled || tool == null) {
                remove(tool);
                return Delivery.SUPPRESSED;
            }
            stale = tool.stale || tool.epoch != epoch || epoch != epochNow;
            if (!stale) awaitingFollowup.addLast(tool);
        }
        // Transport callbacks may fail and close the connection. Hold no session
        // lock here; lifecycle changes are reconciled against the Tool identity below.
        boolean sent = sender.send(stale);
        synchronized (this) {
            if (ended || tools.get(callId) != tool) return Delivery.SUPPRESSED;
            if (!sent) {
                remove(tool);
                return stale ? Delivery.SUPPRESSED : Delivery.FAILED;
            }
            if (stale) {
                remove(tool);
                return Delivery.SENT_STALE;
            }
            return Delivery.SENT;
        }
    }

    void cancelTool(String callId) {
        synchronized (this) { remove(tools.get(callId)); }
    }

    void interrupt(long epoch) {
        synchronized (this) {
            currentEpoch = epoch;
            for (Tool tool : new ArrayList<>(tools.values())) {
                if (tool.epoch != epoch) {
                    tool.stale = true;
                    responses.remove(tool.responseId);
                    tool.responseId = null;
                    awaitingFollowup.remove(tool);
                }
            }
        }
    }

    synchronized void followupStarted(String responseId) {
        if (ended || responseId == null || responseId.isEmpty()) return;
        while (!awaitingFollowup.isEmpty()) {
            Tool tool = awaitingFollowup.removeFirst();
            if (!tool.stale && tools.get(tool.callId) == tool) {
                tool.responseId = responseId;
                responses.put(responseId, tool);
                return;
            }
        }
    }

    synchronized void audioStarted(String responseId) {
        Tool tool = responses.get(responseId);
        if (!ended && tool != null) tool.audioStarted = true;
    }

    synchronized void responseDone(String responseId) {
        Tool tool = responses.get(responseId);
        if (ended || tool == null) return;
        tool.responseDone = true;
        if (!tool.audioStarted || tool.audioPlayed) remove(tool);
    }

    synchronized void audioPlayed(String responseId) {
        Tool tool = responses.get(responseId);
        if (ended || tool == null) return;
        tool.audioPlayed = true;
        if (tool.responseDone) remove(tool);
    }

    void end() {
        synchronized (this) {
            ended = true;
            tools.clear(); responses.clear(); awaitingFollowup.clear();
        }
    }

    private void remove(Tool tool) {
        if (tool == null) return;
        tools.remove(tool.callId, tool);
        awaitingFollowup.remove(tool);
        if (tool.responseId != null) responses.remove(tool.responseId, tool);
    }
}
