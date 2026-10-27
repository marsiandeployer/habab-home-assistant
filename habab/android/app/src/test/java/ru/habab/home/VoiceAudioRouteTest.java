package ru.habab.home;

import org.junit.Test;

import java.util.ArrayList;
import java.util.List;

import static org.junit.Assert.*;

public class VoiceAudioRouteTest {
    @Test public void requestedRoutePreservesAllCurrentModes() {
        assertEquals(VoiceAudioRoute.PHONE, VoiceAudioRoute.requestedRoute("phone", true));
        assertEquals(VoiceAudioRoute.BLUETOOTH, VoiceAudioRoute.requestedRoute("bluetooth", false));
        assertEquals(VoiceAudioRoute.DUPLICATE, VoiceAudioRoute.requestedRoute("duplicate", false));
    }

    @Test public void requestedRouteUsesLegacyBluetoothSettingOnlyWhenModeIsAbsent() {
        assertEquals(VoiceAudioRoute.BLUETOOTH, VoiceAudioRoute.requestedRoute(null, true));
        assertEquals(VoiceAudioRoute.PHONE, VoiceAudioRoute.requestedRoute(null, false));
        assertEquals(VoiceAudioRoute.PHONE, VoiceAudioRoute.requestedRoute("unknown", true));
    }

    @Test public void reconnectRebindsDuplicateTracksToCurrentEndpoints() {
        RecordingOutput<String> primary = new RecordingOutput<>();
        RecordingOutput<String> secondary = new RecordingOutput<>();

        assertTrue(VoiceAudioRoute.refreshDuplicateOutputs(true, true, primary, secondary, "bt-old", "phone"));
        assertTrue(VoiceAudioRoute.refreshDuplicateOutputs(true, true, primary, secondary,
                "bt-reconnected", "phone"));

        assertEquals(java.util.Arrays.asList("bt-old", "bt-reconnected"), primary.devices);
        assertEquals(java.util.Arrays.asList("phone", "phone"), secondary.devices);
    }

    @Test public void phoneModeAndUnavailableBluetoothKeepExistingFallbackUntouched() {
        RecordingOutput<String> primary = new RecordingOutput<>();
        RecordingOutput<String> secondary = new RecordingOutput<>();

        assertFalse(VoiceAudioRoute.refreshDuplicateOutputs(false, true, primary, secondary, "bt", "phone"));
        assertFalse(VoiceAudioRoute.refreshDuplicateOutputs(true, false, primary, secondary, "bt", "phone"));
        assertFalse(VoiceAudioRoute.refreshDuplicateOutputs(true, true, primary, secondary, null, "phone"));
        assertFalse(VoiceAudioRoute.refreshDuplicateOutputs(true, true, primary, secondary, "bt", null));

        assertTrue(primary.devices.isEmpty());
        assertTrue(secondary.devices.isEmpty());
    }

    @Test public void failedPhoneOutputRebindDoesNotPartiallyMovePrimaryTrack() {
        RecordingOutput<String> primary = new RecordingOutput<>();
        RecordingOutput<String> secondary = new RecordingOutput<>();
        secondary.accept = false;

        assertFalse(VoiceAudioRoute.refreshDuplicateOutputs(true, true, primary, secondary, "bt", "phone"));

        assertTrue(primary.devices.isEmpty());
        assertTrue(secondary.devices.isEmpty());
    }

    private static final class RecordingOutput<T> implements VoiceAudioRoute.PreferredOutput<T> {
        private final List<T> devices = new ArrayList<>();
        private boolean accept = true;

        @Override public boolean setPreferredDevice(T device) {
            if (!accept) return false;
            devices.add(device);
            return true;
        }
    }
}
