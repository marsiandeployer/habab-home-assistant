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
    private static final VoiceLifecycleDrain lifecycle = new VoiceLifecycleDrain();
    private static final VoiceLifecycleTrace lifecycleTrace = new VoiceLifecycleTrace();
    private static final java.util.concurrent.ThreadPoolExecutor wakeClipWriter =
            new java.util.concurrent.ThreadPoolExecutor(1, 1, 0, java.util.concurrent.TimeUnit.SECONDS,
                    new java.util.concurrent.ArrayBlockingQueue<>(2),
                    new java.util.concurrent.ThreadPoolExecutor.AbortPolicy());
    private static volatile Context lifecycleContext;
    private static volatile VoiceService instance;
    private final java.util.concurrent.CompletableFuture<Void> captureCleaned = new java.util.concurrent.CompletableFuture<>();
    private final java.util.concurrent.CompletableFuture<Void> serviceCleaned = new java.util.concurrent.CompletableFuture<>();
    private long serviceGeneration;
    private volatile boolean lifecycleCleanupFailed;
    static volatile boolean active;
    static volatile String status = "off";
    static volatile String lastError;
    private volatile long lastErrorAt;
    private volatile boolean running;
    private Thread capture;
    private volatile AudioRecord recorder;
    private volatile SilentVoiceCallPlayback silentCallPlayback;
    private volatile RealtimeVoice realtime;
    private volatile String failure;
    private volatile String finishTrigger;
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
    private java.util.concurrent.CompletableFuture<Boolean> pendingEndCleanup =
            java.util.concurrent.CompletableFuture.completedFuture(false);
    private long cueSequence;
    private AudioManager manager;
    private AudioFocusRequest audioFocus;
    private volatile boolean hasAudioFocus, releasingFocus;
    private boolean legacyScoSampleRate;
    private volatile boolean scoInputEffectsBypassed;
    private volatile boolean preferredScoInputAccepted;
    private volatile long wakeSuppressedUntil;
    private volatile boolean echoAvailable, echoEnabled, noiseEnabled;
    private volatile int outputDeviceType = -1;
    private volatile int inputDeviceType = -1;
    private volatile int traceAudioSource = MediaRecorder.AudioSource.VOICE_COMMUNICATION;
    private volatile int traceSampleRate = 16000;
    private volatile int captureSilenced = -1, recordingEffects = -1;
    private volatile boolean recordingCallbackRegistered;
    private volatile String inputDeviceName = "";
    private volatile String outputDeviceName = "";
    private volatile String audioRoute = VoiceAudioRoute.PHONE;
    private volatile String voiceProvider, voiceModel;
    private volatile long voiceConnectedAt;
    private boolean routeReceiverRegistered;
    private boolean scoReceiverRegistered;
    private boolean audioDeviceCallbackRegistered;
    private final AudioManager.AudioRecordingCallback recordingCallback = new AudioManager.AudioRecordingCallback() {
        @Override public void onRecordingConfigChanged(java.util.List<AudioRecordingConfiguration> configurations) {
            AudioRecord current = recorder;
            if (current == null || Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return;
            try {
                AudioRecordingConfiguration configuration = current.getActiveRecordingConfiguration();
                if (configuration != null) {
                    AudioDeviceInfo input = configuration.getAudioDevice();
                    inputDeviceType = input == null ? -1 : input.getType();
                    AudioFormat format = configuration.getFormat();
                    if (format != null) traceSampleRate = format.getSampleRate();
                    traceAudioSource = configuration.getAudioSource();
                    captureSilenced = configuration.isClientSilenced() ? 1 : 0;
                    java.util.List<?> effects = configuration.getEffects();
                    recordingEffects = effects == null ? 0 : effects.size();
                }
                traceWake(VoiceLifecycleTrace.Event.AUDIO_CAPTURE_CONFIG_CHANGED,
                        VoiceLifecycleTrace.Reason.ROUTE, -1, SystemClock.elapsedRealtime(),
                        -1, -1, -1, -1, -1, -1);
            } catch (RuntimeException ignored) {
                // Route diagnostics must never interrupt capture or application lifecycle.
            }
        }
    };
    private volatile boolean scoConnected;
    private volatile boolean scoConnectFailed;
    private volatile java.util.concurrent.CountDownLatch scoConnection = new java.util.concurrent.CountDownLatch(0);
    private int selectedCommunicationDeviceId = -1;
    private boolean scoRequested;
    private boolean audioRouteInitialized;
    private final AudioDeviceCallback audioDeviceCallback = new AudioDeviceCallback() {
        @Override public void onAudioDevicesAdded(AudioDeviceInfo[] addedDevices) { refreshAudioRoute(); }
        @Override public void onAudioDevicesRemoved(AudioDeviceInfo[] removedDevices) { refreshAudioRoute(); }
    };
    private final BroadcastReceiver routeReceiver = new BroadcastReceiver() {
        @Override public void onReceive(Context context, Intent intent) {
            if (AudioManager.ACTION_AUDIO_BECOMING_NOISY.equals(intent.getAction())) refreshAudioRoute();
        }
    };
    private final BroadcastReceiver scoReceiver = new BroadcastReceiver() {
        @Override public void onReceive(Context context, Intent intent) {
            int state = intent.getIntExtra(AudioManager.EXTRA_SCO_AUDIO_STATE, AudioManager.SCO_AUDIO_STATE_ERROR);
            boolean wasConnected = scoConnected;
            boolean routed = state == AudioManager.SCO_AUDIO_STATE_CONNECTED
                    && (manager.isBluetoothScoOn() || hasScoOutputDevice());
            scoConnected = routed;
            if (routed) {
                scoConnection.countDown();
                if (bluetoothAudioRequested()) {
                    boolean recoveringFallback = scoConnectFailed || "bluetooth_unavailable".equals(audioRoute);
                    AudioRecord current = recorder;
                    AudioDeviceInfo input = current == null ? null : current.getRoutedDevice();
                    int inputType = input == null ? -1 : input.getType();
                    if (VoiceAudioRoute.shouldReselectLegacyBluetooth(legacyScoSampleRate,
                            scoRequested, inputType, recoveringFallback)) {
                        refreshAudioRoute();
                    } else {
                        if (legacyScoSampleRate) scoRequested = true;
                        AudioDeviceInfo output = VoiceAudioRoute.bluetoothCommunicationOutput(manager);
                        audioRoute = output == null ? "bluetooth_unavailable" : VoiceAudioRoute.deviceName(output);
                    }
                }
            }
            else if (wasConnected && state == AudioManager.SCO_AUDIO_STATE_DISCONNECTED
                    && bluetoothAudioRequested()) {
                AudioRecord current = recorder;
                AudioDeviceInfo input = current == null ? null : current.getRoutedDevice();
                AudioDeviceInfo output = VoiceAudioRoute.bluetoothCommunicationOutput(manager);
                if (input != null && output != null
                        && VoiceAudioRoute.shouldKeepActiveScoInput(input.getType(), output != null)) {
                    restoreBluetoothOutputForActiveScoInput(input);
                } else {
                    scoConnectFailed = true;
                    scoRequested = false;
                    VoiceAudioRoute.selectPhone(manager);
                    audioRoute = "bluetooth_unavailable";
                }
            }
            writeStatus(true);
        }
    };
    private long frames, wakeCount, replies, statusWritten;
    private String lastWakeSource = "none";
    private volatile long audioUploaded;

    @Override public IBinder onBind(Intent intent) { return null; }

    static void restartAfterConfiguration(Context context) {
        lifecycleContext = context.getApplicationContext();
        lifecycle.cancel();
        ServerService.clearVoiceStartRequest();
        trace(context, VoiceLifecycleTrace.Event.START_CANCELLED, VoiceLifecycleTrace.Reason.SETTINGS, lifecycle.generation());
        context.stopService(new Intent(context, VoiceService.class));
        ServerService.startVoiceIfReady(context, true);
    }

    static boolean hasActiveConversation() {
        VoiceService service = instance;
        return service != null && service.realtime != null;
    }

    static boolean canApplyRemoteRoute() {
        VoiceService service = instance;
        boolean serviceActive = service != null && service.running;
        return VoiceRouteTarget.safeBoundary(serviceActive, serviceActive,
                serviceActive && service.recorder != null,
                serviceActive ? status : "off", serviceActive && service.realtime != null,
                !serviceActive || service.pendingEndCleanup.isDone() && !service.endRequested,
                serviceActive && service.releasingFocus);
    }

    static JSONObject routeControlSnapshot(Context context) throws org.json.JSONException {
        VoiceService service = instance;
        if (service == null || !service.running) {
            return new JSONObject().put("phase", "off").put("actual_route", "off")
                    .put("input_type", -1);
        }
        AudioRecord current = service.recorder;
        AudioDeviceInfo input = current == null ? null : current.getRoutedDevice();
        int type = input == null ? -1 : input.getType();
        String actual;
        if (service.bluetoothAudioRequested()) {
            actual = VoiceAudioRoute.isLegacyScoInput(type)
                    && !"bluetooth_unavailable".equals(service.audioRoute) ? "bluetooth"
                    : "bluetooth_unavailable";
        } else {
            actual = "phone".equals(service.audioRoute) && type >= 0
                    && type == VoiceRouteTarget.BUILTIN_MIC_INPUT ? "phone" : "phone_unavailable";
        }
        return new JSONObject().put("phase", status).put("actual_route", actual)
                .put("input_type", type);
    }

    static void cancelPendingStart() {
        lifecycle.cancel();
        ServerService.clearVoiceStartRequest();
        Context context = lifecycleContext;
        if (context != null) trace(context, VoiceLifecycleTrace.Event.START_CANCELLED,
                VoiceLifecycleTrace.Reason.STOP, lifecycle.generation());
    }

    static void launchWhenDrained(Context context, boolean afterConfiguration, Runnable launch) {
        Context application = context.getApplicationContext();
        lifecycleContext = application;
        VoiceLifecycleTrace.Reason reason = afterConfiguration
                ? VoiceLifecycleTrace.Reason.SETTINGS : VoiceLifecycleTrace.Reason.AUTOMATIC;
        VoiceLifecycleDrain.Start request = lifecycle.request();
        if (request == null) return;
        trace(application, VoiceLifecycleTrace.Event.START_QUEUED,
                reason, lifecycle.generation());
        request.drained.whenComplete((ignored, error) -> new Handler(Looper.getMainLooper()).post(() -> {
            if (!lifecycle.claim(request)) return;
            trace(application, error == null ? VoiceLifecycleTrace.Event.START_RELEASED : VoiceLifecycleTrace.Event.DRAIN_FAILED,
                    reason, lifecycle.generation());
            if (error == null) launch.run();
        }));
    }

    private static void trace(Context context, VoiceLifecycleTrace.Event event,
            VoiceLifecycleTrace.Reason reason, long generation) {
        String route = context.getSharedPreferences("voice", 0).getString("audio_route_mode", VoiceAudioRoute.PHONE);
        int routeCode = "duplicate".equals(route) ? 2 : "bluetooth".equals(route) ? 1 : 0;
        synchronized (lifecycleTrace) {
            lifecycleTrace.record(event, reason, generation, routeCode, System.currentTimeMillis());
            persistLifecycleTrace(context);
        }
    }

    private void traceWake(VoiceLifecycleTrace.Event event, VoiceLifecycleTrace.Reason reason,
            long attempt, long elapsedAt, int rmsPcm16, int vadSpeech, int partialPresent, int phraseMatch,
            int confidencePermille, long elapsedFromWakeMs) {
        String requested = getSharedPreferences("voice", 0).getString("audio_route_mode", VoiceAudioRoute.PHONE);
        int routeCode = "duplicate".equals(requested) ? 2 : "bluetooth".equals(requested) ? 1 : 0;
        AudioRecord current = recorder;
        AudioDeviceInfo input = current == null ? null : current.getRoutedDevice();
        int deviceType = input == null ? inputDeviceType : input.getType();
        synchronized (lifecycleTrace) {
            lifecycleTrace.recordWake(event, reason, serviceGeneration, attempt, routeCode,
                    deviceType, traceAudioSource, traceSampleRate, captureSilenced, recordingEffects,
                    System.currentTimeMillis(), elapsedAt,
                    rmsPcm16, vadSpeech, partialPresent, phraseMatch, confidencePermille,
                    elapsedFromWakeMs);
            persistLifecycleTrace(this);
        }
    }

    private void traceWakeMilestone(VoiceLifecycleTrace.Event event, VoiceLifecycleTrace.Reason reason,
            long attempt, long wakeAcceptedAt) {
        long now = SystemClock.elapsedRealtime();
        traceWake(event, reason, attempt, now, -1, -1, -1, -1, -1,
                Math.max(0, now - wakeAcceptedAt));
    }

    private static void persistLifecycleTrace(Context context) {
        try {
            VoiceStatusFile.write(new File(context.getFilesDir(), "voice-lifecycle.json").toPath(),
                    new org.json.JSONArray(lifecycleTrace.snapshot()).toString().getBytes(StandardCharsets.UTF_8));
        } catch (Exception ignored) { /* Diagnostics must not interrupt voice cleanup. */ }
    }

    @Override public void onCreate() {
        super.onCreate();
        instance = this;
        lifecycleContext = getApplicationContext();
        serviceGeneration = lifecycle.opened(captureCleaned, serviceCleaned);
        trace(this, VoiceLifecycleTrace.Event.CREATED, VoiceLifecycleTrace.Reason.SERVICE, serviceGeneration);
        String saved = getSharedPreferences("voice", 0).getString("last_error", null);
        lastError = saved == null ? null : VoiceFailure.normalize(saved);
        lastErrorAt = getSharedPreferences("voice", 0).getLong("last_error_at", 0);
        NotificationManager notifications = getSystemService(NotificationManager.class);
        notifications.createNotificationChannel(new NotificationChannel("voice", getString(R.string.voice_title), NotificationManager.IMPORTANCE_LOW));
        startForeground(3, notification());
        manager = getSystemService(AudioManager.class);
        legacyScoSampleRate = Build.VERSION.SDK_INT < Build.VERSION_CODES.S && bluetoothAudioRequested();
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
        cues = new VoiceCuePlayer(manager, () -> hasAudioFocus, legacyScoSampleRate, this::duplicateOutputRequested);
        IntentFilter routeFilter = new IntentFilter(AudioManager.ACTION_AUDIO_BECOMING_NOISY);
        if (Build.VERSION.SDK_INT >= 33) registerReceiver(routeReceiver, routeFilter, Context.RECEIVER_NOT_EXPORTED);
        else registerReceiver(routeReceiver, routeFilter);
        routeReceiverRegistered = true;
        if (legacyScoSampleRate) {
            IntentFilter scoFilter = new IntentFilter(AudioManager.ACTION_SCO_AUDIO_STATE_UPDATED);
            if (Build.VERSION.SDK_INT >= 33) registerReceiver(scoReceiver, scoFilter, Context.RECEIVER_EXPORTED);
            else registerReceiver(scoReceiver, scoFilter);
            scoReceiverRegistered = true;
        }
        manager.registerAudioDeviceCallback(audioDeviceCallback, new Handler(getMainLooper()));
        audioDeviceCallbackRegistered = true;
        refreshAudioRoute();
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
            case "habab_voice_not_ready": text = R.string.voice_error_habab_not_ready; break;
            case "realtime_provider_rejected": text = R.string.voice_error_rejected; break;
            case "voice_rate_limited": text = R.string.voice_error_limit; break;
            case "unsupported_wake_word": case "invalid_wake_phrase": text = R.string.voice_error_phrase; break;
            case "microphone_permission_revoked": text = R.string.voice_error_permission; break;
            case "voice_model_unavailable": case "voice_model_checksum_mismatch":
            case "invalid_voice_model": case "incomplete_voice_model": text = R.string.voice_error_model; break;
            case "voice_assistant_failed": case "empty_assistant_reply": text = R.string.voice_error_assistant; break;
            case "voice_signal_unavailable": text = R.string.voice_error_signal; break;
            case "voice_duplicate_route_unavailable": text = R.string.voice_error_duplicate_route; break;
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
            cancelPendingStart();
            ServerService.clearVoiceStartRequest();
            getSharedPreferences("voice", 0).edit().putBoolean("enabled", false).apply();
            stopSelf(); return START_NOT_STICKY;
        }
        if (!ServerService.voiceStartAllowed(this)) {
            ServerService.clearVoiceStartRequest();
            running = false; active = false;
            stopSelf(startId); return START_NOT_STICKY;
        }
        if (capture != null) { ServerService.clearVoiceStartRequest(); return START_STICKY; }
        running = true; active = true;
        ServerService.clearVoiceStartRequest();
        wakeLock = getSystemService(PowerManager.class).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "habab:voice"); wakeLock.acquire();
        capture = new Thread(this::captureLoop, "habab-voice-capture"); capture.start();
        trace(this, VoiceLifecycleTrace.Event.CAPTURE_STARTED, VoiceLifecycleTrace.Reason.SERVICE, serviceGeneration);
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
            AudioRecord currentRecorder = recorder;
            AudioDeviceInfo input = currentRecorder == null ? null : currentRecorder.getRoutedDevice();
            inputDeviceType = input == null ? -1 : input.getType();
            inputDeviceName = input == null ? "" : VoiceAudioRoute.deviceName(input);
            android.content.SharedPreferences voicePreferences = getSharedPreferences("voice", 0);
            JSONObject result = new JSONObject().put("phase", status).put("active", running)
                    .put("serviceGeneration", serviceGeneration).put("lifecycleCleanupFailed", lifecycleCleanupFailed)
                    .put("captureFrames", frames).put("wakeCount", wakeCount).put("replies", replies)
                    .put("lastWakeSource", lastWakeSource)
                    .put("uploadedPcmBytes", audioUploaded).put("screenOn", getSystemService(PowerManager.class).isInteractive())
                    .put("timingsMs", new JSONObject(timings))
                    .put("audio", new JSONObject().put("echoAvailable", echoAvailable).put("echoEnabled", echoEnabled)
                            .put("noiseSuppressionEnabled", noiseEnabled)
                            .put("scoInputEffectsBypassed", scoInputEffectsBypassed)
                            .put("preferredScoInputAccepted", preferredScoInputAccepted).put("focus", hasAudioFocus)
                            .put("requestedRoute", VoiceAudioRoute.requestedRoute(
                                    voicePreferences.getString("audio_route_mode", null),
                                    voicePreferences.getBoolean("bluetooth_audio", false)))
                            .put("activeRoute", audioRoute).put("inputDeviceType", inputDeviceType)
                            .put("outputDeviceType", outputDeviceType)
                            .put("inputDeviceName", inputDeviceName).put("outputDeviceName", outputDeviceName))
                    .put("recordedAt", System.currentTimeMillis());
            if (cues != null) {
                result.put("lastCueDiagnostics", new JSONObject(cues.diagnostics()));
                result.put("lastStartCueDiagnostics", new JSONObject(cues.startDiagnostics()));
            }
            if (finishTrigger != null) result.put("finishTrigger", finishTrigger);
            if (failure != null) result.put("error", failure);
            RealtimeVoice connected = realtime;
            if (voiceProvider != null && voiceModel != null) result.put("lastConnection", new JSONObject()
                    .put("provider", voiceProvider).put("model", voiceModel).put("connectedAt", voiceConnectedAt)
                    .put("active", connected != null && connected.ready() && !connected.finishing()));
            if (lastError != null) result.put("lastError", lastError).put("lastErrorAt", lastErrorAt);
            SilentVoiceCallPlayback playback = silentCallPlayback;
            if (BuildConfig.DEBUG && playback != null) playback.addDiagnostics(result.getJSONObject("audio"));
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
            try (WakeDetector detector = new WakeDetector(model.toString(), phrase,
                    (event, reason, attempt, elapsedAt, rmsPcm16, vadSpeech,
                            partialPresent, phraseMatch, confidencePermille, elapsedFromWakeMs) ->
                            traceWake(event, reason, attempt, elapsedAt, rmsPcm16, vadSpeech,
                                    partialPresent, phraseMatch, confidencePermille, elapsedFromWakeMs))) {
                awaitBluetoothSco();
                int inputRate = legacyScoSampleRate ? 8000 : 16000;
                int size = Math.max(6400, AudioRecord.getMinBufferSize(inputRate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT));
                if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED)
                    throw new SecurityException("microphone_permission_revoked");
                recorder = new AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION, inputRate,
                        AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, size);
                if (recorder.getState() != AudioRecord.STATE_INITIALIZED) throw new IllegalStateException("microphone_unavailable");
                if (legacyScoSampleRate) {
                    AudioDeviceInfo scoInput = findBluetoothScoInput();
                    preferredScoInputAccepted = scoInput != null && recorder.setPreferredDevice(scoInput);
                } else {
                    preferredScoInputAccepted = false;
                }
                if (legacyScoSampleRate) silentCallPlayback = new SilentVoiceCallPlayback();
                echoAvailable = AcousticEchoCanceler.isAvailable();
                scoInputEffectsBypassed = VoiceAudioRoute.shouldBypassClientEffects(legacyScoSampleRate);
                if (!scoInputEffectsBypassed && echoAvailable) { echo = AcousticEchoCanceler.create(recorder.getAudioSessionId()); if (echo != null) { echo.setEnabled(true); echoEnabled = echo.getEnabled(); } }
                if (!scoInputEffectsBypassed && NoiseSuppressor.isAvailable()) { noise = NoiseSuppressor.create(recorder.getAudioSessionId()); if (noise != null) { noise.setEnabled(true); noiseEnabled = noise.getEnabled(); } }
                traceSampleRate = inputRate;
                traceAudioSource = MediaRecorder.AudioSource.VOICE_COMMUNICATION;
                captureSilenced = -1; recordingEffects = -1;
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    try {
                        recorder.registerAudioRecordingCallback(getMainExecutor(), recordingCallback);
                        recordingCallbackRegistered = true;
                    } catch (RuntimeException callbackError) {
                        traceWake(VoiceLifecycleTrace.Event.AUDIO_CALLBACK_UNAVAILABLE,
                                VoiceLifecycleTrace.Reason.ROUTE, -1, SystemClock.elapsedRealtime(),
                                -1, -1, -1, -1, -1, -1);
                    }
                }
                recorder.startRecording();
                if (legacyScoSampleRate) {
                    AudioDeviceInfo routedInput = awaitRoutedBluetoothScoInput(recorder);
                    if (routedInput != null && VoiceAudioRoute.isLegacyScoInput(routedInput.getType()))
                        restoreBluetoothOutputForActiveScoInput(routedInput);
                    else {
                        scoConnectFailed = true;
                        if (scoRequested) manager.stopBluetoothSco();
                        scoRequested = false; scoConnected = false; selectedCommunicationDeviceId = -1;
                        VoiceAudioRoute.selectPhone(manager);
                        audioRoute = "bluetooth_unavailable"; audioRouteInitialized = true;
                    }
                }
                short[] inputSamples = new short[inputRate / 50];
                PcmRateConverter inputResampler = legacyScoSampleRate ? new PcmRateConverter(8000, 16000) : null;
                ArrayDeque<short[]> pending = new ArrayDeque<>();
                WakeClipBuffer wakeClip = new WakeClipBuffer(8);
                boolean wakeClipActive = false;
                long started = 0;
                phase("wake");
                traceWake(VoiceLifecycleTrace.Event.AUDIO_CAPTURE_READY, VoiceLifecycleTrace.Reason.ROUTE,
                        -1, SystemClock.elapsedRealtime(), -1, -1, -1, -1, -1, -1);
                while (running) {
                    int inputCount = recorder.read(inputSamples, 0, inputSamples.length, AudioRecord.READ_BLOCKING);
                    if (inputCount <= 0) throw new IllegalStateException("microphone_read_failed");
                    short[] samples = inputResampler == null ? inputSamples : inputResampler.convert(inputSamples, inputCount);
                    int count = inputResampler == null ? inputCount : samples.length;
                    frames++; long now = SystemClock.elapsedRealtime();
                    RealtimeVoice client = realtime;
                    if (failure != null) {
                        recordFailure(failure);
                        endSession(VoiceSessionSignals.EndReason.ERROR); pending.clear();
                        detector.reset(now, VoiceLifecycleTrace.Reason.ERROR_RESET);
                        phase("error"); writeStatus(true);
                        Thread.sleep(1200); failure = null; phase("wake");
                    } else if (endRequested) {
                        endRequested = false;
                        endSession(VoiceSessionSignals.EndReason.NORMAL); pending.clear();
                        detector.reset(now, VoiceLifecycleTrace.Reason.SESSION_RESET); phase("wake");
                    } else if (client == null && now >= wakeSuppressedUntil) {
                        boolean recordWake = getSharedPreferences("voice", 0).getBoolean("record_wake_clips", false);
                        if (recordWake) wakeClip.append(samples, count);
                        else if (wakeClipActive) wakeClip.clear();
                        wakeClipActive = recordWake;
                        if (detector.accept(samples, count, now)) {
                            long capturedAt = System.currentTimeMillis();
                            if (recordWake) {
                                File external = getExternalFilesDir(null);
                                if (external != null) {
                                    short[] clip = wakeClip.snapshot();
                                    long capturedAttempt = detector.lastMatchAttempt();
                                    try {
                                        wakeClipWriter.execute(() -> {
                                            try { WakeClipBuffer.save(new File(external, "wake-clips"), clip, capturedAt, capturedAttempt); }
                                            catch (java.io.IOException error) { android.util.Log.w("HababWakeClip", "Failed to save wake clip", error); }
                                            finally { java.util.Arrays.fill(clip, (short) 0); }
                                        });
                                    } catch (java.util.concurrent.RejectedExecutionException full) {
                                        java.util.Arrays.fill(clip, (short) 0);
                                        android.util.Log.w("HababWakeClip", "Wake clip queue full");
                                    }
                                }
                            }
                            wakeClip.clear();
                            WakeMatchGate.MatchSource source = detector.lastMatchSource();
                            lastWakeSource = source == null ? "unknown" : source.statusValue();
                            wakeCount++; started = now; lastActivity = now;
                            beginSession(now, detector.lastMatchAttempt(), recordWake ? capturedAt : -1);
                        }
                    } else if (client == null) {
                        if (wakeClipActive) wakeClip.clear();
                        wakeClipActive = false;
                        pending.clear(); // The END cue and its acoustic tail cannot activate the next session.
                    } else if (client.finishing()) {
                        if (wakeClipActive) wakeClip.clear();
                        wakeClipActive = false;
                        pending.clear();
                    } else if (client.deadlineAction(now) == VoiceToolSession.DeadlineAction.TIMEOUT) {
                        client.timeoutToolGrace(); pending.clear();
                        detector.reset(now, VoiceLifecycleTrace.Reason.SESSION_RESET);
                    } else if (client.deadlineAction(now) == VoiceToolSession.DeadlineAction.FINISH) {
                        client.finish(VoiceFinishTrigger.AUDIO_LIMIT); pending.clear();
                        detector.reset(now, VoiceLifecycleTrace.Reason.SESSION_RESET);
                    } else if (client.deadlineAction(now) == VoiceToolSession.DeadlineAction.DRAIN_RESULT) {
                        pending.clear(); client.awaitToolResult();
                        detector.reset(now, VoiceLifecycleTrace.Reason.SESSION_RESET);
                    } else if (client.ready() && !status.equals("thinking") && now - lastActivity > 20000) {
                        recordFinishTrigger(VoiceFinishTrigger.IDLE_20S);
                        timings.put("end_idle_elapsed_ms", now - lastActivity);
                        client.finish(VoiceFinishTrigger.IDLE_20S); pending.clear();
                        detector.reset(now, VoiceLifecycleTrace.Reason.SESSION_RESET);
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
            try {
                running = false; active = false;
                SilentVoiceCallPlayback playback = silentCallPlayback;
                silentCallPlayback = null;
                if (playback != null) playback.close();
                endSession(failure == null ? VoiceSessionSignals.EndReason.STOP : VoiceSessionSignals.EndReason.ERROR);
                AudioRecord current = recorder;
                if (current != null) {
                    if (recordingCallbackRegistered && Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                        try { current.unregisterAudioRecordingCallback(recordingCallback); }
                        catch (RuntimeException ignored) { /* Recorder is already stopping. */ }
                        recordingCallbackRegistered = false;
                    }
                    try { current.stop(); } catch (IllegalStateException ignored) {}
                    current.release(); recorder = null;
                }
                if (echo != null) echo.release(); if (noise != null) noise.release(); echoEnabled = noiseEnabled = false;
                writeStatus(true); stopSelf();
                trace(this, VoiceLifecycleTrace.Event.CAPTURE_CLEANED, VoiceLifecycleTrace.Reason.SERVICE, serviceGeneration);
                captureCleaned.complete(null);
            } catch (RuntimeException | LinkageError error) {
                lifecycleCleanupFailed = true;
                trace(this, VoiceLifecycleTrace.Event.DRAIN_FAILED, VoiceLifecycleTrace.Reason.SERVICE, serviceGeneration);
                writeStatus(true);
                captureCleaned.completeExceptionally(error);
                throw error;
            }
        }
    }

    private synchronized void beginSession(long wakeAt, long wakeAttempt, long capturedAt) {
        if (!running || realtime != null) return;
        timings.clear();
        finishTrigger = null;
        voiceProvider = voiceModel = null; voiceConnectedAt = 0;
        outputDeviceType = -1; outputDeviceName = "";
        endRequested = false;
        hasAudioFocus = manager.requestAudioFocus(audioFocus) == AudioManager.AUDIOFOCUS_REQUEST_GRANTED;
        if (!hasAudioFocus) { failure = "voice_audio_busy"; return; }
        long attempt = ++generation;
        traceWakeMilestone(VoiceLifecycleTrace.Event.VOICE_SESSION_BEGIN,
                VoiceLifecycleTrace.Reason.EXACT_FINAL, wakeAttempt, wakeAt);
        phase("connecting");
        RealtimeVoice client = new RealtimeVoice(this, new RealtimeVoice.Listener() {
            @Override public void phase(String value) { if (running && generation == attempt) { lastActivity = SystemClock.elapsedRealtime(); VoiceService.this.phase(value); } }
            @Override public void completed() { if (generation == attempt) { replies++; clearFailure(); } }
            @Override public void failed(String code) { if (generation == attempt) failure = VoiceFailure.normalize(code); }
            @Override public void ended() { if (running && generation == attempt) endRequested = true; }
            @Override public void reviewWake() {
                if (generation != attempt || capturedAt < 0) return;
                File external = getExternalFilesDir(null);
                if (external == null) return;
                try {
                    wakeClipWriter.execute(() -> {
                        try { WakeClipBuffer.markForReview(new File(external, "wake-clips"), capturedAt, wakeAttempt); }
                        catch (java.io.IOException error) { android.util.Log.w("HababWakeClip", "Failed to mark wake clip", error); }
                    });
                } catch (java.util.concurrent.RejectedExecutionException full) {
                    android.util.Log.w("HababWakeClip", "Wake review queue full");
                }
            }
            @Override public void timing(String name, long value) { if (generation == attempt) timings.put(name, value); }
            @Override public void finishTrigger(String value) {
                if (generation == attempt) { recordFinishTrigger(value); writeStatus(true); }
            }
            @Override public void routed(int type, String name) {
                if (generation == attempt) { outputDeviceType = type; outputDeviceName = name; writeStatus(true); }
            }
            @Override public void connected(String provider, String model) {
                if (generation == attempt) { voiceConnectedAt = System.currentTimeMillis(); voiceModel = model; voiceProvider = provider; }
            }
        }, wakeAt, legacyScoSampleRate, duplicateOutputRequested());
        realtime = client;
        synchronized (cueLock) {
            traceWakeMilestone(VoiceLifecycleTrace.Event.START_CUE_QUEUED,
                    VoiceLifecycleTrace.Reason.CUE, wakeAttempt, wakeAt);
            playCue(signals.begin(attempt)).thenAccept(complete -> {
                if (!running || generation != attempt) return;
                if (!signals.startCompleted(attempt, complete)) { failure = "voice_signal_unavailable"; return; }
                traceWakeMilestone(VoiceLifecycleTrace.Event.ASR_GATE_OPEN,
                        VoiceLifecycleTrace.Reason.CUE, wakeAttempt, wakeAt);
                new Thread(() -> {
                    if (!running || generation != attempt) { client.close(); return; }
                    try {
                        traceWakeMilestone(VoiceLifecycleTrace.Event.REALTIME_CONNECT_START,
                                VoiceLifecycleTrace.Reason.CUE, wakeAttempt, wakeAt);
                        client.start(); if (!running || generation != attempt) client.close();
                    }
                    catch (Exception error) { if (generation == attempt) failure = VoiceFailure.code(error); client.close(); }
                }, "habab-voice-connect").start();
            });
        }
    }

    private synchronized java.util.concurrent.CompletableFuture<Boolean> endSession(VoiceSessionSignals.EndReason reason) {
        generation++;
        RealtimeVoice client = realtime; realtime = null;
        if (client != null && finishTrigger == null) {
            recordFinishTrigger(reason == VoiceSessionSignals.EndReason.ERROR ? VoiceFinishTrigger.ERROR
                    : reason == VoiceSessionSignals.EndReason.STOP ? VoiceFinishTrigger.SERVICE_STOP
                    : VoiceFinishTrigger.NORMAL);
        }
        if (client != null) client.close();
        synchronized (cueLock) {
            VoiceSessionSignals.Cue cue = signals.end(reason);
            if (cue != VoiceSessionSignals.Cue.NONE) {
                wakeSuppressedUntil = Long.MAX_VALUE; releasingFocus = true;
                java.util.concurrent.CompletableFuture<Boolean> completion = playCue(cue);
                pendingEndCleanup = completion.whenComplete((complete, error) -> {
                    releaseAudioFocus();
                    wakeSuppressedUntil = SystemClock.elapsedRealtime() + 300;
                });
                return pendingEndCleanup;
            } else if (!releasingFocus) releaseAudioFocus();
            else return pendingEndCleanup;
        }
        return java.util.concurrent.CompletableFuture.completedFuture(false);
    }

    private synchronized void releaseAudioFocus() {
        manager.abandonAudioFocusRequest(audioFocus); hasAudioFocus = false; releasingFocus = false;
    }

    private synchronized void recordFinishTrigger(String value) {
        boolean changed = value == null ? finishTrigger != null : !value.equals(finishTrigger);
        finishTrigger = value;
        if (!changed) return;
        String logLine = VoiceFinishTrigger.logLine(value);
        if (logLine != null) android.util.Log.i("HababVoiceTiming", logLine);
    }

    private void refreshAudioRoute() {
        if (manager == null) return;
        String previousRoute = audioRoute;
        int previousInputType = inputDeviceType;
        boolean bluetoothRequested = bluetoothAudioRequested();
        if (!bluetoothRequested) {
            if (!audioRouteInitialized || scoRequested || selectedCommunicationDeviceId != -1)
                VoiceAudioRoute.selectPhone(manager);
            scoRequested = false;
            scoConnected = false;
            scoConnectFailed = false;
            selectedCommunicationDeviceId = -1;
            audioRoute = getString(R.string.voice_phone_audio);
        } else {
            java.util.List<AudioDeviceInfo> devices = VoiceAudioRoute.bluetoothCommunicationOutputs(manager);
            if (devices.isEmpty()) {
                if (!audioRouteInitialized || scoRequested || selectedCommunicationDeviceId != -1)
                    VoiceAudioRoute.selectPhone(manager);
                scoRequested = false;
                scoConnected = false;
                scoConnectFailed = false;
                selectedCommunicationDeviceId = -1;
                audioRoute = "bluetooth_unavailable";
            } else {
                AudioDeviceInfo selected = devices.get(0);
                boolean sameRoute = audioRouteInitialized && selectedCommunicationDeviceId == selected.getId();
                if (legacyScoSampleRate && sameRoute && scoConnectFailed) {
                    audioRoute = "bluetooth_unavailable";
                } else if (!sameRoute || (legacyScoSampleRate && !scoRequested)) {
                    if (scoRequested) manager.stopBluetoothSco();
                    scoConnection = new java.util.concurrent.CountDownLatch(1);
                    scoConnected = false;
                    if (VoiceAudioRoute.selectBluetooth(manager, selected)) {
                        selectedCommunicationDeviceId = selected.getId();
                        scoRequested = legacyScoSampleRate;
                        scoConnectFailed = false;
                        audioRoute = VoiceAudioRoute.deviceName(selected);
                    } else {
                        VoiceAudioRoute.selectPhone(manager);
                        selectedCommunicationDeviceId = -1;
                        scoRequested = false;
                        audioRoute = "bluetooth_unavailable";
                    }
                } else {
                    audioRoute = VoiceAudioRoute.deviceName(selected);
                }
            }
        }
        audioRouteInitialized = true;
        RealtimeVoice currentVoice = realtime;
        if (currentVoice != null) currentVoice.refreshDuplicateOutputDevices(manager, duplicateOutputRequested());
        AudioRecord current = recorder;
        AudioDeviceInfo input = current == null ? null : current.getRoutedDevice();
        inputDeviceType = input == null ? -1 : input.getType();
        inputDeviceName = input == null ? "" : VoiceAudioRoute.deviceName(input);
        if (!previousRoute.equals(audioRoute) || previousInputType != inputDeviceType) {
            traceWake(VoiceLifecycleTrace.Event.ROUTE_CHANGED, VoiceLifecycleTrace.Reason.ROUTE,
                    -1, SystemClock.elapsedRealtime(), -1, -1, -1, -1, -1, -1);
        }
        writeStatus(true);
    }

    private void awaitBluetoothSco() throws InterruptedException {
        if (!legacyScoSampleRate || !scoRequested || scoConnected) return;
        boolean connected = scoConnection.await(8, java.util.concurrent.TimeUnit.SECONDS) || scoConnected;
        if (connected) return;
        synchronized (this) {
            scoConnectFailed = true;
            if (scoRequested) manager.stopBluetoothSco();
            scoRequested = false;
            VoiceAudioRoute.selectPhone(manager);
            audioRoute = "bluetooth_unavailable";
            audioRouteInitialized = true;
            writeStatus(true);
        }
    }

    private AudioDeviceInfo awaitRoutedBluetoothScoInput(AudioRecord current) throws InterruptedException {
        long deadline = SystemClock.elapsedRealtime() + 1500;
        AudioDeviceInfo routed = current.getRoutedDevice();
        while ((routed == null || routed.getType() != AudioDeviceInfo.TYPE_BLUETOOTH_SCO)
                && SystemClock.elapsedRealtime() < deadline) {
            Thread.sleep(50);
            routed = current.getRoutedDevice();
        }
        return routed;
    }

    private AudioDeviceInfo findBluetoothScoInput() {
        for (AudioDeviceInfo device : manager.getDevices(AudioManager.GET_DEVICES_INPUTS))
            if (device.isSource() && VoiceAudioRoute.isLegacyScoInput(device.getType())) return device;
        return null;
    }

    private void restoreBluetoothOutputForActiveScoInput(AudioDeviceInfo activeInput) {
        if (activeInput == null || activeInput.getType() != AudioDeviceInfo.TYPE_BLUETOOTH_SCO
                || manager == null || !bluetoothAudioRequested()) return;
        AudioDeviceInfo output = VoiceAudioRoute.bluetoothCommunicationOutput(manager);
        if (output == null) return;
        synchronized (this) {
            VoiceAudioRoute.selectBluetooth(manager, output);
            scoRequested = true;
            scoConnected = true;
            scoConnectFailed = false;
            selectedCommunicationDeviceId = output.getId();
            audioRoute = VoiceAudioRoute.deviceName(output);
            audioRouteInitialized = true;
            writeStatus(true);
        }
    }

    private boolean hasScoOutputDevice() {
        for (AudioDeviceInfo device : manager.getDevices(AudioManager.GET_DEVICES_OUTPUTS))
            if (device.getType() == AudioDeviceInfo.TYPE_BLUETOOTH_SCO) return true;
        return false;
    }

    private boolean bluetoothAudioRequested() {
        android.content.SharedPreferences preferences = getSharedPreferences("voice", 0);
        String route = preferences.getString("audio_route_mode",
                preferences.getBoolean("bluetooth_audio", false) ? "bluetooth" : VoiceAudioRoute.PHONE);
        return "bluetooth".equals(route) || "duplicate".equals(route);
    }

    private boolean duplicateOutputRequested() {
        return "duplicate".equals(getSharedPreferences("voice", 0).getString("audio_route_mode", VoiceAudioRoute.PHONE))
                && !"bluetooth_unavailable".equals(audioRoute);
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
        running = false; active = false; ServerService.clearVoiceStartRequest();
        trace(this, VoiceLifecycleTrace.Event.DESTROY_REQUESTED, VoiceLifecycleTrace.Reason.SERVICE, serviceGeneration);
        if (routeReceiverRegistered) { unregisterReceiver(routeReceiver); routeReceiverRegistered = false; }
        if (scoReceiverRegistered) { unregisterReceiver(scoReceiver); scoReceiverRegistered = false; }
        if (audioDeviceCallbackRegistered) { manager.unregisterAudioDeviceCallback(audioDeviceCallback); audioDeviceCallbackRegistered = false; }
        AudioRecord current = recorder; if (current != null) { try { current.stop(); } catch (IllegalStateException ignored) {} }
        if (capture != null) capture.interrupt();
        else captureCleaned.complete(null);
        java.util.concurrent.CompletableFuture<Boolean> endCleaned = endSession(VoiceSessionSignals.EndReason.STOP);
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        if (!status.equals("error")) status = "off";
        writeStatus(true);
        stopForeground(true); super.onDestroy();
        java.util.concurrent.CompletableFuture.allOf(captureCleaned, endCleaned).whenComplete((ignored, error) -> {
            try {
                VoiceAudioRoute.release(manager);
                trace(this, VoiceLifecycleTrace.Event.ROUTE_RELEASED, VoiceLifecycleTrace.Reason.SERVICE, serviceGeneration);
                audioRoute = VoiceAudioRoute.PHONE;
                writeStatus(true);
                if (cues != null) cues.close();
                lifecycleCleanupFailed = error != null;
                trace(this, error == null ? VoiceLifecycleTrace.Event.DRAINED : VoiceLifecycleTrace.Event.DRAIN_FAILED,
                        VoiceLifecycleTrace.Reason.SERVICE, serviceGeneration);
                if (error != null) writeStatus(true);
                if (error == null) serviceCleaned.complete(null);
                else serviceCleaned.completeExceptionally(error);
            } catch (RuntimeException | LinkageError cleanupError) {
                lifecycleCleanupFailed = true;
                trace(this, VoiceLifecycleTrace.Event.DRAIN_FAILED, VoiceLifecycleTrace.Reason.SERVICE, serviceGeneration);
                writeStatus(true);
                serviceCleaned.completeExceptionally(cleanupError);
            }
        });
        if (instance == this) instance = null;
    }
}
