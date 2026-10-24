package ru.habab.home;

/** A tool result may arrive before its spoken preamble finishes generating. */
final class VoiceResponseGate implements AutoCloseable {
    private String active;
    private boolean awaiting, closed;
    private Runnable pending;

    synchronized void created(String id) { if (!closed) { active = id; awaiting = false; } }

    synchronized String interrupted() {
        String previous = active;
        active = null; pending = null; awaiting = !closed;
        return previous;
    }

    synchronized Runnable request(Runnable response) {
        if (closed) return null;
        if (active != null || awaiting) { pending = response; return null; }
        awaiting = true; return response;
    }

    synchronized Runnable done(String id) {
        if (closed || active == null || !active.equals(id)) return null;
        active = null;
        Runnable response = pending; pending = null;
        if (response != null) awaiting = true;
        return response;
    }

    @Override public synchronized void close() { closed = true; pending = null; active = null; }
}
