package ru.habab.home;

/** One audible opening and one closing cue per conversation, including concurrent cleanup. */
final class VoiceSessionSignals {
    enum Cue { NONE, START, END, ERROR }
    enum EndReason { NORMAL, STOP, ERROR }
    private boolean open;
    private boolean startCompleted;
    private long session;

    synchronized Cue begin(long session) {
        if (open || session <= this.session) return Cue.NONE;
        open = true; startCompleted = false; this.session = session;
        return Cue.START;
    }

    synchronized Cue end(EndReason reason) {
        if (!open) return Cue.NONE;
        open = false; startCompleted = false;
        return reason == EndReason.ERROR ? Cue.ERROR : Cue.END;
    }

    synchronized boolean startCompleted(long session, boolean audible) {
        if (!open || this.session != session) return false;
        startCompleted = audible; return audible;
    }

    synchronized boolean allowsAudio() { return open && startCompleted; }
}
