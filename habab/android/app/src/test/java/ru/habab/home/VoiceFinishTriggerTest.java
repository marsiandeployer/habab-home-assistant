package ru.habab.home;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;

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

    @Test public void logLineContainsOnlyAllowlistedFinishReasons() {
        for (String reason : new String[]{VoiceFinishTrigger.TOOL, VoiceFinishTrigger.IDLE_20S,
                VoiceFinishTrigger.AUDIO_LIMIT, VoiceFinishTrigger.TIMEOUT, VoiceFinishTrigger.ERROR,
                VoiceFinishTrigger.SERVICE_STOP, VoiceFinishTrigger.NORMAL}) {
            assertEquals("finish_trigger=" + reason, VoiceFinishTrigger.logLine(reason));
        }
        assertNull(VoiceFinishTrigger.logLine(null));
        assertNull(VoiceFinishTrigger.logLine("transcript or secret sentinel"));
    }
}
