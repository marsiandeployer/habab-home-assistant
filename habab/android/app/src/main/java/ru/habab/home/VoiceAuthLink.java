package ru.habab.home;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

/** Tracks the authenticated grant, independently of navigation and temporary DOM loading. */
final class VoiceAuthLink {
    static final String SNAPSHOT_SCRIPT = """
        (() => {
          if (location.origin !== 'http://127.0.0.1:8123') return {state: 'loading'};
          if (localStorage.getItem('hassTokens') === 'null') return {state: 'signed_out'};
          const hass = document.querySelector('home-assistant')?.hass;
          const user = hass?.user;
          const auth = hass?.auth?.data;
          if (!user || !auth || !user.id) return {state: 'loading'};
          if (!user.is_admin || user.is_active === false) return {state: 'forbidden'};
          if (auth.hassUrl && new URL(auth.hassUrl).origin !== location.origin) return {state: 'loading'};
          if (!auth.access_token || !auth.refresh_token || !auth.clientId) return {state: 'loading'};
          return {state: 'authenticated', userId: user.id, auth: {
            access_token: auth.access_token, refresh_token: auth.refresh_token,
            clientId: auth.clientId, expires: auth.expires, expires_in: auth.expires_in
          }};
        })()
        """;

    interface Save { void run() throws Exception; }
    static final class Ticket {
        final String identity;
        private final long generation;
        private Ticket(String identity, long generation) { this.identity = identity; this.generation = generation; }
    }
    private long generation;
    private String linkedIdentity, observedIdentity, failedIdentity;
    private Ticket pending;
    private boolean signedOut;
    private boolean blocked;

    VoiceAuthLink(String identity) { linkedIdentity = identity; }

    static String identity(String userId, String clientId, String refreshToken) throws Exception {
        if (userId.isEmpty() || clientId.isEmpty() || refreshToken.isEmpty())
            throw new IllegalArgumentException("invalid_voice_authorization");
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        // Length-prefix fields so identities cannot collide through delimiter choices.
        for (String value : new String[]{userId, clientId, refreshToken}) {
            byte[] bytes = value.getBytes(StandardCharsets.UTF_8);
            digest.update((bytes.length + ":").getBytes(StandardCharsets.US_ASCII)); digest.update(bytes);
        }
        StringBuilder result = new StringBuilder();
        for (byte value : digest.digest()) result.append(String.format(java.util.Locale.ROOT, "%02x", value & 255));
        return result.toString();
    }

    synchronized Ticket authenticated(String identity) {
        if (!identity.equals(observedIdentity) || signedOut) {
            generation++; pending = null; failedIdentity = null;
            observedIdentity = identity; signedOut = false;
        }
        if (identity.equals(linkedIdentity)) { blocked = false; return null; }
        blocked = true;
        if (pending != null || identity.equals(failedIdentity)) return null;
        pending = new Ticket(identity, generation);
        return pending;
    }

    /** Explicit revoked/forbidden auth is different from a missing, still-loading DOM. */
    synchronized boolean signedOut() {
        if (signedOut) return false;
        generation++; pending = null; linkedIdentity = null; observedIdentity = null;
        failedIdentity = null; signedOut = true; blocked = true;
        return true;
    }

    synchronized void credentialsCleared() { linkedIdentity = null; }
    synchronized void navigation() { generation++; pending = null; }
    synchronized void retry() { failedIdentity = null; }
    synchronized boolean failed() { return failedIdentity != null; }
    synchronized boolean nativeAllowed() { return !blocked; }
    synchronized boolean current(Ticket ticket) {
        return ticket != null && ticket == pending && ticket.generation == generation && !signedOut;
    }
    synchronized boolean commit(Ticket ticket, Save save) throws Exception {
        if (!current(ticket)) return false;
        save.run();
        linkedIdentity = ticket.identity; pending = null; failedIdentity = null; blocked = false;
        return true;
    }
    synchronized void failed(Ticket ticket) {
        if (!current(ticket)) return;
        failedIdentity = ticket.identity; pending = null;
    }
}
