package ru.habab.home;

import java.net.URI;
import java.net.URISyntaxException;

/** One launcher session may install, start, and open once; retries require a user action. */
final class LaunchStartup {
    enum Action { WAIT, INSTALL, START, OPEN }
    boolean installAttempted;
    boolean startAttempted;
    boolean homeOpened;

    Action next(boolean installed, boolean maintenance, boolean serverActive, boolean stopping,
                boolean serverReady, boolean suppressed, boolean busy, boolean foreground) {
        if (suppressed || busy || maintenance || stopping) return Action.WAIT;
        if (!installed) {
            if (installAttempted || serverActive) return Action.WAIT;
            installAttempted = true;
            return Action.INSTALL;
        }
        if (serverReady && foreground && !homeOpened) {
            homeOpened = true;
            return Action.OPEN;
        }
        if (serverActive || startAttempted) return Action.WAIT;
        startAttempted = true;
        return Action.START;
    }

    static boolean localOrigin(String address) {
        if (address == null) return false;
        try {
            URI uri = new URI(address);
            return "http".equals(uri.getScheme()) && "127.0.0.1".equals(uri.getHost())
                    && uri.getPort() == 8123 && uri.getRawUserInfo() == null;
        } catch (URISyntaxException error) { return false; }
    }

}
