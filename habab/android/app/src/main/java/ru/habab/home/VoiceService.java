package ru.habab.home;

import android.Manifest;
import android.app.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.media.*;
import android.media.audiofx.AcousticEchoCanceler;
import android.media.audiofx.NoiseSuppressor;
import android.os.*;
import java.io.File;
import java.nio.charset.StandardCharsets;
import java.util.ArrayDeque;
import org.json.JSONObject;

/** Persistent native microphone; no idle-room audio is sent to the network. */
public final class VoiceService extends Service {
    static volatile boolean active;
    static volatile String status = "off";
    static volatile String lastError;
    private volatile long lastErrorAt;
    private volatile boolean running;
    private Thread capture;
    private volatile AudioRecord recorder;
    private volatile RealtimeVoice realtime;
    private volatile String failure;
    private volatile boolean endRequested;
    private volatile long lastActivity;
    private volatile long generation;
    private PowerManager.WakeLock wakeLock;
    private VoiceCuePlayer cues;
    private final Object cueLock = new Object();
    private final VoiceSessionSignals signals = new VoiceSessionSignals();
    private final java.util.concurrent.ConcurrentHashMap<String, Long> timings = new java.util.concurrent.ConcurrentHashMap<>();
    private volatile String lastCue;
    private volatile long lastCueAt;
    private volatile long lastCueElapsedRealtimeAt = -1;
    private volatile long lastCueCompletedElapsedRealtimeAt = -1;
    private volatile boolean lastCueCompleted;
    private long cueSequence;
    private AudioManager manager;
    private AudioFocusRequest audioFocus;
    private volatile boolean hasAudioFocus, releasingFocus;
    private volatile long wakeSuppressedUntil;
    private volatile boolean echoAvailable, echoEnabled, noiseEnabled;
    private volatile int outputDeviceType = -1;
    private volatile String voiceProvider, voiceModel;
    private volatile long voiceConnectedAt;
    private boolean routeReceiverRegistered;
    private final BroadcastReceiver routeReceiver = new BroadcastReceiver() {
        @Override public void onReceive(Context context, Intent intent) {
            if (AudioManager.ACTION_AUDIO_BECOMING_NOISY.equals(intent.getAction())) {
                RealtimeVoice current = realtime;
                if (current != null) { endRequested = true; current.close(); }
            }
        }
    };
    private long frames, wakeCount, replies, statusWritten;
    private volatile long audioUploaded;

    @Override public IBinder onBind(Intent intent) { return null; }

    @Override public void onCreate() {
        super.onCreate();
        String saved = getSharedPreferences("voice", 0).getString("last_error", null);
        lastError = saved == null ? null : VoiceFailure.normalize(saved);
        lastErrorAt = getSharedPreferences("voice", 0).getLong("last_error_at", 0);
        NotificationManager notifications = getSystemService(NotificationManager.class);
        notifications.createNotificationChannel(new NotificationChannel("voice", getString(R.string.voice_title), NotificationManager.IMPORTANCE_LOW));
        startForeground(3, notification());
        manager = getSystemService(AudioManager.class);
        audioFocus = new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_EXCLUSIVE)
                .setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ASSISTANT)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                .setAcceptsDelayedFocusGain(false)
                .setOnAudioFocusChangeListener(change -> {
                    if (change < 0) {
                        hasAudioFocus = false;
                        RealtimeVoice current = realtime;
                        if (running && current != null) { failure = "voice_audio_interrupted"; current.close(); }
                    }
                }).build();
        cues = new VoiceCuePlayer(manager, () -> hasAudioFocus);
        IntentFilter routeFilter = new IntentFilter(AudioManager.ACTION_AUDIO_BECOMING_NOISY);
        if (Build.VERSION.SDK_INT >= 33) registerReceiver(routeReceiver, routeFilter, Context.RECEIVER_NOT_EXPORTED);
        else registerReceiver(routeReceiver, routeFilter);
        routeReceiverRegistered = true;
    }

    private Notification notification() {
        PendingIntent open = PendingIntent.getActivity(this, 30, new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        PendingIntent stop = PendingIntent.getService(this, 31, new Intent(this, VoiceService.class).setAction("stop"), PendingIntent.FLAG_IMMUTABLE);
        String message = label(status);
        if (lastError != null && status.equals("error")) message = failureLabel(this, lastError);
        else if (lastError != null && status.equals("wake"))
            message = getString(R.string.voice_wait_after_error, message, failureLabel(this, lastError));
        return new Notification.Builder(this, "voice").setSmallIcon(android.R.drawable.ic_btn_speak_now)
                .setContentTitle(getString(R.string.voice_title)).setContentText(message)
                .setStyle(new Notification.BigTextStyle().bigText(message))
                .setContentIntent(open).setOngoing(true).setOnlyAlertOnce(true)
                .addAction(new Notification.Action.Builder(null, getString(R.string.voice_stop), stop).build()).build();
    }

    static String label(Context context, String phase) {
        int text;
        switch (phase) {
            case "preparing": text = R.string.voice_preparing; break;
            case "wake": return context.getString(R.string.voice_wake,
                    context.getSharedPreferences("voice", 0).getString("phrase", "эй дом"));
            case "connecting": text = R.string.voice_connecting; break;
            case "listening": case "speech": text = R.string.voice_listening; break;
            case "thinking": text = R.string.voice_thinking; break;
            case "speaking": text = R.string.voice_speaking; break;
            case "finishing": text = R.string.voice_finishing; break;
            case "off": text = R.string.voice_off; break;
            default: text = R.string.voice_error;
        }
        return context.getString(text);
    }
    private String label(String phase) { return label(this, phase); }

    static String failureLabel(Context context, String code) {
        int text;
        switch (VoiceFailure.normalize(code)) {
            case "openai_required": case "provider_required": case "realtime_provider_required": text = R.string.voice_error_provider; break;
            case "voice_authorization_required": case "voice_authorization_expired": case "not_allowed":
            case "invalid_voice_authorization": case "voice_authorization_not_saved": text = R.string.voice_error_auth; break;
            case "realtime_provider_rejected": text = R.string.voice_error_rejected; break;
            case "voice_rate_limited": text = R.string.voice_error_limit; break;
            case "unsupported_wake_word": case "invalid_wake_phrase": text = R.string.voice_error_phrase; break;
            case "microphone_permission_revoked": text = R.string.voice_error_permission; break;
            case "voice_model_unavailable": case "voice_model_checksum_mismatch":
            case "invalid_voice_model": case "incomplete_voice_model": text = R.string.voice_error_model; break;
            case "voice_assistant_failed": case "empty_assistant_reply": text = R.string.voice_error_assistant; break;
            case "voice_signal_unavailable": text = R.string.voice_error_signal; break;
            case "voice_audio_busy": case "voice_audio_interrupted": text = R.string.voice_error_audio_focus; break;
            case "unsupported_voice_protocol": case "voice_history_protocol_required": text = R.string.voice_error_update; break;
            case "voice_history_failed": text = R.string.voice_error_history; break;
            case "voice_timeout": case "voice_connection_failed": case "realtime_connection_failed":
            case "realtime_connection_closed": case "realtime_unavailable": text = R.string.voice_error_connection; break;
            default: text = R.string.voice_error;
        }
        return context.getString(text);
    }

    private void recordFailure(String code) {
        lastError = VoiceFailure.normalize(code); lastErrorAt = System.currentTimeMillis();
        getSharedPreferences("voice", 0).edit().putString("last_error", lastError).putLong("last_error_at", lastErrorAt).apply();
    }

    private void clearFailure() {
        lastError = null; lastErrorAt = 0;
        getSharedPreferences("voice", 0).edit().remove("last_error").remove("last_error_at").apply();
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && "stop".equals(intent.getAction())) {
            getSharedPreferences("voice", 0).edit().putBoolean("enabled", false).apply();
            stopSelf(); return START_NOT_STICKY;
        }
        if (capture != null) return START_STICKY;
        if (!getSharedPreferences("voice", 0).getBoolean("enabled", false)
                || checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED
                || !new VoiceCredentials(this).configured()) { stopSelf(); return START_NOT_STICKY; }
        running = true; active = true;
        wakeLock = getSystemService(PowerManager.class).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "habab:voice"); wakeLock.acquire();
        capture = new Thread(this::captureLoop, "habab-voice-capture"); capture.start();
        return START_STICKY;
    }

    private void phase(String next) {
        if (next.equals(status)) return;
        status = next;
        getSystemService(NotificationManager.class).notify(3, notification());
    }

    private void writeStatus(boolean force) {
        if (!force && SystemClock.elapsedRealtime() - statusWritten < 2000) return;
        statusWritten = SystemClock.elapsedRealtime();
        try {
            JSONObject result = new JSONObject().put("phase", status).put("active", running)
                    .put("captureFrames", frames).put("wakeCount", wakeCount).put("replies", replies)
                    .put("uploadedPcmBytes", audioUploaded).put("screenOn", getSystemService(PowerManager.class).isInteractive())
                    .put("timingsMs", new JSONObject(timings))
                    .put("audio", new JSONObject().put("echoAvailable", echoAvailable).put("echoEnabled", echoEnabled)
                            .put("noiseSuppressionEnabled", noiseEnabled).put("focus", hasAudioFocus).put("outputDeviceType", outputDeviceType))
                    .put("recordedAt", System.currentTimeMillis());
            if (failure != null) result.put("error", failure);
            RealtimeVoice connected = realtime;
            if (voiceProvider != null && voiceModel != null) result.put("lastConnection", new JSONObject()
                    .put("provider", voiceProvider).put("model", voiceModel).put("connectedAt", voiceConnectedAt)
                    .put("active", connected != null && connected.ready() && !connected.finishing()));
            if (lastError != null) result.put("lastError", lastError).put("lastErrorAt", lastErrorAt);
            if (lastCue != null) result.put("lastCue", lastCue).put("lastCueAt", lastCueAt)
                    .put("lastCueElapsedRealtimeAt", lastCueElapsedRealtimeAt)
                    .put("lastCueCompletedElapsedRealtimeAt", lastCueCompletedElapsedRealtimeAt)
                    .put("lastCueCompleted", lastCueCompleted);
            VoiceStatusFile.write(new File(getFilesDir(), "voice-status.json").toPath(),
                    result.toString().getBytes(StandardCharsets.UTF_8));
        } catch (Exception ignored) { /* Foreground status still reports failure. */ }
    }

    private void captureLoop() {
        AcousticEchoCanceler echo = null; NoiseSuppressor noise = null;
        try {
            phase("preparing"); writeStatus(true);
            File model = VoiceModel.prepare(this);
            String phrase = getSharedPreferences("voice", 0).getString("phrase", "эй дом");
            try (WakeDetector detector = new WakeDetector(model.toString(), phrase)) {
                int size = Math.max(6400, AudioRecord.getMinBufferSize(16000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT));
                if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED)
                    throw new SecurityException("microphone_permission_revoked");
                recorder = new AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION, 16000,
                        AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, size);
                if (recorder.getState() != AudioRecord.STATE_INITIALIZED) throw new IllegalStateException("microphone_unavailable");
                echoAvailable = AcousticEchoCanceler.isAvailable();
                if (echoAvailable) { echo = AcousticEchoCanceler.create(recorder.getAudioSessionId()); if (echo != null) { echo.setEnabled(true); echoEnabled = echo.getEnabled(); } }
                if (NoiseSuppressor.isAvailable()) { noise = NoiseSuppressor.create(recorder.getAudioSessionId()); if (noise != null) { noise.setEnabled(true); noiseEnabled = noise.getEnabled(); } }
                recorder.startRecording();
                short[] samples = new short[320];
                ArrayDeque<short[]> pending = new ArrayDeque<>();
                long started = 0;
                phase("wake");
                while (running) {
                    int count = recorder.read(samples, 0, samples.length, AudioRecord.READ_BLOCKING);
                    if (count <= 0) throw new IllegalStateException("microphone_read_failed");
                    frames++; long now = SystemClock.elapsedRealtime();
                    RealtimeVoice client = realtime;
                    if (failure != null) {
                        recordFailure(failure);
                        endSession(VoiceSessionSignals.EndReason.ERROR); pending.clear(); detector.reset();
                        phase("error"); writeStatus(true);
                        Thread.sleep(1200); failure = null; phase("wake");
                    } else if (endRequested) {
                        endRequested = false;
                        endSession(VoiceSessionSignals.EndReason.NORMAL); pending.clear(); detector.reset(); phase("wake");
                    } else if (client == null && now >= wakeSuppressedUntil) {
                        if (detector.accept(samples, count, now)) { wakeCount++; started = now; lastActivity = now; beginSession(now); }
                    } else if (client == null) {
                        pending.clear(); // The END cue and its acoustic tail cannot activate the next session.
                    } else if (client.finishing()) {
                        pending.clear();
                    } else if (client.deadlineAction(now) == VoiceToolSession.DeadlineAction.TIMEOUT) {
                        client.timeoutToolGrace(); pending.clear(); detector.reset();
                    } else if (client.deadlineAction(now) == VoiceToolSession.DeadlineAction.FINISH) {
                        client.finish(); pending.clear(); detector.reset();
                    } else if (client.deadlineAction(now) == VoiceToolSession.DeadlineAction.DRAIN_RESULT) {
                        pending.clear(); client.awaitToolResult(); detector.reset();
                    } else if (client.ready() && !status.equals("thinking") && now - lastActivity > 20000) {
                        client.finish(); pending.clear(); detector.reset();
                    } else if (!signals.allowsAudio()) {
                        pending.clear(); // Discard microphone frames while the START cue is still playing.
                    } else if (client.ready()) {
                        while (!pending.isEmpty()) { short[] buffered = pending.removeFirst(); audioUploaded += client.append(buffered, buffered.length); }
                        audioUploaded += client.append(samples, count);
                    } else {
                        if (pending.size() >= 400) failure = "voice_connection_too_slow";
                        else pending.add(java.util.Arrays.copyOf(samples, count));
                    }
                    writeStatus(false);
                }
            }
        } catch (Exception | LinkageError error) {
            if (running) {
                failure = VoiceFailure.code(error); recordFailure(failure);
                phase("error");
            }
        } finally {
            running = false; active = false; endSession(failure == null ? VoiceSessionSignals.EndReason.STOP : VoiceSessionSignals.EndReason.ERROR);
            AudioRecord current = recorder; if (current != null) { try { current.stop(); } catch (IllegalStateException ignored) {} current.release(); recorder = null; }
            if (echo != null) echo.release(); if (noise != null) noise.release(); echoEnabled = noiseEnabled = false; cues.close();
            writeStatus(true); stopSelf();
        }
    }

    private synchronized void beginSession(long wakeAt) {
        if (!running || realtime != null) return;
        timings.clear();
        voiceProvider = voiceModel = null; voiceConnectedAt = 0;
        outputDeviceType = -1;
        endRequested = false;
        hasAudioFocus = manager.requestAudioFocus(audioFocus) == AudioManager.AUDIOFOCUS_REQUEST_GRANTED;
        if (!hasAudioFocus) { failure = "voice_audio_busy"; return; }
        long attempt = ++generation;
        phase("connecting");
        RealtimeVoice client = new RealtimeVoice(this, new RealtimeVoice.Listener() {
            @Override public void phase(String value) { if (running && generation == attempt) { lastActivity = SystemClock.elapsedRealtime(); VoiceService.this.phase(value); } }
            @Override public void completed() { if (generation == attempt) { replies++; clearFailure(); } }
            @Override public void failed(String code) { if (generation == attempt) failure = VoiceFailure.normalize(code); }
            @Override public void ended() { if (running && generation == attempt) endRequested = true; }
            @Override public void timing(String name, long duration) { if (generation == attempt) timings.put(name, duration); }
            @Override public void routed(int type) { if (generation == attempt) outputDeviceType = type; }
            @Override public void connected(String provider, String model) {
                if (generation == attempt) { voiceConnectedAt = System.currentTimeMillis(); voiceModel = model; voiceProvider = provider; }
            }
        }, wakeAt);
        realtime = client;
        synchronized (cueLock) {
            playCue(signals.begin(attempt)).thenAccept(complete -> {
                if (!running || generation != attempt) return;
                if (!signals.startCompleted(attempt, complete)) { failure = "voice_signal_unavailable"; return; }
                new Thread(() -> {
                    if (!running || generation != attempt) { client.close(); return; }
                    try { client.start(); if (!running || generation != attempt) client.close(); }
                    catch (Exception error) { if (generation == attempt) failure = VoiceFailure.code(error); client.close(); }
                }, "habab-voice-connect").start();
            });
        }
    }

    private synchronized void endSession(VoiceSessionSignals.EndReason reason) {
        generation++;
        RealtimeVoice client = realtime; realtime = null; if (client != null) client.close();
        synchronized (cueLock) {
            VoiceSessionSignals.Cue cue = signals.end(reason);
            if (cue != VoiceSessionSignals.Cue.NONE) {
                wakeSuppressedUntil = Long.MAX_VALUE; releasingFocus = true;
                playCue(cue).thenAccept(complete -> {
                    releaseAudioFocus();
                    wakeSuppressedUntil = SystemClock.elapsedRealtime() + 300;
                });
            } else if (!releasingFocus) releaseAudioFocus();
        }
    }

    private synchronized void releaseAudioFocus() {
        manager.abandonAudioFocusRequest(audioFocus); hasAudioFocus = false; releasingFocus = false;
    }

    private java.util.concurrent.CompletableFuture<Boolean> playCue(VoiceSessionSignals.Cue cue) {
        if (cue == VoiceSessionSignals.Cue.NONE) return java.util.concurrent.CompletableFuture.completedFuture(false);
        synchronized (cueLock) {
            long sequence = ++cueSequence;
            lastCue = cue.name().toLowerCase(java.util.Locale.ROOT);
            lastCueAt = System.currentTimeMillis(); lastCueCompleted = false;
            lastCueElapsedRealtimeAt = SystemClock.elapsedRealtime();
            lastCueCompletedElapsedRealtimeAt = -1;
            java.util.concurrent.CompletableFuture<Boolean> completion = cues.play(cue);
            completion.thenAccept(complete -> { synchronized (cueLock) { if (sequence == cueSequence) {
                lastCueCompletedElapsedRealtimeAt = SystemClock.elapsedRealtime();
                lastCueCompleted = complete;
            } } });
            return completion;
        }
    }

    @Override public void onDestroy() {
        running = false; active = false;
        if (routeReceiverRegistered) { unregisterReceiver(routeReceiver); routeReceiverRegistered = false; }
        AudioRecord current = recorder; if (current != null) { try { current.stop(); } catch (IllegalStateException ignored) {} }
        if (capture != null) capture.interrupt();
        endSession(VoiceSessionSignals.EndReason.STOP);
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        if (!status.equals("error")) status = "off";
        writeStatus(true);
        stopForeground(true); super.onDestroy();
    }
}
