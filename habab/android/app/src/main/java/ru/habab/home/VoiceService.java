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
        NotificationManager notifications = getSystemService(NotificationManager.class);
        notifications.createNotificationChannel(new NotificationChannel("voice", getString(R.string.voice_title), NotificationManager.IMPORTANCE_LOW));
        startForeground(3, notification());
        manager = getSystemService(AudioManager.class);
    }

    private Notification notification() {
        PendingIntent open = PendingIntent.getActivity(this, 30, new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        PendingIntent stop = PendingIntent.getService(this, 31, new Intent(this, VoiceService.class).setAction("stop"), PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, "voice").setSmallIcon(android.R.drawable.ic_btn_speak_now)
                .setContentTitle(getString(R.string.voice_title)).setContentText(label(status))
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
                failure = error instanceof java.io.IOException && error.getMessage() != null
                        ? error.getMessage() : error.getClass().getSimpleName();
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
            @Override public void completed() { if (generation == attempt) replies++; }
            @Override public void failed(String code) { if (generation == attempt) failure = code; }
        });
        realtime = client;
        new Thread(() -> { try { client.start(); if (!running || generation != attempt) client.close(); }
            catch (Exception error) { if (generation == attempt) failure = "voice_connection_failed"; client.close(); } }, "habab-voice-connect").start();
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
