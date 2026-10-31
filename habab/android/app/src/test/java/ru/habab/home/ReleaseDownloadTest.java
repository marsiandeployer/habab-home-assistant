package ru.habab.home;

import static org.junit.Assert.*;
import java.io.IOException;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.List;
import org.junit.Test;

public class ReleaseDownloadTest {
    static class Response extends HttpURLConnection {
        final int code;
        final String location;
        boolean disconnected;
        Response(URL url, int code, String location) { super(url); this.code = code; this.location = location; }
        @Override public int getResponseCode() { return code; }
        @Override public String getHeaderField(String name) { return location; }
        @Override public void disconnect() { disconnected = true; }
        @Override public boolean usingProxy() { return false; }
        @Override public void connect() {}
    }

    @Test public void followsGithubSignedAssetRedirect() throws Exception {
        List<Response> calls = new ArrayList<>();
        HttpURLConnection result = ReleaseDownload.connection("https://github.com/org/repo/releases/download/tag/runtime.tgz", url -> {
            Response response = new Response(url, calls.isEmpty() ? 302 : 200,
                    "https://release-assets.githubusercontent.com/asset?signature=short-lived");
            calls.add(response); return response;
        });
        assertEquals(2, calls.size());
        assertTrue(calls.get(0).disconnected);
        assertSame(calls.get(1), result);
        assertFalse(result.getInstanceFollowRedirects());
    }

    @Test public void rejectsDowngradeCredentialsAndUnrelatedHosts() throws Exception {
        for (String location : new String[]{"http://github.com/asset", "https://user@github.com/asset",
                "https://evil.example/asset", "https://github.com:444/asset", "https://github.com/asset#fragment"}) {
            assertThrows(IOException.class, () -> ReleaseDownload.connection("https://github.com/o/r/asset", url ->
                    new Response(url, 302, location)));
        }
    }

    @Test public void redirectsAreBoundedAndDisconnected() throws Exception {
        List<Response> calls = new ArrayList<>();
        assertThrows(IOException.class, () -> ReleaseDownload.connection("https://github.com/o/r/asset", url -> {
            Response response = new Response(url, 302, "/loop"); calls.add(response); return response;
        }));
        assertEquals(6, calls.size());
        assertTrue(calls.stream().allMatch(response -> response.disconnected));
    }

    @Test public void allowsSameHostRedirectOnExistingDownloadServer() throws Exception {
        List<Response> calls = new ArrayList<>();
        ReleaseDownload.connection("https://habab-demo.wpmix.net/downloads/runtime.tgz", url -> {
            Response response = new Response(url, calls.isEmpty() ? 307 : 200, "/new-runtime.tgz");
            calls.add(response); return response;
        });
        assertEquals(2, calls.size());
    }

    @Test public void validatesInitialUrlAndRejectsHttpErrors() {
        assertThrows(IOException.class, () -> ReleaseDownload.connection("http://github.com/o/r/asset", url -> new Response(url, 200, null)));
        assertThrows(IOException.class, () -> ReleaseDownload.connection("https://github.com/o/r/asset?query=x", url -> new Response(url, 200, null)));
        assertThrows(IOException.class, () -> ReleaseDownload.connection("https://github.com/o/r/asset", url -> new Response(url, 404, null)));
    }
}
