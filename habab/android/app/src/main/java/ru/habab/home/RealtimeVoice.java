package ru.habab.home;

import android.content.Context;
import android.media.*;
import android.util.Base64;
import java.io.IOException;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicLong;
import okhttp3.*;
import org.json.*;

/** Native PCM transport. Master provider credentials never enter this class. */
final class RealtimeVoice implements AutoCloseable {
    interface Listener { void phase(String value); void completed(); void failed(String code); }
    private final VoiceCredentials credentials;
    private final Listener listener;
    private final OkHttpClient client = new OkHttpClient.Builder().readTimeout(0, TimeUnit.SECONDS)
            .connectTimeout(15, TimeUnit.SECONDS).pingInterval(20, TimeUnit.SECONDS).build();
    private final ExecutorService tools = Executors.newSingleThreadExecutor();
    private final ExecutorService playback = Executors.newSingleThreadExecutor();
    private final AtomicLong queuedAudio = new AtomicLong();
    private final PcmResampler resampler = new PcmResampler();
    private final Object outputLock = new Object();
    private volatile boolean closed, ready;
    private volatile WebSocket socket;
    private AudioTrack track;
    private String sessionId, outputItem;
    private int generation, toolCalls;
    private long playbackStart;
    private long writtenFrames;

    RealtimeVoice(Context context, Listener listener) {
        this.credentials = new VoiceCredentials(context); this.listener = listener;
    }

    void start() throws Exception {
        JSONObject connection = credentials.call("session", new JSONObject());
        sessionId = connection.getString("session_id");
        int size = Math.max(AudioTrack.getMinBufferSize(24000, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT), 9600);
        track = new AudioTrack.Builder().setAudioAttributes(new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                .setAudioFormat(new AudioFormat.Builder().setSampleRate(24000).setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                .setBufferSizeInBytes(size).setTransferMode(AudioTrack.MODE_STREAM).build();
        if (track.getState() != AudioTrack.STATE_INITIALIZED) throw new IOException("voice_playback_unavailable");
        track.play();
        String address = connection.getString("websocket_url");
        java.net.URI uri = new java.net.URI(address);
        if (!"wss".equals(uri.getScheme()) || uri.getHost() == null || uri.getUserInfo() != null) throw new IOException("invalid_voice_endpoint");
        Request request = new Request.Builder().url(address)
                .header("Authorization", "Bearer " + connection.getString("value")).build();
        socket = client.newWebSocket(request, new WebSocketListener() {
            @Override public void onMessage(WebSocket ws, String text) {
                try { event(new JSONObject(text)); }
                catch (Exception error) { fail("invalid_realtime_event"); }
            }
            @Override public void onFailure(WebSocket ws, Throwable error, Response response) { fail("realtime_connection_failed"); }
            @Override public void onClosed(WebSocket ws, int code, String reason) { if (!closed) fail("realtime_connection_closed"); }
        });
    }

    boolean ready() { return ready && socket != null && !closed; }

    synchronized int append(short[] samples, int count) throws Exception {
        if (!ready()) return 0;
        if (socket.queueSize() > 512 * 1024) { fail("realtime_upload_backpressure"); return 0; }
        byte[] audio = resampler.convert(samples, count);
        send(new JSONObject().put("type", "input_audio_buffer.append").put("audio", Base64.encodeToString(audio, Base64.NO_WRAP)));
        return audio.length;
    }

    private void send(JSONObject event) {
        WebSocket current = socket;
        if (!closed && current != null && !current.send(event.toString())) fail("realtime_send_failed");
    }

    private void event(JSONObject event) throws Exception {
        String type = event.optString("type");
        if (type.equals("session.created")) { ready = true; listener.phase("listening"); }
        else if (type.equals("input_audio_buffer.speech_started")) {
            interruptOutput(); listener.phase("speech");
        } else if (type.equals("input_audio_buffer.speech_stopped")) listener.phase("thinking");
        else if (type.equals("response.output_audio.delta")) queueAudio(event);
        else if (type.equals("response.function_call_arguments.done")) {
            if (!event.optString("name").equals("ask_home_assistant") || ++toolCalls > 8) { fail("invalid_voice_tool"); return; }
            String callId = event.getString("call_id");
            JSONObject arguments = new JSONObject(event.getString("arguments"));
            String text = arguments.getString("text");
            if (text.trim().isEmpty() || text.length() > 4000) { fail("invalid_voice_command"); return; }
            tools.execute(() -> {
                try {
                    listener.phase("thinking");
                    JSONObject result = credentials.call("turn", new JSONObject().put("session_id", sessionId).put("text", text));
                    if (closed) return;
                    JSONObject item = new JSONObject().put("type", "function_call_output").put("call_id", callId)
                            .put("output", new JSONObject().put("answer", result.getString("text")).toString());
                    send(new JSONObject().put("type", "conversation.item.create").put("item", item));
                    send(new JSONObject().put("type", "response.create").put("response", new JSONObject()
                            .put("tool_choice", "none").put("instructions", "Озвучь ответ инструмента кратко и точно, не добавляя действий или неподтверждённых фактов.")));
                } catch (Exception error) { fail("voice_assistant_failed"); }
            });
        } else if (type.equals("response.output_audio.done")) {
            final int version;
            synchronized (outputLock) { version = generation; }
            playback.execute(() -> {
                long deadline = android.os.SystemClock.elapsedRealtime() + 15000;
                while (!closed && android.os.SystemClock.elapsedRealtime() < deadline) {
                    synchronized (outputLock) {
                        if (version != generation || track == null) return;
                        long played = Math.max(0, Integer.toUnsignedLong(track.getPlaybackHeadPosition()) - playbackStart);
                        if (played >= writtenFrames) { listener.completed(); listener.phase("listening"); return; }
                    }
                    try { Thread.sleep(20); } catch (InterruptedException error) { Thread.currentThread().interrupt(); return; }
                }
                if (!closed) fail("voice_playback_timeout");
            });
        } else if (type.equals("response.done") && event.optJSONObject("response") != null
                && event.getJSONObject("response").optString("status").equals("failed")) {
            fail("realtime_response_failed");
        } else if (type.equals("error")) fail("realtime_provider_error");
    }

    private void queueAudio(JSONObject event) throws Exception {
        byte[] audio = Base64.decode(event.getString("delta"), Base64.DEFAULT);
        if ((audio.length & 1) != 0 || queuedAudio.addAndGet(audio.length) > 3 * 1024 * 1024) {
            fail("realtime_playback_backpressure"); return;
        }
        int version;
        synchronized (outputLock) {
            String item = event.getString("item_id");
            if (closed || track == null) return;
            if (!item.equals(outputItem)) { outputItem = item; playbackStart = Integer.toUnsignedLong(track.getPlaybackHeadPosition()); writtenFrames = 0; }
            version = generation;
        }
        listener.phase("speaking");
        playback.execute(() -> {
            try {
                int offset = 0;
                while (!closed && offset < audio.length) {
                    synchronized (outputLock) {
                        if (version != generation) break;
                        int count = track.write(audio, offset, Math.min(4096, audio.length - offset), AudioTrack.WRITE_BLOCKING);
                        if (count <= 0) { fail("voice_playback_failed"); break; }
                        offset += count;
                        writtenFrames += count / 2;
                    }
                }
            } finally { queuedAudio.addAndGet(-audio.length); }
        });
    }

    private void interruptOutput() throws Exception {
        synchronized (outputLock) {
            if (closed || track == null) return;
            generation++;
            if (outputItem != null) {
                long frames = Math.max(0, Integer.toUnsignedLong(track.getPlaybackHeadPosition()) - playbackStart);
                send(new JSONObject().put("type", "conversation.item.truncate").put("item_id", outputItem)
                        .put("content_index", 0).put("audio_end_ms", frames * 1000 / 24000));
                outputItem = null;
            }
            track.pause(); track.flush(); track.play();
        }
    }

    private void fail(String code) { if (!closed) { listener.failed(code); close(); } }

    @Override public void close() {
        if (closed) return; closed = true; ready = false;
        WebSocket current = socket; if (current != null) current.close(1000, "voice session ended");
        tools.shutdownNow(); playback.shutdownNow();
        synchronized (outputLock) { if (track != null) { track.pause(); track.flush(); track.release(); track = null; } }
        client.dispatcher().executorService().shutdown(); client.connectionPool().evictAll();
    }
}
