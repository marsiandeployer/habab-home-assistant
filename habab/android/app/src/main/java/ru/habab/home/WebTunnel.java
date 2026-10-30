package ru.habab.home;

import android.content.Context;
import android.content.SharedPreferences;
import java.io.IOException;
import java.net.URI;
import java.net.Proxy;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import okhttp3.Call;
import okhttp3.Headers;
import okhttp3.HttpUrl;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;
import okhttp3.ResponseBody;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;
import okio.BufferedSink;
import okio.ByteString;
import okio.BufferedSource;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/** Outbound-only HTTP/WebSocket bridge to the per-home product relay. */
final class WebTunnel {
    static volatile String status = "Выключен";
    static final String PREFS = "remote_access";
    static final String CONTROL_PATH = "/_habab_tunnel/control";
    static final String DATA_PATH = "/_habab_tunnel/data/";
    static final String LOCAL_ORIGIN = "http://127.0.0.1:8123";
    static final int MAX_STREAMS = 16;
    static final int MAX_BODY = 32 * 1024 * 1024;
    static final int MAX_WS_MESSAGE = 8 * 1024 * 1024;
    static final int CHUNK = 64 * 1024;
    private static final int MAX_QUEUED_FRAMES = 64;
    private static final long MAX_QUEUED_BYTES = 16L * 1024 * 1024;
    private static final Set<String> HOP_HEADERS = new HashSet<>(Arrays.asList(
            "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
            "te", "trailer", "transfer-encoding", "upgrade", "content-length"));

    private final SharedPreferences prefs;
    private final AtomicBoolean closed = new AtomicBoolean();
    private final AtomicBoolean running = new AtomicBoolean();
    private final ConcurrentHashMap<String, Stream> streams = new ConcurrentHashMap<>();
    private final ExecutorService workers = Executors.newFixedThreadPool(MAX_STREAMS);
    private final OkHttpClient client = new OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(180, TimeUnit.SECONDS)
            .writeTimeout(180, TimeUnit.SECONDS)
            .followRedirects(false)
            .followSslRedirects(false)
            .pingInterval(20, TimeUnit.SECONDS)
            .build();
    private final OkHttpClient localClient = client.newBuilder().proxy(Proxy.NO_PROXY).build();
    private volatile WebSocket control;
    private volatile Thread runner;
    private final Context context;

    WebTunnel(Context context) {
        this.context = context.getApplicationContext();
        prefs = this.context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    /** Runs on the caller's background thread until close() or server shutdown. */
    void run() {
        if (!running.compareAndSet(false, true)) return;
        runner = Thread.currentThread();
        int delay = 1;
        try {
            while (!closed.get() && prefs.getBoolean("enabled", false)) {
                if (!coreReady()) {
                    status = "Ожидание Home Assistant";
                    if (pause(1000)) break;
                    continue;
                }
                if (!hasOwner()) {
                    status = "Ожидание владельца";
                    if (pause(5000)) break;
                    continue;
                }
                WebSocket socket = null;
                try {
                    Config config = Config.from(prefs);
                    status = "Подключение";
                    ControlListener listener = new ControlListener(config);
                    Request request = authorized(config.relay, config.home, config.token).build();
                    socket = client.newWebSocket(request, listener);
                    control = socket;
                    String first = listener.ready.get(20, TimeUnit.SECONDS);
                    if (!"ready".equals(new JSONObject(first).optString("type")))
                        throw new IOException("relay handshake failed");
                    status = "Подключено";
                    delay = 1;
                    listener.awaitClosed();
                } catch (InterruptedException error) {
                    Thread.currentThread().interrupt();
                    break;
                } catch (Exception ignored) {
                    // Exceptions can contain URLs or headers; never log tunnel credentials.
                    status = "Переподключение";
                } finally {
                    if (socket != null) socket.close(1001, "reconnecting");
                    if (control == socket) control = null;
                    cancelStreams();
                }
                if (!closed.get() && prefs.getBoolean("enabled", false)) {
                    if (pause(delay * 1000L)) break;
                    delay = Math.min(delay * 2, 30);
                }
            }
        } finally {
            cancelStreams();
            if (closed.get() || !prefs.getBoolean("enabled", false)) status = "Выключен";
            runner = null;
            running.set(false);
        }
    }

    private static boolean pause(long millis) {
        try { Thread.sleep(millis); return false; }
        catch (InterruptedException error) { Thread.currentThread().interrupt(); return true; }
    }

    private boolean coreReady() { return ServerService.active && "running".equals(ServerService.status); }

    private boolean hasOwner() {
        try {
            java.io.File onboarding = new java.io.File(new RuntimeStore(context).config(), ".storage/onboarding");
            byte[] raw = java.nio.file.Files.readAllBytes(onboarding.toPath());
            JSONObject data = new JSONObject(new String(raw, java.nio.charset.StandardCharsets.UTF_8));
            JSONArray done = data.getJSONObject("data").getJSONArray("done");
            for (int i = 0; i < done.length(); i++) if ("user".equals(done.getString(i))) return true;
        } catch (Exception ignored) { }
        return false;
    }

    void close() {
        if (!closed.compareAndSet(false, true)) return;
        WebSocket socket = control;
        if (socket != null) socket.close(1001, "service stopped");
        Thread activeRunner = runner;
        if (activeRunner != null) activeRunner.interrupt();
        cancelStreams();
        workers.shutdownNow();
        client.dispatcher().cancelAll();
        client.connectionPool().evictAll();
        status = "Выключен";
    }

    String publicUrl() { return prefs.getString("public_url", ""); }

    private void cancelStreams() {
        for (Stream stream : streams.values()) stream.cancel();
        streams.clear();
    }

    private final class ControlListener extends WebSocketListener {
        final Config config;
        final CompletableFuture<String> ready = new CompletableFuture<>();
        private final BlockingQueue<String> incoming = new ArrayBlockingQueue<>(MAX_QUEUED_FRAMES);
        private volatile boolean ended;

        ControlListener(Config config) { this.config = config; }

        @Override public void onMessage(WebSocket webSocket, String text) {
            if (text.length() > CHUNK) { webSocket.close(1009, "control message too large"); ended = true; return; }
            if (!ready.isDone()) { ready.complete(text); return; }
            if (!incoming.offer(text)) {
                webSocket.close(1009, "control queue full");
                ended = true;
            }
        }

        @Override public void onClosed(WebSocket webSocket, int code, String reason) {
            ended = true;
            ready.completeExceptionally(new IOException("relay closed"));
        }

        @Override public void onFailure(WebSocket webSocket, Throwable error, Response response) {
            ended = true;
            ready.completeExceptionally(new IOException("relay connection failed"));
        }

        void awaitClosed() throws Exception {
            while (!closed.get() && !ended) {
                String message = incoming.poll(1, TimeUnit.SECONDS);
                if (message == null) continue;
                JSONObject data = new JSONObject(message);
                String streamId = data.optString("stream", "");
                if (!"open".equals(data.optString("type")) || !streamId.matches("[a-f0-9]{48}"))
                    throw new IOException("invalid relay instruction");
                if (streams.size() >= MAX_STREAMS) continue;
                Stream stream = new Stream(streamId, config);
                if (streams.putIfAbsent(streamId, stream) == null) {
                    try { workers.execute(() -> serve(stream)); }
                    catch (RuntimeException rejected) { streams.remove(streamId, stream); stream.cancel(); }
                }
            }
        }
    }

    private final class Stream {
        final String id;
        final Config config;
        final BlockingQueue<Frame> frames = new ArrayBlockingQueue<>(MAX_QUEUED_FRAMES);
        final CompletableFuture<JSONObject> request = new CompletableFuture<>();
        final CompletableFuture<Void> dataEnded = new CompletableFuture<>();
        final AtomicBoolean cancelled = new AtomicBoolean();
        final AtomicBoolean dataFailed = new AtomicBoolean();
        final java.util.concurrent.atomic.AtomicLong queuedBytes = new java.util.concurrent.atomic.AtomicLong();
        volatile boolean websocket;
        volatile WebSocket data;
        volatile Call call;
        volatile WebSocket localSocket;
        Stream(String id, Config config) { this.id = id; this.config = config; }
        void cancel() {
            if (!cancelled.compareAndSet(false, true)) return;
            request.completeExceptionally(new IOException("stream cancelled"));
            Call currentCall = call; if (currentCall != null) currentCall.cancel();
            WebSocket currentData = data; if (currentData != null) currentData.cancel();
            WebSocket currentLocal = localSocket; if (currentLocal != null) currentLocal.cancel();
        }
        boolean send(String value) throws InterruptedException {
            WebSocket ws = data;
            if (ws == null || cancelled.get() || value.getBytes(StandardCharsets.UTF_8).length > MAX_WS_MESSAGE + CHUNK) return false;
            while (!cancelled.get() && ws.queueSize() > 8L * 1024 * 1024) Thread.sleep(10);
            return !cancelled.get() && ws.send(value);
        }
        void dataClose(int code, String reason) { WebSocket ws = data; if (ws != null) ws.close(code, reason); }
        boolean finishNormally() {
            WebSocket ws = data;
            if (ws == null || dataFailed.get()) return false;
            return gracefulDataClose(ws, dataEnded, 5000) && !dataFailed.get();
        }
        boolean send(ByteString value) throws InterruptedException {
            WebSocket ws = data;
            if (ws == null || cancelled.get()) return false;
            while (!cancelled.get() && ws.queueSize() > 8L * 1024 * 1024) Thread.sleep(10);
            return !cancelled.get() && ws.send(value);
        }
        boolean offerFrame(Frame frame) {
            if (frame.end) return frames.offer(frame);
            long bytes = frame.bytes != null ? frame.bytes.size() : frame.text.getBytes(StandardCharsets.UTF_8).length;
            long total = queuedBytes.addAndGet(bytes);
            if (total > MAX_QUEUED_BYTES || !frames.offer(frame)) {
                queuedBytes.addAndGet(-bytes);
                return false;
            }
            return true;
        }
        void consumed(Frame frame) {
            if (frame.bytes != null) queuedBytes.addAndGet(-frame.bytes.size());
            else if (frame.text != null) queuedBytes.addAndGet(-frame.text.getBytes(StandardCharsets.UTF_8).length);
        }
    }

    private static final class Frame {
        final ByteString bytes;
        final String text;
        final boolean end;
        Frame(ByteString bytes, String text, boolean end) { this.bytes = bytes; this.text = text; this.end = end; }
        static Frame bytes(ByteString value) { return new Frame(value, null, false); }
        static Frame text(String value) { return new Frame(null, value, false); }
        static Frame end() { return new Frame(null, null, true); }
    }

    private void serve(Stream stream) {
        try {
            HttpUrl dataUrl = stream.config.dataUrl(stream.id);
            Request request = authorized(dataUrl, stream.config.home, stream.config.token).build();
            CompletableFuture<WebSocket> opened = new CompletableFuture<>();
            WebSocket socket = client.newWebSocket(request, new WebSocketListener() {
                @Override public void onOpen(WebSocket webSocket, Response response) { opened.complete(webSocket); }
                @Override public void onMessage(WebSocket webSocket, String text) {
                    try {
                        if (!stream.request.isDone() && text.length() > CHUNK) {
                            stream.cancel();
                            return;
                        }
                        JSONObject data = new JSONObject(text);
                        if (!stream.request.isDone() && "request".equals(data.optString("type"))) {
                            stream.websocket = data.optBoolean("websocket", false);
                            stream.request.complete(data);
                        } else if ("end".equals(data.optString("type")) && stream.request.isDone()) {
                            if (!stream.offerFrame(Frame.end())) stream.cancel();
                        } else if (stream.request.isDone()) {
                    if (text.getBytes(StandardCharsets.UTF_8).length > MAX_WS_MESSAGE + CHUNK
                                    || !stream.offerFrame(Frame.text(text))) stream.cancel();
                        } else stream.request.completeExceptionally(new IOException("invalid request"));
                    } catch (JSONException error) { stream.cancel(); }
                }
                @Override public void onMessage(WebSocket webSocket, ByteString bytes) {
                    int max = stream.websocket ? MAX_WS_MESSAGE : CHUNK;
                    if (bytes.size() > max || !stream.offerFrame(Frame.bytes(bytes))) stream.cancel();
                }
                @Override public void onClosed(WebSocket webSocket, int code, String reason) {
                    opened.completeExceptionally(new IOException("data socket closed"));
                    stream.dataEnded.complete(null);
                    stream.request.completeExceptionally(new IOException("data socket closed"));
                    stream.offerFrame(Frame.end());
                }
                @Override public void onFailure(WebSocket webSocket, Throwable error, Response response) {
                opened.completeExceptionally(new IOException("data socket failed"));
                    stream.dataFailed.set(true);
                    stream.dataEnded.complete(null);
                    stream.request.completeExceptionally(new IOException("data socket failed"));
                    stream.offerFrame(Frame.end());
                }
            });
            stream.data = socket;
                    opened.get(15, TimeUnit.SECONDS);
            JSONObject message = stream.request.get(15, TimeUnit.SECONDS);
            String path = message.getString("path");
            HttpUrl target = localUrl(path);
            boolean websocket = message.optBoolean("websocket", false);
            Headers headers = headers(message.optJSONArray("headers"), websocket);
            if (websocket) serveWebSocket(stream, target, headers, message.optString("protocols", ""));
            else serveHttp(stream, target, headers, message.getString("method"));
        } catch (Exception ignored) {
            stream.dataClose(1011, "local request failed");
        } finally {
            // Successful handlers wait for the peer close before reaching this cancellation.
            stream.cancel();
            streams.remove(stream.id, stream);
        }
    }

    private void serveHttp(Stream stream, HttpUrl url, Headers headers, String method) throws Exception {
        RequestBody body = permitsBody(method) ? new RequestBody() {
            @Override public MediaType contentType() { return null; }
            @Override public long contentLength() { return -1; }
            @Override public void writeTo(BufferedSink sink) throws IOException {
                long total = 0;
                try {
                    while (!stream.cancelled.get()) {
                        Frame frame = stream.frames.poll(180, TimeUnit.SECONDS);
                        if (frame == null) throw new IOException("request body timeout");
                        stream.consumed(frame);
                        if (frame.end) return;
                        if (frame.bytes == null) throw new IOException("unexpected request frame");
                        total += frame.bytes.size();
                        if (total > MAX_BODY) throw new IOException("request body too large");
                        sink.write(frame.bytes);
                    }
                } catch (InterruptedException error) {
                    Thread.currentThread().interrupt(); throw new IOException("request cancelled");
                }
            }
        } : null;
        if (headers.get("Accept-Encoding") == null)
            headers = headers.newBuilder().set("Accept-Encoding", "identity").build();
        Request.Builder builder = new Request.Builder().url(url).headers(headers).method(method, body);
        stream.call = localClient.newCall(builder.build());
        try (Response response = stream.call.execute()) {
            forwardResponse(stream, response.code(), response.headers(), response.body());
        }
    }

    private void forwardMetadata(Stream stream, int statusCode, Headers headers, boolean websocket) throws Exception {
        JSONArray responseHeaders = headerPairs(headers, websocket);
        if (!stream.send(new JSONObject().put("type", "response").put("status", statusCode)
                .put("headers", responseHeaders).toString())) throw new IOException("data socket closed");
    }

    private void forwardResponse(Stream stream, int statusCode, Headers headers, ResponseBody body) throws Exception {
        forwardMetadata(stream, statusCode, headers, false);
        if (body != null) {
            BufferedSource source = body.source();
            byte[] buffer = new byte[CHUNK];
            long total = 0;
            while (!stream.cancelled.get()) {
                int count = source.read(buffer, 0, buffer.length);
                if (count == -1) break;
                total += count;
                if (total > MAX_BODY) throw new IOException("response too large");
                if (!stream.send(ByteString.of(buffer, 0, count))) throw new IOException("data socket closed");
            }
        }
        if (!stream.send("{\"type\":\"end\"}")) throw new IOException("data socket closed");
        stream.finishNormally();
    }

    private void serveWebSocket(Stream stream, HttpUrl url, Headers headers, String protocols) throws Exception {
        Headers upstreamHeaders = withProtocols(headers, protocols);
        if (upstreamHeaders.get("Accept-Encoding") == null)
            upstreamHeaders = upstreamHeaders.newBuilder().set("Accept-Encoding", "identity").build();
        Request.Builder builder = new Request.Builder().url(url).headers(upstreamHeaders);
        CompletableFuture<Boolean> opened = new CompletableFuture<>();
        stream.localSocket = localClient.newWebSocket(builder.build(), new WebSocketListener() {
            @Override public void onOpen(WebSocket webSocket, Response response) {
                try {
                    if (!stream.send(new JSONObject().put("type", "response").put("status", 101)
                            .put("protocol", response.header("Sec-WebSocket-Protocol") == null
                                    ? JSONObject.NULL : response.header("Sec-WebSocket-Protocol")).toString()))
                        throw new IOException("data socket closed");
                    opened.complete(true);
                } catch (Exception error) { opened.completeExceptionally(new IOException("tunnel response failed")); }
            }
            @Override public void onMessage(WebSocket webSocket, String text) {
                try {
                    if (text.getBytes(StandardCharsets.UTF_8).length > MAX_WS_MESSAGE) { webSocket.close(1009, "message too large"); return; }
                    String frame = new JSONObject().put("type", "text").put("data", text).toString();
                    if (frame.getBytes(StandardCharsets.UTF_8).length > MAX_WS_MESSAGE + CHUNK) {
                        webSocket.close(1009, "message too large"); return;
                    }
                    stream.send(frame);
                }
                catch (InterruptedException error) { Thread.currentThread().interrupt(); webSocket.cancel(); }
                catch (JSONException error) { webSocket.cancel(); }
            }
            @Override public void onMessage(WebSocket webSocket, ByteString bytes) {
                try { stream.send(bytes); } catch (InterruptedException error) { Thread.currentThread().interrupt(); webSocket.cancel(); }
            }
            @Override public void onClosing(WebSocket webSocket, int code, String reason) {
                try { stream.send(new JSONObject().put("type", "close").put("code", code).put("reason", reason).toString()); }
                catch (InterruptedException error) { Thread.currentThread().interrupt(); }
                catch (JSONException ignored) { }
            }
            @Override public void onClosed(WebSocket webSocket, int code, String reason) { }
            @Override public void onFailure(WebSocket webSocket, Throwable error, Response response) {
                if (response == null) {
                    stream.dataFailed.set(true);
                    stream.dataEnded.complete(null);
                    opened.completeExceptionally(new IOException("local websocket failed"));
                    stream.cancel();
                    return;
                }
                try {
                    forwardMetadata(stream, response.code(), response.headers(), true);
                    stream.send("{\"type\":\"end\"}");
                    opened.complete(false);
                } catch (Exception ignored) {
                    opened.completeExceptionally(new IOException("local websocket response failed"));
                    stream.cancel();
                } finally {
                    if (response.body() != null) response.body().close();
                }
            }
        });
        if (!opened.get(15, TimeUnit.SECONDS)) {
            stream.finishNormally();
            return;
        }
        WebSocket upstream = stream.localSocket;
        if (upstream == null) throw new IOException("local websocket unavailable");
        while (!stream.cancelled.get()) {
            Frame frame = stream.frames.poll(180, TimeUnit.SECONDS);
            if (frame == null) throw new IOException("websocket tunnel timed out");
            if (frame.end) break;
            stream.consumed(frame);
            if (frame.bytes != null) {
                if (frame.bytes.size() > MAX_WS_MESSAGE) throw new IOException("websocket message too large");
                upstream.send(frame.bytes);
            } else if (frame.text != null) {
                JSONObject item = new JSONObject(frame.text);
                if ("text".equals(item.optString("type"))) {
                    String text = item.getString("data");
                    if (text.getBytes(StandardCharsets.UTF_8).length > MAX_WS_MESSAGE) throw new IOException("websocket message too large");
                    upstream.send(text);
                } else if ("close".equals(item.optString("type"))) {
                    int code = item.optInt("code", 1000);
                    if (code == 1005 || code == 1006 || code == 1015) code = 1001;
                    upstream.close(code, item.optString("reason", ""));
                    break;
                } else throw new IOException("invalid websocket frame");
            }
        }
        upstream.close(1000, "tunnel closed");
        stream.finishNormally();
    }

    private static Headers withProtocols(Headers source, String protocols) {
        Headers.Builder result = source.newBuilder();
        if (!protocols.isEmpty()) result.set("Sec-WebSocket-Protocol", protocols);
        return result.build();
    }

    static HttpUrl localUrl(String path) throws IOException {
        if (path == null || !path.startsWith("/") || path.startsWith("//") || path.indexOf('\\') >= 0
                || path.indexOf('#') >= 0 || hasControl(path)) throw new IOException("invalid origin-form path");
        HttpUrl url = HttpUrl.parse(LOCAL_ORIGIN + path);
        if (url == null || !"127.0.0.1".equals(url.host()) || url.port() != 8123)
            throw new IOException("invalid local path");
        return url;
    }

    static HttpUrl validateRelay(String value) throws IOException {
        URI uri;
        try { uri = value == null ? null : new URI(value); }
        catch (Exception error) { throw new IOException("invalid relay URL"); }
        if (uri == null || !"wss".equalsIgnoreCase(uri.getScheme()) || uri.getHost() == null
                || uri.getRawUserInfo() != null || uri.getRawQuery() != null || uri.getRawFragment() != null
                || !CONTROL_PATH.equals(uri.getRawPath())) throw new IOException("invalid relay URL");
        HttpUrl url = HttpUrl.parse("https" + value.substring(3));
        if (url == null || !"https".equals(url.scheme()) || url.username().length() != 0
                || url.password().length() != 0 || url.query() != null || url.fragment() != null
                || !CONTROL_PATH.equals(url.encodedPath())) throw new IOException("invalid relay URL");
        return url;
    }

    static HttpUrl dataUrl(HttpUrl relay, String stream) throws IOException {
        if (relay == null || stream == null || !stream.matches("[a-f0-9]{48}"))
            throw new IOException("invalid data stream");
        return relay.newBuilder().encodedPath(DATA_PATH + stream).build();
    }

    static Headers headers(JSONArray pairs, boolean websocket) throws IOException {
        Headers.Builder result = new Headers.Builder();
        Set<String> blocked = new HashSet<>(HOP_HEADERS);
        blocked.add("content-length");
        if (websocket) blocked.addAll(Arrays.asList("sec-websocket-key", "sec-websocket-version",
                "sec-websocket-extensions", "sec-websocket-accept", "sec-websocket-protocol"));
        if (pairs == null) return result.build();
        for (int i = 0; i < pairs.length(); i++) {
            JSONArray pair = pairs.optJSONArray(i);
            if (pair != null && pair.length() == 2 && "connection".equalsIgnoreCase(pair.optString(0))) {
                for (String token : pair.optString(1, "").split(","))
                    if (!token.trim().isEmpty()) blocked.add(token.trim().toLowerCase(Locale.ROOT));
            }
        }
        for (int i = 0; i < pairs.length(); i++) {
            JSONArray pair = pairs.optJSONArray(i);
            if (pair == null || pair.length() != 2) continue;
            String name = pair.optString(0, ""), value = pair.optString(1, "");
            if (name.isEmpty() || blocked.contains(name.toLowerCase(Locale.ROOT))
                    || isForwardedHeader(name)) continue;
            try { result.add(name, value); }
            catch (IllegalArgumentException ignored) { /* Malformed relay headers are dropped. */ }
        }
        return result.build();
    }

    private static Request.Builder authorized(HttpUrl url, String home, String token) {
        return new Request.Builder().url(url).header("Authorization", "Bearer " + token)
                .header("X-Habab-Home", home);
    }

    private static boolean permitsBody(String method) {
        return !"GET".equalsIgnoreCase(method) && !"HEAD".equalsIgnoreCase(method);
    }

    private static JSONArray headerPairs(Headers headers, boolean websocket) throws JSONException {
        JSONArray result = new JSONArray();
        Set<String> blocked = new HashSet<>(HOP_HEADERS);
        blocked.remove("content-length");
        if (websocket) blocked.addAll(Arrays.asList("sec-websocket-key", "sec-websocket-version",
                "sec-websocket-extensions", "sec-websocket-accept", "sec-websocket-protocol", "content-length"));
        for (int i = 0; i < headers.size(); i++) if ("connection".equalsIgnoreCase(headers.name(i)))
            for (String token : headers.value(i).split(","))
                if (!token.trim().isEmpty()) blocked.add(token.trim().toLowerCase(Locale.ROOT));
        for (int i = 0; i < headers.size(); i++) {
            String name = headers.name(i);
            if (blocked.contains(name.toLowerCase(Locale.ROOT)) || isForwardedHeader(name)) continue;
            result.put(new JSONArray().put(name).put(headers.value(i)));
        }
        return result;
    }

    private static boolean hasControl(String value) {
        for (int i = 0; i < value.length(); i++) if (value.charAt(i) < 0x20 || value.charAt(i) == 0x7f) return true;
        return false;
    }

    private static boolean isForwardedHeader(String name) {
        return name.equalsIgnoreCase("forwarded") || name.equalsIgnoreCase("x-habab-home")
                || name.equalsIgnoreCase("x-real-ip")
                || name.toLowerCase(Locale.ROOT).startsWith("x-forwarded-");
    }

    static boolean gracefulDataClose(WebSocket ws, CompletableFuture<Void> ended, long timeoutMillis) {
        if (ws == null || ended == null || timeoutMillis < 1) return false;
        if (!ended.isDone()) ws.close(1000, "stream complete");
        try {
            ended.get(timeoutMillis, TimeUnit.MILLISECONDS);
            return true;
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            return false;
        } catch (Exception ignored) { return false; }
    }

    private static final class Config {
        final HttpUrl relay;
        final String home;
        final String token;
        Config(HttpUrl relay, String home, String token) { this.relay = relay; this.home = home; this.token = token; }
        static Config from(SharedPreferences prefs) throws IOException {
            HttpUrl relay = validateRelay(prefs.getString("relay_url", ""));
            String home = prefs.getString("home", "");
            String token = prefs.getString("token", "");
            if (home.length() > 253 || !validHostname(home)
                    || token.length() < 32 || !token.matches("[\\x21-\\x7e]+"))
                throw new IOException("invalid home credentials");
            return new Config(relay, home, token);
        }
        HttpUrl dataUrl(String stream) {
            try { return WebTunnel.dataUrl(relay, stream); }
            catch (IOException impossible) { throw new IllegalArgumentException("invalid stream id"); }
        }
    }

    private static boolean validHostname(String value) {
        if (value == null || value.isEmpty()) return false;
        for (String label : value.split("\\.", -1)) {
            if (label.isEmpty() || label.length() > 63 || !label.matches("[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"))
                return false;
        }
        return true;
    }
}
