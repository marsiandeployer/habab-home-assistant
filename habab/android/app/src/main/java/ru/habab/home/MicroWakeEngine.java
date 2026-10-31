package ru.habab.home;

import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.MappedByteBuffer;
import java.nio.channels.FileChannel;
import java.security.MessageDigest;
import java.util.Arrays;
import org.tensorflow.lite.DataType;
import org.tensorflow.lite.Interpreter;

/** Stateful microWakeWord detector using the exact MicroFrontend and streaming TFLite model. */
final class MicroWakeEngine implements WakeEngine {
    private static final int FEATURE_STRIDE = 3;
    private static final int FEATURE_COUNT = MicroFrontend.FEATURES_PER_FRAME;
    private static final int INPUT_BYTES = FEATURE_STRIDE * FEATURE_COUNT;
    private static final float ATTEMPT_FLOOR = 0.5f;

    private final WakeEngineInfo info;
    private final WakeDetector.Diagnostics diagnostics;
    private final MicroFrontend frontend;
    private final Interpreter interpreter;
    private final ByteBuffer input = ByteBuffer.allocateDirect(INPUT_BYTES).order(ByteOrder.nativeOrder());
    private final ByteBuffer output = ByteBuffer.allocateDirect(1).order(ByteOrder.nativeOrder());
    private final float[] inferenceFeatures = new float[INPUT_BYTES];
    private final float[] scoreWindow;
    private final double inputScale;
    private final int inputZeroPoint;
    private final double outputScale;
    private final int outputZeroPoint;
    private int featureCount;
    private int scoreCount;
    private int scorePosition;
    private long attemptSequence;
    private long currentAttempt;
    private long attemptStartedAt;
    private long lastMatchAttempt;
    private boolean attemptFired;
    private boolean closed;
    private long lastScoreTelemetryAt = -1;
    private float scoreTelemetryPeak;
    private long lastInferenceMicros;

    MicroWakeEngine(File modelDir, WakeEngineInfo info, WakeDetector.Diagnostics diagnostics)
            throws Exception {
        if (modelDir == null || info == null || diagnostics == null)
            throw new IllegalArgumentException("micro_engine_arguments");
        if (!"microwakeword".equals(info.engineId))
            throw new IllegalArgumentException("micro_engine_identity");
        File modelFile = new File(modelDir, "classifier.tflite");
        if (!modelFile.isFile() || !modelFile.canRead()) throw new IOException("wake_model_missing");
        String actualHash = sha256(modelFile);
        if (!actualHash.equals(info.modelSha256)) throw new IOException("wake_model_hash_mismatch");

        this.info = info;
        this.diagnostics = diagnostics;
        MappedByteBuffer model = mapModel(modelFile);
        Interpreter created = null;
        MicroFrontend createdFrontend = null;
        try {
            created = new Interpreter(model, new Interpreter.Options().setNumThreads(2));
            validateModel(created);
            org.tensorflow.lite.Tensor inputTensor = created.getInputTensor(0);
            org.tensorflow.lite.Tensor outputTensor = created.getOutputTensor(0);
            this.inputScale = inputTensor.quantizationParams().getScale();
            this.inputZeroPoint = inputTensor.quantizationParams().getZeroPoint();
            this.outputScale = outputTensor.quantizationParams().getScale();
            this.outputZeroPoint = outputTensor.quantizationParams().getZeroPoint();
            if (!Double.isFinite(inputScale) || inputScale <= 0 || !Double.isFinite(outputScale)
                    || outputScale <= 0)
                throw new IOException("wake_model_quantization");
            createdFrontend = new MicroFrontend();
            this.scoreWindow = new float[info.slidingWindow];
            this.interpreter = created;
            this.frontend = createdFrontend;
        } catch (Exception | LinkageError error) {
            if (createdFrontend != null) createdFrontend.close();
            if (created != null) created.close();
            throw error;
        }
    }

    @Override public synchronized boolean accept(short[] samples, int count, long now) throws Exception {
        ensureOpen();
        if (samples == null || count <= 0 || count > samples.length || count > 1600)
            throw new IllegalArgumentException("wake_audio_chunk_size");
        float[] features = frontend.process(samples, count);
        int frames = MicroFrontend.frameCount(features);
        if (frames > 10) {
            MicroFrontend.zero(features);
            throw new IOException("wake_frontend_frame_overflow");
        }
        boolean detected = false;
        try {
            for (int frame = 0; frame < frames; frame++) {
                System.arraycopy(features, frame * FEATURE_COUNT, inferenceFeatures,
                        featureCount * FEATURE_COUNT, FEATURE_COUNT);
                featureCount++;
                if (featureCount == FEATURE_STRIDE) {
                    float score = infer();
                    detected |= observeScore(score, now);
                }
            }
            return detected;
        } finally {
            MicroFrontend.zero(features);
        }
    }

    private float infer() throws IOException {
        input.clear();
        for (float feature : inferenceFeatures) input.put(quantizeFeature(feature, inputScale, inputZeroPoint));
        input.rewind();
        output.clear();
        long inferenceStarted = System.nanoTime();
        interpreter.run(input, output);
        lastInferenceMicros = Math.max(0, (System.nanoTime() - inferenceStarted) / 1000);
        output.rewind();
        int encoded = output.get() & 0xff;
        float score = (float) ((encoded - outputZeroPoint) * outputScale);
        if (!Float.isFinite(score) || score < 0f || score > 1f)
            throw new IOException("wake_model_score_range");
        Arrays.fill(inferenceFeatures, 0f);
        featureCount = 0;
        return score;
    }

    private boolean observeScore(float score, long now) {
        scoreWindow[scorePosition] = score;
        scorePosition = (scorePosition + 1) % scoreWindow.length;
        scoreCount = Math.min(scoreCount + 1, scoreWindow.length);
        if (scoreCount < scoreWindow.length) return false;
        float mean = meanScore(scoreWindow, scoreCount);

        if (mean >= ATTEMPT_FLOOR && currentAttempt == 0) {
            currentAttempt = ++attemptSequence;
            attemptStartedAt = now;
        }
        boolean accepted = mean >= info.threshold && !attemptFired;
        long attempt = currentAttempt == 0 ? attemptSequence : currentAttempt;
        scoreTelemetryPeak = Math.max(scoreTelemetryPeak, mean);
        if (currentAttempt != 0 && (lastScoreTelemetryAt < 0 || now - lastScoreTelemetryAt >= 2000)) {
            diagnostics.record(VoiceLifecycleTrace.Event.WAKE_MODEL_SCORE,
                    VoiceLifecycleTrace.Reason.PARTIAL, attempt, now, -1, -1, -1,
                    -1, Math.round(scoreTelemetryPeak * 1000),
                    attemptStartedAt == 0 ? -1 : Math.max(0, now - attemptStartedAt));
            lastScoreTelemetryAt = now; scoreTelemetryPeak = 0;
        }
        if (accepted) {
            lastMatchAttempt = attempt;
            attemptFired = true;
            android.util.Log.i("HababWakeModel", "event=first_detection engine=" + info.engineId
                    + " model=" + info.modelId + " version=" + info.modelVersion
                    + " score=" + mean + " threshold=" + info.threshold
                    + " inference_us=" + lastInferenceMicros);
            diagnostics.record(VoiceLifecycleTrace.Event.WAKE_ACCEPTED,
                    VoiceLifecycleTrace.Reason.EXACT_FINAL, attempt, now, -1, -1, -1,
                    1, Math.round(mean * 1000), Math.max(0, now - attemptStartedAt));
            return true;
        }
        if (currentAttempt != 0 && mean < ATTEMPT_FLOOR) {
            diagnostics.record(VoiceLifecycleTrace.Event.WAKE_REJECTED,
                    VoiceLifecycleTrace.Reason.LOW_CONFIDENCE, currentAttempt, now,
                    -1, -1, -1, 0, Math.round(mean * 1000),
                    Math.max(0, now - attemptStartedAt));
            currentAttempt = 0;
            attemptFired = false;
            attemptStartedAt = 0;
        }
        return false;
    }

    static float meanScore(float[] values, int count) {
        if (values == null || count <= 0 || count > values.length)
            throw new IllegalArgumentException("wake_score_window");
        double total = 0;
        for (int i = 0; i < count; i++) {
            if (!Float.isFinite(values[i])) throw new IllegalArgumentException("wake_score_window");
            total += values[i];
        }
        return (float) (total / count);
    }

    static byte quantizeFeature(float value, double scale, int zeroPoint) throws IOException {
        if (!Float.isFinite(value) || !Double.isFinite(scale) || scale <= 0)
            throw new IOException("wake_feature_quantization");
        long quantized = Math.round(value / scale) + zeroPoint;
        return (byte) Math.max(Byte.MIN_VALUE, Math.min(Byte.MAX_VALUE, quantized));
    }

    static float dequantizeScore(int value, double scale, int zeroPoint) throws IOException {
        if (value < 0 || value > 255 || !Double.isFinite(scale) || scale <= 0)
            throw new IOException("wake_score_quantization");
        float score = (float) ((value - zeroPoint) * scale);
        if (!Float.isFinite(score) || score < 0 || score > 1)
            throw new IOException("wake_model_score_range");
        return score;
    }

    private static void validateModel(Interpreter interpreter) throws IOException {
        if (interpreter.getInputTensorCount() != 1 || interpreter.getOutputTensorCount() != 1)
            throw new IOException("wake_model_tensor_count");
        org.tensorflow.lite.Tensor in = interpreter.getInputTensor(0);
        org.tensorflow.lite.Tensor out = interpreter.getOutputTensor(0);
        if (in.dataType() != DataType.INT8 || !Arrays.equals(in.shape(), new int[]{1, 3, 40})
                || out.dataType() != DataType.UINT8 || !Arrays.equals(out.shape(), new int[]{1, 1}))
            throw new IOException("wake_model_tensor_shape");
    }

    private static MappedByteBuffer mapModel(File file) throws IOException {
        try (FileInputStream stream = new FileInputStream(file);
             FileChannel channel = stream.getChannel()) {
            return channel.map(FileChannel.MapMode.READ_ONLY, 0, channel.size());
        }
    }

    private static String sha256(File file) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        try (FileInputStream stream = new FileInputStream(file)) {
            byte[] buffer = new byte[8192];
            int read;
            while ((read = stream.read(buffer)) != -1) digest.update(buffer, 0, read);
            Arrays.fill(buffer, (byte) 0);
        }
        StringBuilder hex = new StringBuilder(64);
        for (byte value : digest.digest()) hex.append(String.format(java.util.Locale.ROOT, "%02x", value & 0xff));
        return hex.toString();
    }

    private void ensureOpen() {
        if (closed) throw new IllegalStateException("wake_engine_closed");
    }

    @Override public synchronized void reset(long now, VoiceLifecycleTrace.Reason reason) {
        if (closed) return;
        frontend.reset();
        interpreter.resetVariableTensors();
        Arrays.fill(inferenceFeatures, 0f);
        Arrays.fill(scoreWindow, 0f);
        input.clear(); while (input.hasRemaining()) input.put((byte) 0);
        output.clear(); while (output.hasRemaining()) output.put((byte) 0);
        featureCount = scoreCount = scorePosition = 0;
        currentAttempt = 0;
        attemptFired = false;
        attemptStartedAt = 0;
        lastMatchAttempt = 0;
        lastScoreTelemetryAt = -1;
        scoreTelemetryPeak = 0;
    }

    @Override public WakeMatchGate.MatchSource lastMatchSource() { return null; }
    @Override public synchronized long lastMatchAttempt() { return lastMatchAttempt; }
    @Override public void setDiagnosticCaptureEnabled(boolean enabled) { /* no audio capture in model backend */ }
    @Override public WakeVerifier.Result consumeDiagnosticResult() { return null; }
    @Override public short[] consumeVerifiedAudio() { return null; }
    @Override public WakeEngineInfo info() { return info; }

    @Override public synchronized void close() {
        if (closed) return;
        closed = true;
        frontend.close();
        interpreter.close();
        Arrays.fill(inferenceFeatures, 0f);
        Arrays.fill(scoreWindow, 0f);
        input.clear(); while (input.hasRemaining()) input.put((byte) 0);
        output.clear(); while (output.hasRemaining()) output.put((byte) 0);
    }
}
