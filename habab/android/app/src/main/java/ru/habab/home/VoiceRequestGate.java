package ru.habab.home;

import java.util.HashSet;
import java.util.Set;

/** Registers a request before execution so closing also cancels a not-yet-started call. */
final class VoiceRequestGate implements AutoCloseable {
    private boolean closed;
    private final Set<Ticket> active = new HashSet<>();

    final class Ticket implements AutoCloseable {
        private final Runnable cancel;
        private Ticket(Runnable cancel) { this.cancel = cancel; }
        @Override public void close() { synchronized (VoiceRequestGate.this) { active.remove(this); } }
    }

    synchronized Ticket register(Runnable cancel) {
        if (closed) { cancel.run(); return null; }
        Ticket ticket = new Ticket(cancel); active.add(ticket); return ticket;
    }

    @Override public void close() {
        Set<Ticket> pending;
        synchronized (this) {
            if (closed) return;
            closed = true; pending = new HashSet<>(active); active.clear();
        }
        for (Ticket ticket : pending) ticket.cancel.run();
    }
}
