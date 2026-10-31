package ru.habab.home;

import ai.onnxruntime.OnnxTensor;
import ai.onnxruntime.OrtEnvironment;
import ai.onnxruntime.OrtSession;
import java.io.File;
import java.io.IOException;
import java.nio.FloatBuffer;
import java.util.Arrays;
import java.util.Map;

/** Local ONNX wake scorer. Capture only copies PCM; all model work runs on its worker. */
final class OnnxWakeEngine implements WakeEngine {
    private static final int MAX_PENDING_HOPS = 8;
    private static final int MAX_JOIN_MS = 1500;

    private static final class HopContext {
        final long generation, attempt, attemptStartedAt, sampleTime;
        final int rms, vad;
        HopContext(long generation, long attempt, long attemptStartedAt, long sampleTime,
                int rms, int vad) {
            this.generation = generation; this.attempt = attempt;
            this.attemptStartedAt = attemptStartedAt; this.sampleTime = sampleTime;
            this.rms = rms; this.vad = vad;
        }
    }
    private static final class Snapshot {
        final short[] audio;
        final HopContext[] contexts;
        final long generation;
        Snapshot(short[] audio, HopContext[] contexts, long generation) {
            this.audio = audio; this.contexts = contexts; this.generation = generation;
        }
    }
    private static final class Detection {
        final long generation, attempt;
        Detection(long generation, long attempt) { this.generation = generation; this.attempt = attempt; }
    }

    private final Object lock = new Object();
    private final WakeEngineInfo info;
    private final WakeDetector.Diagnostics diagnostics;
    private final String modeId;
    private final OnnxWakeFrontend frontend;
    private final Thread worker;
    private final int hopSamples;
    private final short[] partialHop;
    private final short[] pendingAudio;
    private final HopContext[] pendingContexts;
    private final short[] livekitCaptureRing;
    private final VoiceActivityGate vadGate = new VoiceActivityGate();
    private int partialSize, pendingHopCount;
    private int livekitCapturePosition, livekitCaptureSize;
    private Snapshot livekitPending;
    private long generation, attemptSequence, currentAttempt, attemptStartedAt;
    private long lastScoredSampleTime = -1, lastScoredCadenceMs = -1;
    private long lastModelScoreTelemetryAt = -1;
    private long firstDetectionGeneration = -1;
    private Detection availableDetection;
    private volatile Detection detected;
    private volatile float lastScore = Float.NaN;
    private volatile long lastInferenceMs = -1;
    private boolean wasSpeech, diagnosticCaptureEnabled, closed, resetPending;
    private volatile Throwable workerFailure;
    private volatile long scoreSequence;
    private volatile WakeMatchGate.MatchSource lastMatchSource;
    private volatile long lastMatchAttempt = -1;

    OnnxWakeEngine(File modelDirectory, WakeEngineInfo info,
            WakeDetector.Diagnostics diagnostics, String modeId) throws Exception {
        if (modelDirectory == null || !modelDirectory.isDirectory()) throw new IOException("wake_model_directory_missing");
        if (!"openwakeword".equals(modeId) && !"livekit".equals(modeId)) throw new IOException("unsupported_wake_mode");
        if (info == null || !modeId.equals(info.engineId)) throw new IOException("wake_engine_identity_mismatch");
        this.info = info;
        this.diagnostics = diagnostics;
        this.modeId = modeId;
        OrtEnvironment environment = OrtEnvironment.getEnvironment();
        OrtSession mel = null, embedding = null, classifier = null;
        try {
            mel = createSession(environment, new File(modelDirectory, "melspectrogram.onnx"));
            embedding = createSession(environment, new File(modelDirectory, "embedding_model.onnx"));
            classifier = createSession(environment, new File(modelDirectory, "classifier.onnx"));
            this.frontend = new OnnxWakeFrontend(modeId, new OrtRunner(environment, mel, "input"),
                    new OrtRunner(environment, embedding, "input_1"),
                    new OrtRunner(environment, classifier, "embeddings"), this::onScore);
        } catch (Exception | LinkageError error) {
            closeSession(classifier); closeSession(embedding); closeSession(mel);
            throw error;
        }
        hopSamples = frontend.samplesPerCall();
        if (info.hopMs > 0 && info.hopMs != frontend.cadenceMs()) {
            frontend.close();
            throw new IOException("wake_model_hop_mismatch");
        }
        if (info.contextFrames > 0 && info.contextFrames != OnnxWakeFrontend.CLASSIFIER_WINDOW) {
            frontend.close();
            throw new IOException("wake_model_context_mismatch");
        }
        partialHop = new short[hopSamples];
        pendingAudio = new short[MAX_PENDING_HOPS * hopSamples];
        pendingContexts = new HopContext[MAX_PENDING_HOPS];
        livekitCaptureRing = "livekit".equals(modeId)
                ? new short[OnnxWakeFrontend.LIVEKIT_WINDOW_SAMPLES] : null;
        worker = new Thread(this::runWorker, "HababOnnxWake-" + modeId);
        worker.setDaemon(true);
        try { worker.start(); }
        catch (RuntimeException | Error error) { frontend.close(); throw error; }
        android.util.Log.i("HababWakeModel", "event=engine_ready engine=" + info.engineId
                + " model=" + info.modelId + " hop_ms=" + frontend.cadenceMs()
                + " context_frames=" + info.contextFrames
                + " mel_context_frames=" + OnnxWakeFrontend.MEL_WINDOW
                + " audio_window_ms=" + ("livekit".equals(modeId) ? 2000 : -1));
    }

    @Override public boolean accept(short[] samples, int count, long now) throws Exception {
        if (samples == null || count < 0 || count > samples.length) throw new IllegalArgumentException("wake_audio_count");
        synchronized (lock) {
            checkWorkerFailure();
            if (closed) throw new IOException("wake_engine_closed");
            Detection result = availableDetection;
            availableDetection = null;
            if (result != null && result.generation == generation) {
                lastMatchAttempt = result.attempt;
                lastMatchSource = null; // acoustic model source; VoiceService uses its acoustic fallback.
                return true;
            }
            boolean speech = vadGate.speech(samples, count);
            if (speech && !wasSpeech) {
                attemptStartedAt = now;
                currentAttempt = ++attemptSequence;
                firstDetectionGeneration = -1;
            }
            wasSpeech = speech;
            int rms = rms(samples, count);
            for (int i = 0; i < count; i++) {
                partialHop[partialSize++] = samples[i];
                if (livekitCaptureRing != null) {
                    livekitCaptureRing[livekitCapturePosition] = samples[i];
                    livekitCapturePosition = (livekitCapturePosition + 1) % livekitCaptureRing.length;
                    livekitCaptureSize = Math.min(livekitCaptureRing.length, livekitCaptureSize + 1);
                }
                if (partialSize == hopSamples) {
                    if (livekitCaptureRing != null) {
                        if (livekitCaptureSize == livekitCaptureRing.length) {
                            int first = livekitCapturePosition;
                            short[] window = new short[livekitCaptureRing.length];
                            for (int j = 0; j < window.length; j++)
                                window[j] = livekitCaptureRing[(first + j) % livekitCaptureRing.length];
                            HopContext context = new HopContext(generation, currentAttempt,
                                    attemptStartedAt, now, rms, speech ? 1 : 0);
                            if (livekitPending != null) Arrays.fill(livekitPending.audio, (short) 0);
                            livekitPending = new Snapshot(window, new HopContext[]{context}, generation);
                        }
                        Arrays.fill(partialHop, (short) 0);
                        partialSize = 0;
                        lock.notifyAll();
                        continue;
                    }
                    if (pendingHopCount >= MAX_PENDING_HOPS) throw new IOException("wake_inference_backpressure");
                    int hop = pendingHopCount++;
                    System.arraycopy(partialHop, 0, pendingAudio,
                            hop * hopSamples, partialHop.length);
                    pendingContexts[hop] = new HopContext(generation, currentAttempt,
                            attemptStartedAt, now, rms, speech ? 1 : 0);
                    Arrays.fill(partialHop, (short) 0);
                    partialSize = 0;
                    lock.notifyAll();
                }
            }
            return false;
        }
    }

    private void runWorker() {
        try {
            for (;;) {
                Snapshot snapshot;
                synchronized (lock) {
                    while (!closed && pendingHopCount == 0 && livekitPending == null && !resetPending) lock.wait();
                    if (closed) return;
                    if (resetPending) {
                        resetPending = false;
                        frontend.reset();
                        lastScore = Float.NaN;
                        lastInferenceMs = -1;
                    }
                    snapshot = livekitCaptureRing == null ? takeSnapshot() : takeLivekitSnapshot();
                }
                if (snapshot == null) continue;
                if (livekitCaptureRing != null) {
                    HopContext context = snapshot.contexts[0];
                    long previousScoreSequence = scoreSequence;
                    if (isCurrent(context.generation))
                        frontend.processLivekitWindow(snapshot.audio, snapshot.audio.length);
                    if (!isCurrent(context.generation)) {
                        lastScore = Float.NaN;
                        lastInferenceMs = -1;
                    } else if (scoreSequence != previousScoreSequence) {
                        float score = lastScore;
                        reportScore(context, score);
                        acceptScore(context, score);
                    }
                    Arrays.fill(snapshot.audio, (short) 0);
                    continue;
                }
                for (int hop = 0; hop < snapshot.contexts.length; hop++) {
                    HopContext context = snapshot.contexts[hop];
                    if (!isCurrent(context.generation)) break;
                    long previousScoreSequence = scoreSequence;
                    short[] pcm = Arrays.copyOfRange(snapshot.audio,
                            hop * hopSamples,
                            (hop + 1) * hopSamples);
                    try { frontend.processHop(pcm, pcm.length); }
                    finally { Arrays.fill(pcm, (short) 0); }
                    if (!isCurrent(context.generation)) {
                        lastScore = Float.NaN;
                        lastInferenceMs = -1;
                        break;
                    }
                    if (scoreSequence == previousScoreSequence) continue;
                    float score = lastScore;
                    reportScore(context, score);
                    acceptScore(context, score);
                }
                Arrays.fill(snapshot.audio, (short) 0);
            }
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
            if (!closed) fail(new IOException("wake_worker_interrupted", interrupted));
        } catch (Throwable error) {
            fail(error);
        } finally {
            try { frontend.close(); }
            catch (Exception error) { if (!closed) workerFailure = error; }
            synchronized (lock) { lock.notifyAll(); }
        }
    }

    private Snapshot takeSnapshot() {
        if (pendingHopCount == 0) return null;
        int samples = pendingHopCount * hopSamples;
        short[] audio = Arrays.copyOf(pendingAudio, samples);
        HopContext[] contexts = Arrays.copyOf(pendingContexts, pendingHopCount);
        Arrays.fill(pendingAudio, 0, samples, (short) 0);
        Arrays.fill(pendingContexts, 0, pendingHopCount, null);
        pendingHopCount = 0;
        return new Snapshot(audio, contexts, generation);
    }

    private Snapshot takeLivekitSnapshot() {
        Snapshot snapshot = livekitPending;
        livekitPending = null;
        return snapshot;
    }

    private void acceptScore(HopContext context, float score) {
        if (Float.isFinite(score) && score >= info.threshold) {
            synchronized (lock) {
                if (!closed && generation == context.generation
                        && firstDetectionGeneration != context.generation) {
                    firstDetectionGeneration = context.generation;
                    detected = new Detection(context.generation, context.attempt);
                    availableDetection = detected;
                    android.util.Log.i("HababWakeModel", "event=first_detection engine="
                            + info.engineId + " model=" + info.modelId + " version="
                            + info.modelVersion + " score=" + score + " threshold="
                            + info.threshold + " inference_ms=" + lastInferenceMs
                            + " cadence_ms=" + frontend.cadenceMs()
                            + " sample_cadence_ms=" + lastScoredCadenceMs);
                    record(VoiceLifecycleTrace.Event.WAKE_ACCEPTED,
                            VoiceLifecycleTrace.Reason.ENDPOINT, context,
                            permille(score), Math.max(0, context.sampleTime - context.attemptStartedAt));
                }
            }
        }
    }

    private boolean isCurrent(long expected) {
        synchronized (lock) { return !closed && generation == expected; }
    }

    private void onScore(float score, long inferenceMs) {
        lastScore = score;
        lastInferenceMs = inferenceMs;
        scoreSequence++;
    }

    private void reportScore(HopContext context, float score) {
        if (!Float.isFinite(score) || context.vad != 1) return;
        if (lastScoredSampleTime >= 0 && context.sampleTime >= lastScoredSampleTime)
            lastScoredCadenceMs = context.sampleTime - lastScoredSampleTime;
        lastScoredSampleTime = context.sampleTime;
        if (scoreTelemetryDue(lastModelScoreTelemetryAt, context.sampleTime, context.vad)) {
            record(VoiceLifecycleTrace.Event.WAKE_MODEL_SCORE, VoiceLifecycleTrace.Reason.PARTIAL,
                    context, permille(score), Math.max(0, context.sampleTime - context.attemptStartedAt));
            lastModelScoreTelemetryAt = context.sampleTime;
        }
    }

    static boolean scoreTelemetryDue(long previousAt, long now, int vadSpeech) {
        return vadSpeech == 1 && (previousAt < 0 || now - previousAt >= 2000);
    }

    private void record(VoiceLifecycleTrace.Event event, VoiceLifecycleTrace.Reason reason,
            HopContext context, int score, long elapsedFromWake) {
        if (diagnostics != null) diagnostics.record(event, reason, context.attempt,
                context.sampleTime, context.rms, context.vad, -1, -1, score, elapsedFromWake);
    }

    @Override public void reset(long now, VoiceLifecycleTrace.Reason reason) {
        synchronized (lock) {
            if (closed) return;
            generation++;
            Arrays.fill(partialHop, (short) 0);
            Arrays.fill(pendingAudio, (short) 0);
            Arrays.fill(pendingContexts, null);
            if (livekitCaptureRing != null) Arrays.fill(livekitCaptureRing, (short) 0);
            livekitCapturePosition = livekitCaptureSize = 0;
            if (livekitPending != null) Arrays.fill(livekitPending.audio, (short) 0);
            livekitPending = null;
            partialSize = pendingHopCount = 0;
            currentAttempt = ++attemptSequence;
            attemptStartedAt = now;
            wasSpeech = false;
            lastMatchSource = null;
            lastMatchAttempt = -1;
            availableDetection = detected = null;
            firstDetectionGeneration = -1;
            lastScoredSampleTime = lastScoredCadenceMs = -1;
            lastModelScoreTelemetryAt = -1;
            lastScore = Float.NaN;
            lastInferenceMs = -1;
            resetPending = true;
            lock.notifyAll();
        }
    }

    @Override public WakeMatchGate.MatchSource lastMatchSource() { return lastMatchSource; }
    @Override public long lastMatchAttempt() { return lastMatchAttempt; }
    @Override public void setDiagnosticCaptureEnabled(boolean enabled) { diagnosticCaptureEnabled = enabled; }
    @Override public WakeVerifier.Result consumeDiagnosticResult() { return null; }
    @Override public short[] consumeVerifiedAudio() { return null; }
    @Override public WakeEngineInfo info() { return info; }

    @Override public void close() {
        synchronized (lock) {
            if (closed) return;
            closed = true;
            generation++;
            Arrays.fill(partialHop, (short) 0);
            Arrays.fill(pendingAudio, (short) 0);
            Arrays.fill(pendingContexts, null);
            if (livekitCaptureRing != null) Arrays.fill(livekitCaptureRing, (short) 0);
            livekitCapturePosition = livekitCaptureSize = 0;
            if (livekitPending != null) Arrays.fill(livekitPending.audio, (short) 0);
            livekitPending = null;
            pendingHopCount = partialSize = 0;
            availableDetection = detected = null;
            lock.notifyAll();
        }
        try { worker.join(MAX_JOIN_MS); }
        catch (InterruptedException interrupted) { Thread.currentThread().interrupt(); }
    }

    private void checkWorkerFailure() throws IOException {
        Throwable failure = workerFailure;
        if (failure != null) throw new IOException("wake_inference_failed", failure);
    }
    private void fail(Throwable error) {
        synchronized (lock) {
            if (closed) return;
            workerFailure = error;
            Arrays.fill(pendingAudio, (short) 0);
            Arrays.fill(pendingContexts, null);
            if (livekitCaptureRing != null) Arrays.fill(livekitCaptureRing, (short) 0);
            livekitCapturePosition = livekitCaptureSize = 0;
            if (livekitPending != null) Arrays.fill(livekitPending.audio, (short) 0);
            livekitPending = null;
            pendingHopCount = 0;
            lock.notifyAll();
        }
    }

    private static int rms(short[] samples, int count) {
        if (count == 0) return 0;
        double power = 0;
        for (int i = 0; i < count; i++) power += (double) samples[i] * samples[i];
        return (int) Math.round(Math.sqrt(power / count));
    }
    private static int permille(float value) { return (int) Math.round(value * 1000); }

    private static OrtSession createSession(OrtEnvironment environment, File model) throws Exception {
        if (!model.isFile() || model.length() == 0) throw new IOException("wake_model_file_missing");
        OrtSession.SessionOptions options = new OrtSession.SessionOptions();
        try {
            options.setIntraOpNumThreads(1);
            options.setInterOpNumThreads(1);
            options.setExecutionMode(OrtSession.SessionOptions.ExecutionMode.SEQUENTIAL);
            return environment.createSession(model.getAbsolutePath(), options);
        } finally { options.close(); }
    }
    private static void closeSession(OrtSession session) {
        if (session != null) try { session.close(); } catch (Exception ignored) { }
    }

    private static final class OrtRunner implements OnnxWakeFrontend.Runner {
        private final OrtEnvironment environment;
        private final OrtSession session;
        private final String inputName;
        OrtRunner(OrtEnvironment environment, OrtSession session, String inputName) {
            this.environment = environment; this.session = session; this.inputName = inputName;
        }
        @Override public float[] run(float[] input, long... shape) throws Exception {
            long expected = 1;
            for (long dimension : shape) expected *= dimension;
            if (expected != input.length) throw new IOException("invalid_onnx_input_shape");
            try (OnnxTensor tensor = OnnxTensor.createTensor(environment, FloatBuffer.wrap(input), shape);
                 OrtSession.Result result = session.run(Map.of(inputName, tensor))) {
                return OnnxWakeFrontend.flatten(result.get(0).getValue());
            }
        }
        @Override public void close() throws Exception { session.close(); }
    }
}
