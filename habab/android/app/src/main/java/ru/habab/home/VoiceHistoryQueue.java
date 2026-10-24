package ru.habab.home;

import java.util.concurrent.*;

/** Normal END waits for accepted fast journal writes; a stalled journal is an explicit error. */
final class VoiceHistoryQueue implements AutoCloseable {
    interface Event { void accept() throws Exception; }
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final ScheduledExecutorService timer = Executors.newSingleThreadScheduledExecutor();
    private final long timeoutMillis;
    private boolean accepting = true, done;
    private int pending;

    VoiceHistoryQueue() { this(1500); }
    VoiceHistoryQueue(long timeoutMillis) { this.timeoutMillis = timeoutMillis; }

    synchronized void dispatch(Event event) throws Exception {
        if (accepting && !done) event.accept();
    }

    synchronized void submit(Runnable write) {
        if (!accepting || done || pending >= 64) throw new RejectedExecutionException();
        pending++;
        worker.execute(() -> { try { write.run(); } finally { synchronized (this) { pending--; } } });
    }

    synchronized void finish(Runnable completed, Runnable failed) {
        if (!accepting || done) return;
        accepting = false;
        timer.schedule(() -> complete(failed, true), timeoutMillis, TimeUnit.MILLISECONDS);
        worker.execute(() -> complete(completed, false));
    }

    private void complete(Runnable callback, boolean cancel) {
        synchronized (this) { if (done) return; done = true; accepting = false; }
        timer.shutdownNow();
        if (cancel) worker.shutdownNow(); else worker.shutdown();
        callback.run();
    }

    @Override public void close() {
        synchronized (this) { done = true; accepting = false; }
        worker.shutdownNow(); timer.shutdownNow();
    }
}
