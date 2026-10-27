package ru.habab.home;

import static org.junit.Assert.*;
import org.junit.Test;

public class LaunchStartupTest {
    private static LaunchStartup.Action next(LaunchStartup launch, boolean installed, boolean maintenance,
                                              boolean active, boolean ready, boolean foreground) {
        return launch.next(installed, maintenance, active, false, ready, false, false, foreground);
    }

    @Test public void firstInstallContinuesToStartAndOpenWithoutAnotherUserAction() {
        LaunchStartup launch = new LaunchStartup();
        assertEquals(LaunchStartup.Action.INSTALL, next(launch, false, false, false, false, true));
        assertEquals(LaunchStartup.Action.WAIT, next(launch, false, true, false, false, true));
        assertEquals(LaunchStartup.Action.START, next(launch, true, false, false, false, true));
        assertEquals(LaunchStartup.Action.WAIT, next(launch, true, false, true, false, true));
        assertEquals(LaunchStartup.Action.OPEN, next(launch, true, false, true, true, true));
        assertEquals(LaunchStartup.Action.WAIT, next(launch, true, false, true, true, true));
    }

    @Test public void existingInstallationIsNeverReinstalledByLaunchingOrReturning() {
        LaunchStartup launch = new LaunchStartup();
        assertEquals(LaunchStartup.Action.START, next(launch, true, false, false, false, true));
        assertFalse(launch.installAttempted);
        assertEquals(LaunchStartup.Action.OPEN, next(launch, true, false, true, true, true));
        assertEquals(LaunchStartup.Action.WAIT, next(launch, true, false, true, true, true));
        assertFalse(launch.installAttempted);
    }

    @Test public void failedInstallationAndStartDoNotSpinRetries() {
        LaunchStartup launch = new LaunchStartup();
        assertEquals(LaunchStartup.Action.INSTALL, next(launch, false, false, false, false, true));
        assertEquals(LaunchStartup.Action.WAIT, next(launch, false, false, false, false, true));
        launch.installAttempted = false; // Explicit retry button.
        assertEquals(LaunchStartup.Action.INSTALL, next(launch, false, false, false, false, true));
        assertEquals(LaunchStartup.Action.START, next(launch, true, false, false, false, true));
        assertEquals(LaunchStartup.Action.WAIT, next(launch, true, false, false, false, true));
    }

    @Test public void activityRecreationResumesMaintenanceWithoutDuplicateExtraction() {
        LaunchStartup launch = new LaunchStartup();
        launch.installAttempted = true; // Saved activity state.
        assertEquals(LaunchStartup.Action.WAIT, next(launch, false, true, false, false, true));
        assertEquals(LaunchStartup.Action.START, next(launch, true, false, false, false, true));
        LaunchStartup resumed = new LaunchStartup();
        resumed.installAttempted = launch.installAttempted;
        resumed.startAttempted = launch.startAttempted;
        assertEquals(LaunchStartup.Action.WAIT, next(resumed, true, false, true, false, true));
        assertEquals(LaunchStartup.Action.OPEN, next(resumed, true, false, true, true, true));
    }

    @Test public void backgroundReadinessWaitsForForegroundAndNeverReopensAfterBack() {
        LaunchStartup launch = new LaunchStartup();
        assertEquals(LaunchStartup.Action.WAIT, next(launch, true, false, true, true, false));
        assertFalse(launch.homeOpened);
        assertEquals(LaunchStartup.Action.OPEN, next(launch, true, false, true, true, true));
        assertEquals(LaunchStartup.Action.WAIT, next(launch, true, false, true, true, true));
    }

    @Test public void manualStopMaintenanceAndBusyOperationsBlockStartup() {
        LaunchStartup launch = new LaunchStartup();
        assertEquals(LaunchStartup.Action.WAIT, launch.next(false, false, false, false, false, true, false, true));
        assertEquals(LaunchStartup.Action.WAIT, launch.next(false, false, false, false, false, false, true, true));
        assertEquals(LaunchStartup.Action.WAIT, launch.next(false, false, false, true, false, false, false, true));
        assertEquals(LaunchStartup.Action.WAIT, launch.next(false, true, false, false, false, false, false, true));
        assertFalse(launch.installAttempted);
        assertFalse(launch.startAttempted);
    }

    @Test public void authorizationRequiresExactLocalOrigin() {
        String local = "http://127.0.0.1:8123/onboarding.html";
        assertTrue(LaunchStartup.localOrigin(local));
        for (String unsafe : new String[]{null, "", "http://127.0.0.1:8124/", "http://localhost:8123/",
                "https://127.0.0.1:8123/", "http://127.0.0.1:8123.evil.test/", "http://evil.test:8123/",
                "http://user@127.0.0.1:8123/", "file:///data/local/", "javascript:alert(1)"}) {
            assertFalse(String.valueOf(unsafe), LaunchStartup.localOrigin(unsafe));
        }
    }
}
