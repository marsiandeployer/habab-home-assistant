package ru.habab.home;

import static org.junit.Assert.*;
import org.junit.Test;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

public class VoiceToolSessionTest {
    private static final long START = 10_000;

    @Test public void deliveredFunctionResultWaitsForItsPlayedGeminiFollowupBeforeEnd() {
        VoiceToolSession session = new VoiceToolSession(START);
        session.interrupt(1);
        assertTrue(session.beginTool("call-1", 1, START + 1000));
        long audioDeadline = START + VoiceToolSession.AUDIO_LIMIT_MS;

        assertEquals(VoiceToolSession.DeadlineAction.DRAIN_RESULT, session.deadlineAction(audioDeadline + 1));
        final boolean[] silent = {true};
        VoiceToolSession.Delivery delivery = session.deliverResult("call-1", 1, 1, false, isSilent -> {
            silent[0] = isSilent;
            return true;
        });
        assertEquals(VoiceToolSession.Delivery.SENT, delivery);
        assertFalse(silent[0]);

        session.followupStarted("response-1");
        session.audioStarted("response-1");
        session.responseDone("response-1");
        assertEquals(VoiceToolSession.DeadlineAction.DRAIN_RESULT, session.deadlineAction(audioDeadline + 2));

        session.audioPlayed("response-1");
        assertEquals(VoiceToolSession.DeadlineAction.FINISH, session.deadlineAction(audioDeadline + 3));
    }

    @Test public void activeToolGraceHasAnAbsoluteMonotonicDeadline() {
        VoiceToolSession session = new VoiceToolSession(START);
        session.interrupt(1);
        assertTrue(session.beginTool("call-1", 1, START + 1000));
        long audioDeadline = START + VoiceToolSession.AUDIO_LIMIT_MS;
        long graceDeadline = audioDeadline + VoiceToolSession.RESULT_GRACE_MS;

        assertEquals(VoiceToolSession.DeadlineAction.DRAIN_RESULT, session.deadlineAction(audioDeadline + 1));
        assertFalse(session.graceExpired(graceDeadline - 1));
        assertEquals(VoiceToolSession.DeadlineAction.DRAIN_RESULT, session.deadlineAction(graceDeadline - 1));
        assertTrue(session.graceExpired(graceDeadline));
        assertEquals(VoiceToolSession.DeadlineAction.TIMEOUT, session.deadlineAction(graceDeadline));
    }

    @Test public void explicitToolCancellationSuppressesLateResultCallback() {
        VoiceToolSession session = new VoiceToolSession(START);
        session.interrupt(1);
        assertTrue(session.beginTool("call-1", 1, START + 1000));
        session.cancelTool("call-1");
        final boolean[] sent = {false};

        VoiceToolSession.Delivery delivery = session.deliverResult("call-1", 1, 1, true, silent -> {
            sent[0] = true;
            return true;
        });

        assertEquals(VoiceToolSession.Delivery.SUPPRESSED, delivery);
        assertFalse(sent[0]);
        assertEquals(VoiceToolSession.DeadlineAction.FINISH, session.deadlineAction(START + VoiceToolSession.AUDIO_LIMIT_MS));
    }

    @Test public void explicitSessionEndPreventsLateCallbackFromResurrectingFlow() {
        VoiceToolSession session = new VoiceToolSession(START);
        session.interrupt(1);
        assertTrue(session.beginTool("call-1", 1, START + 1000));
        session.end();
        final boolean[] sent = {false};

        VoiceToolSession.Delivery delivery = session.deliverResult("call-1", 1, 1, false, silent -> {
            sent[0] = true;
            return true;
        });

        assertEquals(VoiceToolSession.Delivery.SUPPRESSED, delivery);
        assertFalse(sent[0]);
        assertEquals(VoiceToolSession.DeadlineAction.FINISH, session.deadlineAction(START + 1000));
    }

    @Test public void staleEpochResultCanOnlyBeSentSilentAndDoesNotHoldGrace() {
        VoiceToolSession session = new VoiceToolSession(START);
        session.interrupt(1);
        assertTrue(session.beginTool("call-1", 1, START + 1000));
        session.interrupt(2);
        final boolean[] silent = {false};

        VoiceToolSession.Delivery delivery = session.deliverResult("call-1", 1, 2, false, isSilent -> {
            silent[0] = isSilent;
            return true;
        });

        assertEquals(VoiceToolSession.Delivery.SENT_STALE, delivery);
        assertTrue(silent[0]);
        assertEquals(VoiceToolSession.DeadlineAction.FINISH, session.deadlineAction(START + VoiceToolSession.AUDIO_LIMIT_MS));
    }

    @Test public void staleResultSendFailureIsSuppressedRatherThanReportedAsSent() {
        VoiceToolSession session = new VoiceToolSession(START);
        session.interrupt(1);
        assertTrue(session.beginTool("call-1", 1, START + 1000));
        session.interrupt(2);

        VoiceToolSession.Delivery delivery = session.deliverResult("call-1", 1, 2, false, silent -> {
            assertTrue(silent);
            return false;
        });

        assertEquals(VoiceToolSession.Delivery.SUPPRESSED, delivery);
    }

    @Test public void callbackEndingSessionSuppressesFlowWithoutResurrection() {
        VoiceToolSession session = new VoiceToolSession(START);
        session.interrupt(1);
        assertTrue(session.beginTool("call-1", 1, START + 1000));

        VoiceToolSession.Delivery delivery = session.deliverResult("call-1", 1, 1, false, silent -> {
            session.end();
            return true;
        });

        assertEquals(VoiceToolSession.Delivery.SUPPRESSED, delivery);
        assertEquals(VoiceToolSession.DeadlineAction.FINISH, session.deadlineAction(START + 1000));
    }

    @Test public void endingSessionDoesNotWaitForTransportCallback() throws Exception {
        VoiceToolSession session = new VoiceToolSession(START);
        session.interrupt(1);
        assertTrue(session.beginTool("call-1", 1, START + 1000));
        CountDownLatch senderEntered = new CountDownLatch(1), releaseSender = new CountDownLatch(1);
        CountDownLatch endCompleted = new CountDownLatch(1);
        AtomicReference<VoiceToolSession.Delivery> delivery = new AtomicReference<>();
        Thread sender = new Thread(() -> delivery.set(session.deliverResult("call-1", 1, 1, false, silent -> {
            senderEntered.countDown(); await(releaseSender); return true;
        })), "voice-tool-sender-test");
        Thread end = new Thread(() -> { session.end(); endCompleted.countDown(); }, "voice-tool-end-test");
        sender.setDaemon(true); end.setDaemon(true);
        sender.start();
        assertTrue(senderEntered.await(1, TimeUnit.SECONDS));
        end.start();
        boolean endedWhileSenderWasBlocked = endCompleted.await(1, TimeUnit.SECONDS);
        releaseSender.countDown();
        sender.join(1000); end.join(1000);

        assertTrue("session end must not wait behind transport callback", endedWhileSenderWasBlocked);
        assertEquals(VoiceToolSession.Delivery.SUPPRESSED, delivery.get());
    }

    @Test public void senderFailureAndHistoryDrainCannotDeadlockEachOther() throws Exception {
        VoiceToolSession session = new VoiceToolSession(START);
        session.interrupt(1);
        assertTrue(session.beginTool("call-1", 1, START + 1000));
        Object historyLock = new Object();
        CountDownLatch senderEntered = new CountDownLatch(1), finishHoldsHistory = new CountDownLatch(1);
        CountDownLatch finishCompleted = new CountDownLatch(1);
        AtomicReference<VoiceToolSession.Delivery> delivery = new AtomicReference<>();
        Thread sender = new Thread(() -> delivery.set(session.deliverResult("call-1", 1, 1, false, silent -> {
            senderEntered.countDown(); await(finishHoldsHistory);
            synchronized (historyLock) { return false; }
        })), "voice-tool-failure-test");
        Thread finish = new Thread(() -> {
            synchronized (historyLock) {
                finishHoldsHistory.countDown(); session.end(); finishCompleted.countDown();
            }
        }, "voice-history-drain-test");
        sender.setDaemon(true); finish.setDaemon(true);
        sender.start();
        assertTrue(senderEntered.await(1, TimeUnit.SECONDS));
        finish.start();
        boolean historyDrained = finishCompleted.await(1, TimeUnit.SECONDS);
        finish.join(1000); sender.join(1000);

        assertTrue("history drain must finish while failed sender waits for its lock", historyDrained);
        assertEquals(VoiceToolSession.Delivery.SUPPRESSED, delivery.get());
    }

    @Test public void currentUncancelledResultSendFailureIsReportedToCaller() {
        VoiceToolSession session = new VoiceToolSession(START);
        session.interrupt(1);
        assertTrue(session.beginTool("call-1", 1, START + 1000));

        VoiceToolSession.Delivery delivery = session.deliverResult("call-1", 1, 1, false, silent -> false);

        assertEquals(VoiceToolSession.Delivery.FAILED, delivery);
        assertEquals(VoiceToolSession.DeadlineAction.FINISH, session.deadlineAction(START + VoiceToolSession.AUDIO_LIMIT_MS));
    }

    private static void await(CountDownLatch latch) {
        try { latch.await(2, TimeUnit.SECONDS); }
        catch (InterruptedException error) { Thread.currentThread().interrupt(); }
    }
}
