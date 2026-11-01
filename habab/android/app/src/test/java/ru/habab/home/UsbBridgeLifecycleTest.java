package ru.habab.home;

import org.junit.Test;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public final class UsbBridgeLifecycleTest {
    @Test
    public void restoresOnlyWhenExplicitlyEnabledAtBoot() {
        assertTrue(UsbBridgeLifecycle.shouldRestoreAfterBoot(
                "android.intent.action.BOOT_COMPLETED", true));
        assertFalse(UsbBridgeLifecycle.shouldRestoreAfterBoot(
                "android.intent.action.BOOT_COMPLETED", false));
        assertFalse(UsbBridgeLifecycle.shouldRestoreAfterBoot(
                "android.intent.action.LOCKED_BOOT_COMPLETED", true));
        assertFalse(UsbBridgeLifecycle.shouldRestoreAfterBoot("other", true));
    }

    @Test
    public void acceptsOnlyCurrentPermissionResultForExpectedDevice() {
        assertTrue(UsbBridgeLifecycle.matchesPermissionResult(
                true, true, "usb1", "usb1", "token-current", "token-current"));
        assertFalse(UsbBridgeLifecycle.matchesPermissionResult(
                true, true, "usb1", "usb1", "token-current", "token-stale"));
        assertFalse(UsbBridgeLifecycle.matchesPermissionResult(
                true, true, "usb1", "usb2", "token-current", "token-current"));
        assertFalse(UsbBridgeLifecycle.matchesPermissionResult(
                false, true, "usb1", "usb1", "token-current", "token-current"));
        assertFalse(UsbBridgeLifecycle.matchesPermissionResult(
                true, false, "usb1", "usb1", "token-current", "token-current"));
        assertFalse(UsbBridgeLifecycle.matchesPermissionResult(
                true, true, "usb1", null, "token-current", "token-current"));
        assertFalse(UsbBridgeLifecycle.matchesPermissionResult(
                true, true, "usb1", "usb1", "token-current", null));
    }

    @Test
    public void detachOnlyCancelsMatchingPendingPermission() {
        assertTrue(UsbBridgeLifecycle.matchesPendingDevice("usb1", "usb1"));
        assertFalse(UsbBridgeLifecycle.matchesPendingDevice("usb1", "usb2"));
        assertFalse(UsbBridgeLifecycle.matchesPendingDevice("usb1", null));
        assertFalse(UsbBridgeLifecycle.matchesPendingDevice(null, "usb1"));
    }
}
