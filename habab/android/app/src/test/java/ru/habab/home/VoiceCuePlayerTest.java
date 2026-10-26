package ru.habab.home;

import android.media.AudioTrack;
import org.junit.Test;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;
import static org.junit.Assert.*;

public class VoiceCuePlayerTest {
    @Test public void staticTrackWithoutPcmIsValidUntilWriteCompletes() {
        VoiceCuePlayer.requireTrackState(AudioTrack.STATE_NO_STATIC_DATA, false);
        VoiceCuePlayer.requireTrackState(AudioTrack.STATE_INITIALIZED, false);
        VoiceCuePlayer.requireTrackState(AudioTrack.STATE_INITIALIZED, true);
        assertThrows(IllegalStateException.class,
                () -> VoiceCuePlayer.requireTrackState(AudioTrack.STATE_NO_STATIC_DATA, true));
        assertThrows(IllegalStateException.class,
                () -> VoiceCuePlayer.requireTrackState(AudioTrack.STATE_UNINITIALIZED, false));
        assertThrows(IllegalStateException.class,
                () -> VoiceCuePlayer.requireTrackState(AudioTrack.STATE_UNINITIALIZED, true));
    }

    @Test public void errorCleanupPreservesTheOpeningFailureSnapshot() throws Exception {
        AtomicInteger attempts = new AtomicInteger();
        VoiceCuePlayer player = new VoiceCuePlayer(pcm -> {
            if (attempts.getAndIncrement() == 0) throw new IllegalStateException("cue_unavailable");
            return new VoiceCuePlayer.Sink() {
                public void play() {} public long played() { return pcm.length; } public void close() {}
            };
        }, () -> true);
        try {
            assertFalse(player.play(VoiceSessionSignals.Cue.START).get(1, TimeUnit.SECONDS));
            java.util.Map<String, Object> opening = player.startDiagnostics();
            assertEquals("IllegalStateException", opening.get("failure"));
            assertThrows(UnsupportedOperationException.class, () -> opening.put("stage", "changed"));
            assertTrue(player.play(VoiceSessionSignals.Cue.ERROR).get(1, TimeUnit.SECONDS));
            assertEquals("complete", player.diagnostics().get("stage"));
            assertSame(opening, player.startDiagnostics());
            assertTrue(player.play(VoiceSessionSignals.Cue.START).get(1, TimeUnit.SECONDS));
            assertEquals("complete", player.startDiagnostics().get("stage"));
            assertEquals("none", player.startDiagnostics().get("failure"));
        } finally { player.close(); }
    }

    @Test public void startIsNotCompleteUntilEveryFrameWasPlayedAndReleased() throws Exception {
        AtomicInteger head = new AtomicInteger(), released = new AtomicInteger();
        VoiceCuePlayer player = new VoiceCuePlayer(pcm -> new VoiceCuePlayer.Sink() {
            public void play() {} public long played() { return head.get(); } public void close() { released.incrementAndGet(); }
        }, () -> true);
        try {
            CompletableFuture<Boolean> cue = player.play(VoiceSessionSignals.Cue.START);
            Thread.sleep(30); assertFalse(cue.isDone());
            head.set(VoiceCuePcm.create(VoiceSessionSignals.Cue.START).length);
            assertTrue(cue.get(1, TimeUnit.SECONDS)); assertEquals(1, released.get());
            assertEquals("complete", player.diagnostics().get("stage"));
            assertEquals((long) VoiceCuePcm.create(VoiceSessionSignals.Cue.START).length,
                    player.diagnostics().get("playedFrames"));
        } finally { player.close(); }
    }

    @Test public void mutedMediaDoesNotPretendThatTheStartCuePlayed() throws Exception {
        AtomicInteger allocated = new AtomicInteger();
        VoiceCuePlayer player = new VoiceCuePlayer(pcm -> { allocated.incrementAndGet(); throw new AssertionError(); }, () -> false);
        try {
            assertFalse(player.play(VoiceSessionSignals.Cue.START).get(1, TimeUnit.SECONDS)); assertEquals(0, allocated.get());
            assertEquals("audibility", player.diagnostics().get("stage"));
            assertEquals("not_audible", player.diagnostics().get("failure"));
        }
        finally { player.close(); }
    }

    @Test public void stoppingTheServiceDrainsTheFinalCueAndRejectsNewCues() throws Exception {
        AtomicInteger released = new AtomicInteger();
        VoiceCuePlayer player = new VoiceCuePlayer(pcm -> new VoiceCuePlayer.Sink() {
            public void play() {} public long played() { return pcm.length; } public void close() { released.incrementAndGet(); }
        }, () -> true);
        CompletableFuture<Boolean> end = player.play(VoiceSessionSignals.Cue.END); player.close();
        assertTrue(end.get(1, TimeUnit.SECONDS)); assertEquals(1, released.get());
        assertFalse(player.play(VoiceSessionSignals.Cue.START).get(1, TimeUnit.SECONDS));
    }

    @Test public void aStalledOutputFailsClosedAndStillReleasesItsTrack() throws Exception {
        AtomicInteger released = new AtomicInteger();
        VoiceCuePlayer player = new VoiceCuePlayer(pcm -> new VoiceCuePlayer.Sink() {
            public void play() {} public long played() { return 0; } public void close() { released.incrementAndGet(); }
        }, () -> true);
        try {
            assertFalse(player.play(VoiceSessionSignals.Cue.START).get(3, TimeUnit.SECONDS)); assertEquals(1, released.get());
            assertEquals("drain", player.diagnostics().get("stage"));
            assertEquals("drain_timeout", player.diagnostics().get("failure"));
            assertEquals(0L, player.diagnostics().get("playedFrames"));
        }
        finally { player.close(); }
    }

    @Test public void cuesAreAudibleBoundedPcmWithDifferentEndSignature() {
        short[] start = VoiceCuePcm.create(VoiceSessionSignals.Cue.START), end = VoiceCuePcm.create(VoiceSessionSignals.Cue.END);
        assertEquals(2880, start.length); assertEquals(5280, end.length);
        long energy = 0; for (short sample : start) { assertTrue(Math.abs((int) sample) <= 8192); energy += Math.abs((int) sample); }
        assertTrue(energy > 100000); assertEquals(0, start[0]); assertEquals(0, start[start.length - 1]);
        for (int i = 1920; i < 3360; i++) assertEquals(0, end[i]);
    }
}
