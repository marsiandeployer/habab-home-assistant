package ru.habab.home;

import java.io.IOException;
import java.net.HttpURLConnection;
import java.net.URL;

/** Bounded HTTPS downloads; manifest signatures and archive hashes are checked by RuntimeStore. */
final class ReleaseDownload {
    interface Opener { HttpURLConnection open(URL url) throws IOException; }

    static HttpURLConnection connection(String address) throws IOException {
        return connection(address, url -> (HttpURLConnection) url.openConnection());
    }

    static HttpURLConnection connection(String address, Opener opener) throws IOException {
        URL initial = new URL(address);
        validate(initial, false);
        URL current = initial;
        for (int redirects = 0; redirects <= 5; redirects++) {
            HttpURLConnection connection = opener.open(current);
            connection.setConnectTimeout(20000);
            connection.setReadTimeout(60000);
            connection.setInstanceFollowRedirects(false);
            int code;
            try { code = connection.getResponseCode(); }
            catch (IOException error) { connection.disconnect(); throw error; }
            if (code == 200) return connection;
            String location = connection.getHeaderField("Location");
            connection.disconnect();
            if (redirects == 5 || !(code == 301 || code == 302 || code == 303 || code == 307 || code == 308)
                    || location == null || location.isEmpty())
                throw new IOException("Download HTTP error: " + code);
            URL next = new URL(current, location);
            validate(next, true);
            boolean sameHost = initial.getHost().equalsIgnoreCase(next.getHost());
            boolean githubAsset = initial.getHost().equalsIgnoreCase("github.com")
                    && next.getHost().equalsIgnoreCase("release-assets.githubusercontent.com");
            if (!sameHost && !githubAsset) throw new IOException("Untrusted download redirect host");
            current = next;
        }
        throw new IOException("Too many download redirects");
    }

    private static void validate(URL url, boolean redirected) throws IOException {
        if (!url.getProtocol().equals("https") || url.getUserInfo() != null || url.getRef() != null
                || (!redirected && url.getQuery() != null) || (url.getPort() != -1 && url.getPort() != 443))
            throw new IOException("A plain HTTPS URL is required");
    }
}
