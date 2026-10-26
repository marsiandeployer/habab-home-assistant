package ru.habab.home;

import org.junit.Test;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.atomic.AtomicInteger;
import static org.junit.Assert.*;

public class VoiceSessionSignalsTest {
    @Test public void failedOpeningKeepsAudioClosedAndNextWakeCanOpenANewSession() {
        VoiceSessionSignals signals = new VoiceSessionSignals();
        WakeMatchGate wake = new WakeMatchGate("эй дом");
        assertTrue(wake.accept("эй дом", WakeMatchGate.ResultKind.ENDPOINT));
        wake.reset(); // WakeDetector resets immediately after an accepted wake.
        assertEquals(VoiceSessionSignals.Cue.START, signals.begin(1));
        assertFalse(signals.startCompleted(1, false));
        assertFalse(signals.allowsAudio());
        assertEquals(VoiceSessionSignals.Cue.ERROR, signals.end(VoiceSessionSignals.EndReason.ERROR));
        wake.reset(); // VoiceService's early-cue-failure cleanup resets the detector again.
        assertTrue(wake.accept("эй дом", WakeMatchGate.ResultKind.VAD_END));
        assertEquals(VoiceSessionSignals.Cue.START, signals.begin(3));
        assertFalse(signals.startCompleted(1, true));
        assertFalse(signals.allowsAudio());
        assertTrue(signals.startCompleted(3, true));
        assertTrue(signals.allowsAudio());
    }

    @Test public void anIdleServiceDoesNotAnnounceAConversationEnding() {
        VoiceSessionSignals signals = new VoiceSessionSignals();
        assertEquals(VoiceSessionSignals.Cue.NONE, signals.end(VoiceSessionSignals.EndReason.STOP));
    }

    @Test public void aConversationHasDistinctOpeningAndClosingCues() {
        VoiceSessionSignals signals = new VoiceSessionSignals();
        assertEquals(VoiceSessionSignals.Cue.START, signals.begin(1));
        assertEquals(VoiceSessionSignals.Cue.NONE, signals.begin(1));
        assertEquals(VoiceSessionSignals.Cue.END, signals.end(VoiceSessionSignals.EndReason.NORMAL));
        assertEquals(VoiceSessionSignals.Cue.NONE, signals.end(VoiceSessionSignals.EndReason.STOP));
    }

    @Test public void aFailureReplacesTheClosingCueWithoutASecondSignal() {
        VoiceSessionSignals signals = new VoiceSessionSignals();
        signals.begin(1);
        assertEquals(VoiceSessionSignals.Cue.ERROR, signals.end(VoiceSessionSignals.EndReason.ERROR));
        assertEquals(VoiceSessionSignals.Cue.NONE, signals.end(VoiceSessionSignals.EndReason.STOP));
    }

    @Test public void theNextConversationGetsItsOwnPairOfCues() {
        VoiceSessionSignals signals = new VoiceSessionSignals();
        for (int i = 0; i < 3; i++) {
            assertEquals(VoiceSessionSignals.Cue.START, signals.begin(i + 1));
            assertEquals(VoiceSessionSignals.Cue.END, signals.end(VoiceSessionSignals.EndReason.STOP));
        }
    }

    @Test public void serviceDestructionAndCaptureCleanupCannotPlayTwoClosingCues() throws Exception {
        VoiceSessionSignals signals = new VoiceSessionSignals();
        signals.begin(1);
        CountDownLatch start = new CountDownLatch(1);
        AtomicInteger audible = new AtomicInteger();
        Runnable finish = () -> {
            try { start.await(); } catch (InterruptedException error) { throw new AssertionError(error); }
            if (signals.end(VoiceSessionSignals.EndReason.STOP) == VoiceSessionSignals.Cue.END) audible.incrementAndGet();
        };
        Thread first = new Thread(finish), second = new Thread(finish);
        first.start(); second.start(); start.countDown(); first.join(); second.join();
        assertEquals(1, audible.get());
    }

    @Test public void audioWaitsForAnActuallyCompletedStartCue() {
        VoiceSessionSignals signals = new VoiceSessionSignals();
        assertFalse(signals.allowsAudio()); signals.begin(1); assertFalse(signals.allowsAudio());
        assertFalse(signals.startCompleted(1, false)); assertFalse(signals.allowsAudio());
        assertTrue(signals.startCompleted(1, true)); assertTrue(signals.allowsAudio());
        signals.end(VoiceSessionSignals.EndReason.NORMAL); assertFalse(signals.allowsAudio());
    }

    @Test public void aLateCueCompletionCannotReopenAClosedOrDifferentSession() {
        VoiceSessionSignals signals = new VoiceSessionSignals(); signals.begin(1);
        signals.end(VoiceSessionSignals.EndReason.STOP);
        assertFalse(signals.startCompleted(1, true)); assertFalse(signals.allowsAudio());
        signals.begin(3); assertFalse(signals.startCompleted(1, true)); assertFalse(signals.allowsAudio());
        assertTrue(signals.startCompleted(3, true)); assertTrue(signals.allowsAudio());
    }
}
