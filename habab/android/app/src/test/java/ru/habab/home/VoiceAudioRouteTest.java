package ru.habab.home;

import org.junit.Test;

import static org.junit.Assert.assertEquals;

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
}
