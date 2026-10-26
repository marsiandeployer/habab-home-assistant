package ru.habab.home;

import java.util.concurrent.CompletableFuture;

/** The next service may start only after the previous capture and teardown finish. */
final class VoiceLifecycleDrain {
    static final class Start {
        final long epoch;
        final CompletableFuture<Void> drained;
        private Start(long epoch, CompletableFuture<Void> drained) {
            this.epoch = epoch; this.drained = drained;
        }
    }

    private CompletableFuture<Void> drained = CompletableFuture.completedFuture(null);
    private long generation, epoch;
    private Start pending;

    synchronized long opened(CompletableFuture<Void> captureCleaned, CompletableFuture<Void> serviceCleaned) {
        drained = CompletableFuture.allOf(drained, captureCleaned, serviceCleaned);
        return ++generation;
    }

    synchronized Start request() {
        if (pending != null) return null;
        pending = new Start(epoch, drained);
        return pending;
    }

    synchronized boolean claim(Start request) {
        if (pending != request || request.epoch != epoch) return false;
        pending = null;
        return true;
    }

    synchronized void cancel() { epoch++; pending = null; }
    synchronized long generation() { return generation; }
}
