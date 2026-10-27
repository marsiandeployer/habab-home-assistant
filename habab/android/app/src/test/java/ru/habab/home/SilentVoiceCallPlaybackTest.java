package ru.habab.home;

import org.junit.Test;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import static org.junit.Assert.*;

public class SilentVoiceCallPlaybackTest {
    @Test public void restartsDeadTrackWithSilenceAndClosesOnCaptureExit() throws Exception {
        AtomicInteger created = new AtomicInteger();
        AtomicInteger active = new AtomicInteger();
        AtomicInteger maxActive = new AtomicInteger();
        AtomicBoolean nonZeroPcm = new AtomicBoolean();
        CountDownLatch secondWrite = new CountDownLatch(1);
        CountDownLatch secondClosed = new CountDownLatch(1);

        SilentVoiceCallPlayback playback = new SilentVoiceCallPlayback(() -> {
            int id = created.incrementAndGet();
            return new SilentVoiceCallPlayback.Track() {
                private final AtomicBoolean closed = new AtomicBoolean();
                @Override public void play() {
                    int count = active.incrementAndGet();
                    maxActive.accumulateAndGet(count, Math::max);
                }
                @Override public int write(short[] silence) {
                    if (silence.length != SilentVoiceCallPlayback.SILENCE_SAMPLES) nonZeroPcm.set(true);
                    for (short sample : silence) if (sample != 0) nonZeroPcm.set(true);
                    if (id == 1) return -6;
                    secondWrite.countDown();
                    try { secondClosed.await(); }
                    catch (InterruptedException interrupted) { Thread.currentThread().interrupt(); }
                    return -6;
                }
                @Override public int playbackState() { return 3; }
                @Override public int routedDeviceType() { return 7; }
                @Override public void close() {
                    if (closed.compareAndSet(false, true)) {
                        active.decrementAndGet();
                        if (id == 2) secondClosed.countDown();
                    }
                }
            };
        });

        try {
            assertTrue("replacement track should open", secondWrite.await(2, TimeUnit.SECONDS));
            assertTrue("dead track should be replaced", created.get() >= 2);
            assertEquals("tracks must not overlap", 1, maxActive.get());
            assertFalse("playback may contain zero samples only", nonZeroPcm.get());
        } finally {
            playback.close();
        }
        assertTrue("replacement track should close", secondClosed.await(1, TimeUnit.SECONDS));
        assertEquals(0, active.get());
    }
}
