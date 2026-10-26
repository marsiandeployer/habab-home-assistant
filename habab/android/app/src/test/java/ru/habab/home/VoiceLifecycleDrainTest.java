package ru.habab.home;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.Test;
import static org.junit.Assert.*;

public class VoiceLifecycleDrainTest {
    @Test public void replacementWaitsForCaptureAndLateEndRouteCleanup() {
        VoiceLifecycleDrain lifecycle = new VoiceLifecycleDrain();
        CompletableFuture<Void> capture = new CompletableFuture<>(), released = new CompletableFuture<>();
        lifecycle.opened(capture, released);
        List<String> actions = new ArrayList<>();
        VoiceLifecycleDrain.Start request = lifecycle.request();
        request.drained.thenRun(() -> { if (lifecycle.claim(request)) actions.add("new_create"); });
        capture.complete(null);
        assertFalse(request.drained.isDone()); assertTrue(actions.isEmpty());
        actions.add("old_end_focus_route_released");
        released.complete(null);
        assertEquals(java.util.Arrays.asList("old_end_focus_route_released", "new_create"), actions);
    }

    @Test public void twoQueuedModeChangesStartOnlyTheLatestRequest() {
        VoiceLifecycleDrain lifecycle = new VoiceLifecycleDrain();
        CompletableFuture<Void> capture = new CompletableFuture<>(), released = new CompletableFuture<>();
        lifecycle.opened(capture, released);
        List<String> started = new ArrayList<>();
        lifecycle.cancel();
        VoiceLifecycleDrain.Start duplicate = lifecycle.request();
        duplicate.drained.thenRun(() -> { if (lifecycle.claim(duplicate)) started.add("duplicate"); });
        assertNull(lifecycle.request()); // Automatic refresh cannot queue another copy.
        lifecycle.cancel();
        VoiceLifecycleDrain.Start phone = lifecycle.request();
        phone.drained.thenRun(() -> { if (lifecycle.claim(phone)) started.add("phone"); });
        released.complete(null); assertTrue(started.isEmpty());
        capture.complete(null);
        assertEquals(java.util.Collections.singletonList("phone"), started);
        assertFalse(lifecycle.claim(duplicate)); assertFalse(lifecycle.claim(phone));
    }

    @Test public void successiveCompletedSwitchesEachReceiveTheirOwnDrain() {
        VoiceLifecycleDrain lifecycle = new VoiceLifecycleDrain();
        AtomicInteger starts = new AtomicInteger();
        for (int i = 0; i < 2; i++) {
            CompletableFuture<Void> capture = new CompletableFuture<>(), released = new CompletableFuture<>();
            assertEquals(i + 1L, lifecycle.opened(capture, released));
            lifecycle.cancel();
            VoiceLifecycleDrain.Start request = lifecycle.request();
            request.drained.thenRun(() -> { if (lifecycle.claim(request)) starts.incrementAndGet(); });
            capture.complete(null); assertEquals(i, starts.get());
            released.complete(null); assertEquals(i + 1, starts.get());
        }
    }

    @Test public void explicitStopCancelsAQueuedRestart() {
        VoiceLifecycleDrain lifecycle = new VoiceLifecycleDrain();
        CompletableFuture<Void> capture = new CompletableFuture<>(), released = new CompletableFuture<>();
        lifecycle.opened(capture, released);
        AtomicInteger starts = new AtomicInteger();
        VoiceLifecycleDrain.Start request = lifecycle.request();
        request.drained.thenRun(() -> { if (lifecycle.claim(request)) starts.incrementAndGet(); });
        lifecycle.cancel();
        capture.complete(null); released.complete(null);
        assertEquals(0, starts.get()); assertFalse(lifecycle.claim(request));
    }

    @Test public void failedCleanupCannotReleaseASuccessfulStart() {
        VoiceLifecycleDrain lifecycle = new VoiceLifecycleDrain();
        CompletableFuture<Void> capture = new CompletableFuture<>(), released = new CompletableFuture<>();
        lifecycle.opened(capture, released);
        VoiceLifecycleDrain.Start request = lifecycle.request();
        AtomicInteger starts = new AtomicInteger();
        request.drained.thenRun(starts::incrementAndGet);
        capture.completeExceptionally(new IllegalStateException("cleanup"));
        released.complete(null);
        assertThrows(CompletionException.class, request.drained::join);
        assertEquals(0, starts.get());
    }
}
