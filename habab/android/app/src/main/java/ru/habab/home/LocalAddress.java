package ru.habab.home;

import java.net.URI;
import java.net.URISyntaxException;

/** A local alias link always opens this installation, without system DNS changes. */
final class LocalAddress {
    static String resolve(String address) {
        if (address == null) return null;
        try {
            URI uri = new URI(address);
            if (!"http".equalsIgnoreCase(uri.getScheme()) || !"homeassistant.local".equalsIgnoreCase(uri.getHost())
                    || uri.getPort() != 8123 || uri.getRawUserInfo() != null) return null;
            String path = uri.getRawPath();
            return "http://127.0.0.1:8123" + (path == null || path.isEmpty() ? "/" : path)
                    + (uri.getRawQuery() == null ? "" : "?" + uri.getRawQuery())
                    + (uri.getRawFragment() == null ? "" : "#" + uri.getRawFragment());
        } catch (URISyntaxException error) { return null; }
    }
}
