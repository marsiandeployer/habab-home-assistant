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
import java.nio.file.Files;
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
    private volatile long lastActivity;
    private volatile long generation;
    private PowerManager.WakeLock wakeLock;
    private ToneGenerator tones;
    private AudioManager manager;
    private int oldMode;
    private boolean oldSpeaker;
    private long frames, wakeCount, replies, statusWritten;
    private volatile long audioUploaded;
    private boolean communicationMode;

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
    }

    private Notification notification() {
        PendingIntent open = PendingIntent.getActivity(this, 30, new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        PendingIntent stop = PendingIntent.getService(this, 31, new Intent(this, VoiceService.class).setAction("stop"), PendingIntent.FLAG_IMMUTABLE);
        String message = lastError != null && (status.equals("wake") || status.equals("error"))
                ? failureLabel(this, lastError) : label(status);
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
            case "wake": text = R.string.voice_wake; break;
            case "connecting": text = R.string.voice_connecting; break;
            case "listening": case "speech": text = R.string.voice_listening; break;
            case "thinking": text = R.string.voice_thinking; break;
            case "speaking": text = R.string.voice_speaking; break;
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
                    .put("recordedAt", System.currentTimeMillis());
            if (failure != null) result.put("error", failure);
            if (lastError != null) result.put("lastError", lastError).put("lastErrorAt", lastErrorAt);
            Files.write(new File(getFilesDir(), "voice-status.json").toPath(), result.toString().getBytes(StandardCharsets.UTF_8));
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
                if (AcousticEchoCanceler.isAvailable()) { echo = AcousticEchoCanceler.create(recorder.getAudioSessionId()); if (echo != null) echo.setEnabled(true); }
                if (NoiseSuppressor.isAvailable()) { noise = NoiseSuppressor.create(recorder.getAudioSessionId()); if (noise != null) noise.setEnabled(true); }
                tones = new ToneGenerator(AudioManager.STREAM_NOTIFICATION, 60);
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
                        endSession(); pending.clear(); detector.reset();
                        phase("error"); writeStatus(true);
                        tones.startTone(ToneGenerator.TONE_PROP_NACK, 180);
                        Thread.sleep(1200); failure = null; phase("wake");
                    } else if (client == null) {
                        if (detector.accept(samples, count, now)) { wakeCount++; started = now; lastActivity = now; beginSession(); }
                    } else if (now - started > 120000 || (client.ready() && !status.equals("thinking") && now - lastActivity > 20000)) {
                        endSession(); pending.clear(); detector.reset(); phase("wake");
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
            running = false; active = false; endSession();
            AudioRecord current = recorder; if (current != null) { try { current.stop(); } catch (IllegalStateException ignored) {} current.release(); recorder = null; }
            if (echo != null) echo.release(); if (noise != null) noise.release(); if (tones != null) tones.release();
            writeStatus(true); stopSelf();
        }
    }

    private void beginSession() {
        long attempt = ++generation;
        oldMode = manager.getMode(); oldSpeaker = manager.isSpeakerphoneOn(); communicationMode = true;
        manager.setMode(AudioManager.MODE_IN_COMMUNICATION); manager.setSpeakerphoneOn(true);
        phase("connecting"); tones.startTone(ToneGenerator.TONE_PROP_BEEP, 120);
        RealtimeVoice client = new RealtimeVoice(this, new RealtimeVoice.Listener() {
            @Override public void phase(String value) { if (running && generation == attempt) { lastActivity = SystemClock.elapsedRealtime(); VoiceService.this.phase(value); } }
            @Override public void completed() { if (generation == attempt) { replies++; clearFailure(); } }
            @Override public void failed(String code) { if (generation == attempt) failure = VoiceFailure.normalize(code); }
        });
        realtime = client;
        new Thread(() -> { try { client.start(); if (!running || generation != attempt) client.close(); }
            catch (Exception error) { if (generation == attempt) failure = VoiceFailure.code(error); client.close(); } }, "habab-voice-connect").start();
    }

    private void endSession() {
        generation++;
        RealtimeVoice client = realtime; realtime = null; if (client != null) client.close();
        if (communicationMode) { manager.setSpeakerphoneOn(oldSpeaker); manager.setMode(oldMode); communicationMode = false; }
    }

    @Override public void onDestroy() {
        running = false; active = false;
        AudioRecord current = recorder; if (current != null) { try { current.stop(); } catch (IllegalStateException ignored) {} }
        if (capture != null) capture.interrupt();
        endSession();
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        if (!status.equals("error")) status = "off";
        stopForeground(true); super.onDestroy();
    }
}
