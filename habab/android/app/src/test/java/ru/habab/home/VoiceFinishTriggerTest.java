package ru.habab.home;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

public class VoiceFinishTriggerTest {
    @Test public void timeoutFailuresHaveTheirOwnBoundedReason() {
        assertEquals(VoiceFinishTrigger.TIMEOUT,
                VoiceFinishTrigger.fromFailureCode("voice_timeout"));
        assertEquals(VoiceFinishTrigger.TIMEOUT,
                VoiceFinishTrigger.fromFailureCode("voice_playback_timeout"));
    }

    @Test public void otherFailuresDoNotExposeTheirErrorTextAsReason() {
        assertEquals(VoiceFinishTrigger.ERROR,
                VoiceFinishTrigger.fromFailureCode("realtime_provider_error"));
        assertEquals(VoiceFinishTrigger.ERROR, VoiceFinishTrigger.fromFailureCode(null));
    }
}
