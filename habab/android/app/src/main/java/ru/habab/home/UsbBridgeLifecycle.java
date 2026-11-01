package ru.habab.home;

final class UsbBridgeLifecycle {
    static final String PREFERENCES = "usb_bridge";
    static final String ENABLED = "enabled";

    private UsbBridgeLifecycle() { }

    static boolean shouldRestoreAfterBoot(String action, boolean enabled) {
        return "android.intent.action.BOOT_COMPLETED".equals(action) && enabled;
    }

    static boolean matchesPermissionResult(boolean requestPending, boolean serviceRunning,
            String expectedDeviceName, String actualDeviceName,
            String expectedToken, String actualToken) {
        return requestPending && serviceRunning
                && actualDeviceName != null && actualDeviceName.equals(expectedDeviceName)
                && actualToken != null && actualToken.equals(expectedToken);
    }

    static boolean matchesPendingDevice(String expectedDeviceName, String actualDeviceName) {
        return actualDeviceName != null && actualDeviceName.equals(expectedDeviceName);
    }
}
