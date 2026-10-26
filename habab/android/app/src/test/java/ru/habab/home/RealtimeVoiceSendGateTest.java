package ru.habab.home;

import static org.junit.Assert.*;
import org.junit.Test;

public class RealtimeVoiceSendGateTest {
    @Test public void endingSessionRejectsLateUnscopedToolResultsAndAllOtherSends() {
        assertFalse(VoiceSendGate.canSend(false, true, true, true, null, 4));
        assertFalse(VoiceSendGate.canSend(false, true, true, true, 4L, 4));
    }

    @Test public void activeSessionStillRequiresAnOpenSocketAllowedToolAndCurrentEpoch() {
        assertTrue(VoiceSendGate.canSend(false, false, true, true, 4L, 4));
        assertFalse(VoiceSendGate.canSend(false, false, false, true, null, 4));
        assertFalse(VoiceSendGate.canSend(false, false, true, false, null, 4));
        assertFalse(VoiceSendGate.canSend(false, false, true, true, 3L, 4));
    }
}
