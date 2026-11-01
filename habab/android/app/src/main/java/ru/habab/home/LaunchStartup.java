package ru.habab.home;

import java.net.URI;
import java.net.URISyntaxException;
import java.net.UnknownHostException;
import java.net.ConnectException;
import java.net.NoRouteToHostException;
import java.net.SocketTimeoutException;

/** One launcher session installs, starts and opens the server without duplicate work. */
final class LaunchStartup {
    private static final long NETWORK_RETRY_DELAY_MS = 30_000;
    enum Action { WAIT, INSTALL, START, OPEN }
    boolean installAttempted;
    boolean startAttempted;
    boolean homeOpened;

    static boolean transientNetworkFailure(Throwable error) {
        for (int depth = 0; error != null && depth < 8; depth++, error = error.getCause()) {
            if (error instanceof UnknownHostException || error instanceof ConnectException
                    || error instanceof NoRouteToHostException || error instanceof SocketTimeoutException) return true;
        }
        return false;
    }

    boolean retryInstallAfterNetworkFailure(boolean installed, boolean maintenance,
                                            boolean suppressed, long failedAt, long now) {
        if (installed || maintenance || suppressed || !installAttempted || failedAt < 0
                || now < failedAt || now - failedAt < NETWORK_RETRY_DELAY_MS) return false;
        installAttempted = false;
        return true;
    }

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
