package ru.habab.home;

import org.junit.Test;
import static org.junit.Assert.*;

public class VoiceToolEndDrainTest {
    @Test public void waitsForResponseAndPlaybackToFinish() {
        VoiceToolEndDrain drain = new VoiceToolEndDrain();
        drain.responseStarted("response");
        drain.audioStarted("item");
        drain.begin(100, 15000);

        assertFalse(drain.ready(101));
        drain.responseFinished("response");
        assertFalse(drain.ready(102));
        drain.audioFinished("item");
        assertTrue(drain.ready(103));
    }

    @Test public void timeoutBoundsMissingProviderCompletion() {
        VoiceToolEndDrain drain = new VoiceToolEndDrain();
        drain.responseStarted("response");
        drain.begin(100, 15000);

        assertFalse(drain.ready(15099));
        assertTrue(drain.ready(15100));
    }

    @Test public void interruptedOldTurnDoesNotDelayTheNextEnd() {
        VoiceToolEndDrain drain = new VoiceToolEndDrain();
        drain.responseStarted("interrupted");
        drain.audioStarted("discarded");
        drain.interrupted();
        drain.responseStarted("confirmation");
        drain.begin(100, 15000);
        assertFalse(drain.ready(101));
        drain.responseFinished("confirmation");
        assertTrue(drain.ready(102));
    }

    @Test public void playbackMayFinishBeforeProviderCompletion() {
        VoiceToolEndDrain drain = new VoiceToolEndDrain();
        drain.responseStarted("confirmation");
        drain.audioStarted("audio");
        drain.begin(100, 15000);
        drain.audioFinished("audio");
        assertFalse(drain.ready(101));
        drain.responseFinished("confirmation");
        assertTrue(drain.ready(102));
    }
}
