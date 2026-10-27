package ru.habab.home;

import static org.junit.Assert.*;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.Test;

public class VoiceAuthLinkTest {
    private static String grant(String user, String token) throws Exception {
        return VoiceAuthLink.identity(user, "local-client", token);
    }

    @Test public void logoutReloadAndLoginInSameWebViewReplacesRevokedGrant() throws Exception {
        VoiceAuthLink link = new VoiceAuthLink(null);
        AtomicInteger writes = new AtomicInteger();
        VoiceAuthLink.Ticket first = link.authenticated(grant("owner-a", "grant-a"));
        assertTrue(link.commit(first, writes::incrementAndGet));
        assertTrue(link.nativeAllowed());
        assertNull(link.authenticated(grant("owner-a", "grant-a")));
        // HA revoke() persists JSON null, then assigns document.location.href = "/".
        assertTrue(link.signedOut());
        assertFalse(link.nativeAllowed());
        link.navigation();
        assertFalse(link.signedOut()); // Repeated signed-out snapshots do not repeatedly stop/clear.
        VoiceAuthLink.Ticket second = link.authenticated(grant("owner-b", "grant-b"));
        assertNotNull(second);
        assertFalse(link.nativeAllowed());
        assertTrue(link.commit(second, writes::incrementAndGet));
        assertTrue(link.nativeAllowed());
        assertEquals(2, writes.get());
        assertNull(link.authenticated(grant("owner-b", "grant-b")));
    }

    @Test public void transientDomAndOrdinaryNavigationKeepWorkingCredentials() throws Exception {
        String identity = grant("owner", "grant");
        VoiceAuthLink link = new VoiceAuthLink(identity);
        link.navigation(); // No auth snapshot while the page is loading.
        assertTrue(link.nativeAllowed());
        assertNull(link.authenticated(identity));
        link.navigation();
        assertTrue(link.nativeAllowed());
        assertNull(link.authenticated(identity));
    }

    @Test public void sameOwnerWithNewRefreshGrantRelinksWithoutObservedLogout() throws Exception {
        VoiceAuthLink link = new VoiceAuthLink(grant("owner", "revoked"));
        VoiceAuthLink.Ticket replacement = link.authenticated(grant("owner", "new-grant"));
        assertNotNull(replacement);
        assertFalse(link.nativeAllowed());
        assertTrue(link.commit(replacement, () -> {}));
        assertTrue(link.nativeAllowed());
    }

    @Test public void staleSaveCannotWriteAfterNavigationOrLogoutOrAnotherOwner() throws Exception {
        for (int scenario = 0; scenario < 3; scenario++) {
            VoiceAuthLink link = new VoiceAuthLink(null);
            VoiceAuthLink.Ticket stale = link.authenticated(grant("owner-a", "grant-a"));
            if (scenario == 0) link.navigation();
            else if (scenario == 1) link.signedOut();
            else link.authenticated(grant("owner-b", "grant-b"));
            AtomicInteger writes = new AtomicInteger();
            assertFalse(link.commit(stale, writes::incrementAndGet));
            assertEquals(0, writes.get());
        }
    }

    @Test public void onlyOneSavePerGrantAndFailureDoesNotBlockNextLogin() throws Exception {
        VoiceAuthLink link = new VoiceAuthLink(null);
        String first = grant("owner", "first");
        VoiceAuthLink.Ticket failed = link.authenticated(first);
        assertNull(link.authenticated(first));
        link.failed(failed);
        assertTrue(link.failed());
        assertNull(link.authenticated(first));
        VoiceAuthLink.Ticket nextLogin = link.authenticated(grant("owner", "second"));
        assertNotNull(nextLogin);
        assertFalse(link.failed());
        assertTrue(link.commit(nextLogin, () -> {}));
    }

    @Test public void interruptedOwnerSwitchCanRelinkPreviousGrantAfterClearing() throws Exception {
        String previous = grant("owner-a", "grant-a");
        VoiceAuthLink link = new VoiceAuthLink(previous);
        VoiceAuthLink.Ticket cancelled = link.authenticated(grant("owner-b", "grant-b"));
        link.credentialsCleared(); // MainActivity invalidates the previous native grant before a switch.
        link.navigation();
        assertFalse(link.commit(cancelled, () -> fail("stale switch wrote credentials")));
        VoiceAuthLink.Ticket restored = link.authenticated(previous);
        assertNotNull(restored);
        assertTrue(link.commit(restored, () -> {}));
        assertTrue(link.nativeAllowed());
    }

    @Test public void explicitRetryAllowsSameFailedGrant() throws Exception {
        VoiceAuthLink link = new VoiceAuthLink(null);
        String identity = grant("owner", "grant");
        link.failed(link.authenticated(identity));
        link.retry();
        assertNotNull(link.authenticated(identity));
    }

    @Test public void ordinaryAccessTokenRefreshDoesNotChangeGrantIdentity() throws Exception {
        String identity = grant("owner", "stable-refresh-grant");
        VoiceAuthLink link = new VoiceAuthLink(identity);
        // Access token and expiry do not participate in identity: native refresh may differ from WebView.
        assertNull(link.authenticated(grant("owner", "stable-refresh-grant")));
        assertNotEquals(identity, grant("other-owner", "stable-refresh-grant"));
        assertNotEquals(identity, grant("owner", "another-refresh-grant"));
        assertEquals(64, identity.length());
    }
}
