package ru.habab.home;

import static org.junit.Assert.*;
import org.junit.Test;

public class VoiceRouteTargetTest {
    @Test public void toggleUsesOnlyTheSavedPhoneOrBluetoothChoice() {
        assertEquals("bluetooth", VoiceRouteTarget.resolve("toggle", "phone"));
        assertEquals("phone", VoiceRouteTarget.resolve("toggle", "bluetooth"));
        assertEquals("phone", VoiceRouteTarget.resolve("toggle", "duplicate"));
        assertEquals("phone", VoiceRouteTarget.resolve("phone", "bluetooth"));
    }

    @Test public void explicitConfirmedRouteReplacesDuplicatePreference() {
        assertEquals("bluetooth",
                VoiceRouteTarget.confirmedPreferenceTarget("bluetooth"));
        assertEquals("phone",
                VoiceRouteTarget.confirmedPreferenceTarget("phone"));
        assertTrue(VoiceRouteTarget.requiresServiceRestartForConfirmedRoute("duplicate", "bluetooth"));
        assertTrue(VoiceRouteTarget.requiresServiceRestartForConfirmedRoute("duplicate", "phone"));
        assertFalse(VoiceRouteTarget.requiresServiceRestartForConfirmedRoute("bluetooth", "bluetooth"));
    }

    @Test public void rejectsAnyUnlistedTarget() {
        try {
            VoiceRouteTarget.resolve("shell", "phone");
            fail("unexpected route target accepted");
        } catch (IllegalArgumentException expected) {
            assertEquals("invalid_voice_route_target", expected.getMessage());
        }
    }

    @Test public void confirmsOnlyStablePhysicalRouteWhenWakeIsRunning() {
        assertTrue(VoiceRouteTarget.confirmed("bluetooth", true, "wake", "bluetooth", 7));
        assertFalse(VoiceRouteTarget.confirmed("bluetooth", true, "wake", "bluetooth_unavailable", 15));
        assertFalse(VoiceRouteTarget.confirmed("bluetooth", true, "preparing", "bluetooth", 7));
        assertTrue(VoiceRouteTarget.confirmed("phone", true, "wake", "phone", 15));
        assertFalse(VoiceRouteTarget.confirmed("phone", true, "wake", "phone", 3));
        assertFalse(VoiceRouteTarget.confirmed("phone", true, "wake", "phone", 7));
        assertFalse(VoiceRouteTarget.confirmed("phone", false, "off", "off", -1));
    }

    @Test public void failedWakeOnRollsBackThePreviousEnabledPreference() {
        assertTrue(VoiceRouteTarget.shouldRollback("error"));
        assertFalse(VoiceRouteTarget.shouldRollback("applied"));
        assertTrue(VoiceRouteTarget.wakeEnabledAfterAck("applied", false, true));
        assertFalse(VoiceRouteTarget.wakeEnabledAfterAck("error", false, true));
        assertTrue(VoiceRouteTarget.wakeEnabledAfterAck("error", true, false));
    }

    @Test public void routeMutationWaitsUntilConversationAndEndCueAreComplete() {
        assertTrue(VoiceRouteTarget.safeBoundary(false, false, false, "off", false, false, false));
        assertTrue(VoiceRouteTarget.safeBoundary(true, false, false, "off", false, false, false));
        assertTrue(VoiceRouteTarget.safeBoundary(true, true, true, "wake", false, true, false));
        assertFalse(VoiceRouteTarget.safeBoundary(true, true, true, "thinking", true, false, false));
        assertFalse(VoiceRouteTarget.safeBoundary(true, true, true, "wake", false, false, true));
        assertFalse(VoiceRouteTarget.safeBoundary(true, true, false, "preparing", false, true, false));
    }

    @Test public void deliveryRetryDoesNotReplayAnAlreadyAcceptedCommand() {
        assertTrue(VoiceRouteTarget.isNewCommand("command-2", "command-1"));
        assertFalse(VoiceRouteTarget.isNewCommand("command-1", "command-1"));
        assertFalse(VoiceRouteTarget.isNewCommand(null, "command-1"));
    }

    @Test public void staleAckCleanupMatchesOnlyItsOwnPendingCommand() {
        assertTrue(VoiceRouteTarget.matchesPendingCommand("stale-id", "stale-id"));
        assertFalse(VoiceRouteTarget.matchesPendingCommand("new-id", "stale-id"));
        assertFalse(VoiceRouteTarget.matchesPendingCommand(null, "stale-id"));
    }
}
