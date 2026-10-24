package ru.habab.home;

import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.Test;
import static org.junit.Assert.*;

public class VoiceHistoryQueueTest {
    @Test public void callbackAlreadyInProgressIsAcceptedBeforeConcurrentFinish() throws Exception {
        VoiceHistoryQueue queue = new VoiceHistoryQueue();
        CountDownLatch entered = new CountDownLatch(1), release = new CountDownLatch(1),
                finishing = new CountDownLatch(1), ended = new CountDownLatch(1);
        AtomicInteger saved = new AtomicInteger(), atEnd = new AtomicInteger(-1), failed = new AtomicInteger();
        AtomicReference<Thread> finisher = new AtomicReference<>();
        ExecutorService threads = Executors.newFixedThreadPool(2);
        try {
            Future<?> callback = threads.submit(() -> {
                try { queue.dispatch(() -> { entered.countDown(); release.await(); queue.submit(saved::incrementAndGet); }); }
                catch (Exception error) { throw new RuntimeException(error); }
            });
            assertTrue(entered.await(1, TimeUnit.SECONDS));
            Future<?> finish = threads.submit(() -> {
                finisher.set(Thread.currentThread());
                finishing.countDown();
                queue.finish(() -> { atEnd.set(saved.get()); ended.countDown(); }, failed::incrementAndGet);
            });
            assertTrue(finishing.await(1, TimeUnit.SECONDS));
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(1);
            while (finisher.get().getState() != Thread.State.BLOCKED && System.nanoTime() < deadline) Thread.sleep(1);
            assertEquals(Thread.State.BLOCKED, finisher.get().getState());
            assertEquals(1, ended.getCount()); release.countDown();
            callback.get(1, TimeUnit.SECONDS); finish.get(1, TimeUnit.SECONDS);
            assertTrue(ended.await(1, TimeUnit.SECONDS)); assertEquals(1, atEnd.get()); assertEquals(0, failed.get());
            queue.dispatch(failed::incrementAndGet); assertEquals(0, failed.get());
        } finally { release.countDown(); queue.close(); threads.shutdownNow(); }
    }

    @Test public void goodbyeDrainsAlreadyAcceptedWritesBeforeEnd() throws Exception {
        VoiceHistoryQueue queue = new VoiceHistoryQueue();
        CountDownLatch writing = new CountDownLatch(1), release = new CountDownLatch(1), ended = new CountDownLatch(1);
        AtomicInteger saved = new AtomicInteger(), atEnd = new AtomicInteger(-1), failed = new AtomicInteger();
        try {
            queue.submit(() -> { writing.countDown(); try { release.await(); saved.incrementAndGet(); }
                catch (InterruptedException e) { Thread.currentThread().interrupt(); } });
            assertTrue(writing.await(1, TimeUnit.SECONDS)); queue.submit(saved::incrementAndGet);
            queue.finish(() -> { atEnd.set(saved.get()); ended.countDown(); }, failed::incrementAndGet);
            assertFalse(ended.await(30, TimeUnit.MILLISECONDS)); release.countDown();
            assertTrue(ended.await(1, TimeUnit.SECONDS)); assertEquals(2, atEnd.get()); assertEquals(0, failed.get());
            try { queue.submit(saved::incrementAndGet); fail("No writes after END"); }
            catch (RejectedExecutionException expected) {}
        } finally { release.countDown(); queue.close(); }
    }

    @Test public void stalledJournalFailsOnceRatherThanAnnouncingSavedConversation() throws Exception {
        VoiceHistoryQueue queue = new VoiceHistoryQueue(100);
        CountDownLatch writing = new CountDownLatch(1), release = new CountDownLatch(1), ended = new CountDownLatch(1);
        AtomicInteger success = new AtomicInteger(), failed = new AtomicInteger();
        try {
            queue.submit(() -> { writing.countDown(); try { release.await(); }
                catch (InterruptedException e) { Thread.currentThread().interrupt(); } });
            assertTrue(writing.await(1, TimeUnit.SECONDS));
            queue.finish(success::incrementAndGet, () -> { failed.incrementAndGet(); ended.countDown(); });
            assertTrue(ended.await(1, TimeUnit.SECONDS)); release.countDown();
            queue.finish(success::incrementAndGet, failed::incrementAndGet);
            assertEquals(0, success.get()); assertEquals(1, failed.get());
        } finally { release.countDown(); queue.close(); }
    }

    @Test public void emergencyCloseRejectsLateWritesAndCallbacks() {
        VoiceHistoryQueue queue = new VoiceHistoryQueue(); AtomicInteger callbacks = new AtomicInteger();
        queue.close(); queue.finish(callbacks::incrementAndGet, callbacks::incrementAndGet);
        try { queue.submit(callbacks::incrementAndGet); fail("Closed queue"); }
        catch (RejectedExecutionException expected) {}
        assertEquals(0, callbacks.get());
    }
}
