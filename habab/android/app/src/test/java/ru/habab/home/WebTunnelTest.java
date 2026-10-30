package ru.habab.home;

import static org.junit.Assert.*;

import okhttp3.Headers;
import okhttp3.HttpUrl;
import okhttp3.WebSocket;
import org.json.JSONArray;
import org.junit.Test;
import java.lang.reflect.Proxy;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

public final class WebTunnelTest {
    private static final String CONTROL = "wss://home-p30.wpmix.net/_habab_tunnel/control";

    @Test public void relayMustBeExactPinnedWssOriginPath() throws Exception {
        HttpUrl relay = WebTunnel.validateRelay(CONTROL);
        HttpUrl data = WebTunnel.dataUrl(relay, "0123456789abcdef0123456789abcdef0123456789abcdef");
        assertEquals("https", data.scheme()); // OkHttp HttpUrl stores WebSocket endpoints as HTTPS URLs.
        assertEquals("home-p30.wpmix.net", data.host());
        assertEquals(443, data.port());
        assertEquals("/_habab_tunnel/data/0123456789abcdef0123456789abcdef0123456789abcdef", data.encodedPath());
        HttpUrl configuredRelay = WebTunnel.validateRelay("wss://relay.example:9443/_habab_tunnel/control");
        assertEquals("relay.example", configuredRelay.host());
        assertEquals(9443, configuredRelay.port());
    }

    @Test public void rejectsRelayUrlVariantsThatCouldChangeOriginOrRoute() {
        for (String value : new String[]{
                "https://home-p30.wpmix.net/_habab_tunnel/control",
                "wss://home-p30.wpmix.net/other",
                "wss://home-p30.wpmix.net/_habab_tunnel/control?next=https://attacker.example",
                "wss://user@home-p30.wpmix.net/_habab_tunnel/control"}) {
            try { WebTunnel.validateRelay(value); fail("accepted " + value); }
            catch (Exception expected) { }
        }
    }

    @Test public void relayStreamIdCannotSelectAnotherPath() throws Exception {
        HttpUrl relay = WebTunnel.validateRelay(CONTROL);
        for (String stream : new String[]{"../other", "abcdef", "a".repeat(47), "g".repeat(48)}) {
            try { WebTunnel.dataUrl(relay, stream); fail("accepted stream id"); }
            catch (Exception expected) { }
        }
    }

    @Test public void everyOriginPathStaysOnLiteralLoopback() throws Exception {
        HttpUrl url = WebTunnel.localUrl("/auth/authorize?client_id=home&redirect_uri=%2F");
        assertEquals("127.0.0.1", url.host());
        assertEquals(8123, url.port());
        assertEquals("/auth/authorize", url.encodedPath());
        for (String path : new String[]{"//attacker.example/x", "http://attacker.example/", "/\\attacker",
                "/path#fragment", "/line\r\nHost: attacker.example"}) {
            try { WebTunnel.localUrl(path); fail("accepted unsafe path"); }
            catch (Exception expected) { }
        }
        // Encoded slashes remain path bytes; they cannot replace the fixed authority.
        assertEquals("127.0.0.1", WebTunnel.localUrl("/%2f%2fattacker.example/path").host());
    }

    @Test public void forwardsNativeAuthHostAndOriginButDropsForwardingAndHopHeaders() throws Exception {
        JSONArray pairs = new JSONArray()
                .put(new JSONArray().put("Host").put("home-p30.wpmix.net"))
                .put(new JSONArray().put("Origin").put("https://home-p30.wpmix.net"))
                .put(new JSONArray().put("Authorization").put("Bearer native-login"))
                .put(new JSONArray().put("Cookie").put("ha_auth=opaque"))
                .put(new JSONArray().put("X-Forwarded-For").put("192.0.2.1"))
                .put(new JSONArray().put("X-Real-IP").put("192.0.2.1"))
                .put(new JSONArray().put("Forwarded").put("for=192.0.2.1"))
                .put(new JSONArray().put("X-Habab-Home").put("spoof"))
                .put(new JSONArray().put("Connection").put("keep-alive, X-Private-Hop"))
                .put(new JSONArray().put("X-Private-Hop").put("secret"))
                .put(new JSONArray().put("Content-Length").put("123"))
                .put(new JSONArray().put("Sec-WebSocket-Key").put("relay-key"));
        Headers headers = WebTunnel.headers(pairs, true);
        assertEquals("home-p30.wpmix.net", headers.get("Host"));
        assertEquals("https://home-p30.wpmix.net", headers.get("Origin"));
        assertEquals("Bearer native-login", headers.get("Authorization"));
        assertEquals("ha_auth=opaque", headers.get("Cookie"));
        for (String dropped : new String[]{"X-Forwarded-For", "X-Real-IP", "Forwarded", "X-Habab-Home", "Connection",
                "X-Private-Hop", "Content-Length", "Sec-WebSocket-Key"}) assertNull(headers.get(dropped));
    }

    @Test public void gracefulDataCloseWaitsForPeerOrTimesOut() {
        CompletableFuture<Void> ended = new CompletableFuture<>();
        WebSocket peerCloses = (WebSocket) Proxy.newProxyInstance(WebSocket.class.getClassLoader(),
                new Class<?>[]{WebSocket.class}, (proxy, method, args) -> {
                    if ("close".equals(method.getName())) { ended.complete(null); return true; }
                    if (method.getReturnType() == boolean.class) return false;
                    if (method.getReturnType() == long.class) return 0L;
                    if (method.getReturnType() == int.class) return 0;
                    return null;
                });
        assertTrue(WebTunnel.gracefulDataClose(peerCloses, ended, 100));

        CompletableFuture<Void> neverEnds = new CompletableFuture<>();
        WebSocket slowPeer = (WebSocket) Proxy.newProxyInstance(WebSocket.class.getClassLoader(),
                new Class<?>[]{WebSocket.class}, (proxy, method, args) -> true);
        long started = System.nanoTime();
        assertFalse(WebTunnel.gracefulDataClose(slowPeer, neverEnds, 25));
        assertTrue(System.nanoTime() - started < 1_000_000_000L);
    }

    @Test public void peerCloseAcknowledgesAndWakesConsumerAfterQueuedFrames() throws Exception {
        WebTunnel.FrameQueue frames = new WebTunnel.FrameQueue();
        for (int i = 0; i < WebTunnel.MAX_QUEUED_FRAMES; i++)
            assertTrue(frames.offer(WebTunnel.Frame.text("frame-" + i)));
        AtomicInteger closeCalls = new AtomicInteger();
        WebSocket peer = (WebSocket) Proxy.newProxyInstance(WebSocket.class.getClassLoader(),
                new Class<?>[]{WebSocket.class}, (proxy, method, args) -> {
                    if ("close".equals(method.getName())) {
                        assertEquals(1000, args[0]);
                        assertEquals("peer finished", args[1]);
                        closeCalls.incrementAndGet();
                        return true;
                    }
                    if (method.getReturnType() == boolean.class) return false;
                    if (method.getReturnType() == long.class) return 0L;
                    if (method.getReturnType() == int.class) return 0;
                    return null;
                });

        WebTunnel.acknowledgePeerClose(peer, 1000, "peer finished", frames);
        WebTunnel.acknowledgePeerClose(peer, 1000, "peer finished", frames);
        for (int i = 0; i < WebTunnel.MAX_QUEUED_FRAMES; i++) {
            WebTunnel.Frame frame = frames.poll(10, TimeUnit.MILLISECONDS);
            assertNotNull(frame);
            assertFalse(frame.end);
            assertEquals("frame-" + i, frame.text);
        }
        WebTunnel.Frame terminal = frames.poll(10, TimeUnit.MILLISECONDS);
        assertNotNull("peer close must wake a blocked consumer", terminal);
        assertTrue(terminal.end);
        assertNull("repeated peer close must not enqueue duplicate end markers",
                frames.poll(10, TimeUnit.MILLISECONDS));
        assertEquals(2, closeCalls.get());
    }

    @Test public void peerCloseReleasesAConsumerWaitingOnAnEmptyFrameQueue() throws Exception {
        WebTunnel.FrameQueue frames = new WebTunnel.FrameQueue();
        CountDownLatch polling = new CountDownLatch(1);
        CompletableFuture<WebTunnel.Frame> next = new CompletableFuture<>();
        Thread consumer = new Thread(() -> {
            try {
                polling.countDown();
                next.complete(frames.poll(5, TimeUnit.SECONDS));
            } catch (InterruptedException error) {
                Thread.currentThread().interrupt();
                next.completeExceptionally(error);
            }
        });
        consumer.start();
        assertTrue(polling.await(1, TimeUnit.SECONDS));

        WebSocket peer = (WebSocket) Proxy.newProxyInstance(WebSocket.class.getClassLoader(),
                new Class<?>[]{WebSocket.class}, (proxy, method, args) ->
                        "close".equals(method.getName()) || method.getReturnType() == boolean.class);
        WebTunnel.acknowledgePeerClose(peer, 1000, "peer finished", frames);

        WebTunnel.Frame terminal = next.get(1, TimeUnit.SECONDS);
        assertNotNull(terminal);
        assertTrue(terminal.end);
        consumer.join(1000);
        assertFalse(consumer.isAlive());
    }

    @Test public void emptyPeerCloseUsesLegalAckCodeAndWakesConsumer() throws Exception {
        WebTunnel.FrameQueue frames = new WebTunnel.FrameQueue();
        AtomicInteger ackCode = new AtomicInteger();
        WebSocket peer = (WebSocket) Proxy.newProxyInstance(WebSocket.class.getClassLoader(),
                new Class<?>[]{WebSocket.class}, (proxy, method, args) -> {
                    if ("close".equals(method.getName())) {
                        ackCode.set((Integer) args[0]);
                        assertEquals("", args[1]);
                        return true;
                    }
                    if (method.getReturnType() == boolean.class) return false;
                    if (method.getReturnType() == long.class) return 0L;
                    if (method.getReturnType() == int.class) return 0;
                    return null;
                });

        WebTunnel.acknowledgePeerClose(peer, 1005, "", frames);

        assertEquals(1001, ackCode.get());
        WebTunnel.Frame terminal = frames.poll(10, TimeUnit.MILLISECONDS);
        assertNotNull("empty close must wake a blocked consumer", terminal);
        assertTrue(terminal.end);
    }
}
