package ru.habab.home;

import java.io.IOException;
import java.lang.reflect.Array;
import java.util.Arrays;

/** ONNX feature pipelines for streaming openWakeWord and stateless LiveKit. */
final class OnnxWakeFrontend implements AutoCloseable {
    static final int SAMPLE_RATE = 16000;
    static final int HOP_SAMPLES = 1280;
    static final int LIVEKIT_HOP_SAMPLES = 2560;
    static final int LIVEKIT_WINDOW_SAMPLES = 32000;
    static final int MEL_BINS = 32;
    static final int MEL_CONTEXT = 480;
    static final int MEL_WINDOW = 76;
    static final int EMBEDDING_SIZE = 96;
    static final int CLASSIFIER_WINDOW = 16;
    static final float THRESHOLD = 0.5f;

    interface Runner extends AutoCloseable {
        float[] run(float[] input, long... shape) throws Exception;
        @Override default void close() throws Exception {}
    }
    interface ScoreListener { void onScore(float score, long inferenceMs); }

    private final String modeId;
    private final Runner mel, embedding, classifier;
    private final ScoreListener listener;
    private final float[] melRing = new float[MEL_WINDOW * MEL_BINS];
    private final float[] embeddingRing = new float[CLASSIFIER_WINDOW * EMBEDDING_SIZE];
    private final short[] rawRing = new short[HOP_SAMPLES + MEL_CONTEXT];
    private final float[] livekitAudio = new float[LIVEKIT_WINDOW_SAMPLES];
    private int rawPosition, rawSize, melFrames, melWritePosition, embeddingCount, embeddingPosition;
    private int livekitPosition, livekitSize;
    private long livekitSamplesSeen;
    private boolean closed;

    OnnxWakeFrontend(String modeId, Runner mel, Runner embedding, Runner classifier,
            ScoreListener listener) {
        if (!"openwakeword".equals(modeId) && !"livekit".equals(modeId))
            throw new IllegalArgumentException("unsupported_wake_mode");
        this.modeId = modeId;
        this.mel = mel;
        this.embedding = embedding;
        this.classifier = classifier;
        this.listener = listener;
        // openWakeWord initializes its streaming mel history to ones.
        Arrays.fill(melRing, 1f);
        melFrames = MEL_WINDOW;
    }

    /** Consume one mode-specific block; the caller keeps ONNX work off the capture thread. */
    int samplesPerCall() { return "livekit".equals(modeId) ? LIVEKIT_HOP_SAMPLES : HOP_SAMPLES; }
    int cadenceMs() { return samplesPerCall() * 1000 / SAMPLE_RATE; }

    void processHop(short[] pcm, int count) throws Exception {
        if (closed) throw new IllegalStateException("wake_frontend_closed");
        if (pcm == null || count != samplesPerCall() || count > pcm.length)
            throw new IllegalArgumentException("wake_hop_size");
        long started = System.nanoTime();
        if ("livekit".equals(modeId)) {
            processLivekit(pcm, count, started);
            return;
        }
        appendRaw(pcm, count);
        int inputCount = Math.min(rawSize, HOP_SAMPLES + MEL_CONTEXT);
        float[] audio = new float[inputCount];
        int first = (rawPosition - inputCount + rawRing.length) % rawRing.length;
        boolean normalized = "livekit".equals(modeId);
        for (int i = 0; i < inputCount; i++) {
            float value = rawRing[(first + i) % rawRing.length];
            audio[i] = normalized ? value / 32768f : value;
        }

        float[] melOutput = mel.run(audio, 1, inputCount);
        if (melOutput.length == 0 || melOutput.length % MEL_BINS != 0)
            throw new IOException("invalid_melspectrogram_shape");
        int frames = melOutput.length / MEL_BINS;
        if (frames > 16) throw new IOException("melspectrogram_frame_overflow");
        int frameOffset = Math.max(0, frames - count / 160);
        for (int frame = frameOffset; frame < frames; frame++) {
            int dst = melWritePosition * MEL_BINS;
            for (int bin = 0; bin < MEL_BINS; bin++)
                melRing[dst + bin] = melOutput[frame * MEL_BINS + bin] / 10f + 2f;
            melWritePosition = (melWritePosition + 1) % MEL_WINDOW;
            melFrames = Math.min(MEL_WINDOW, melFrames + 1);
        }

        float[] melWindow = new float[MEL_WINDOW * MEL_BINS];
        int startFrame = melFrames < MEL_WINDOW ? melFrames : melWritePosition;
        for (int frame = 0; frame < MEL_WINDOW; frame++) {
            int source = ((startFrame + frame) % MEL_WINDOW) * MEL_BINS;
            System.arraycopy(melRing, source, melWindow, frame * MEL_BINS, MEL_BINS);
        }
        float[] vector = embedding.run(melWindow, 1, MEL_WINDOW, MEL_BINS, 1);
        if (vector.length != EMBEDDING_SIZE) throw new IOException("invalid_embedding_shape");
        int embedDst = (embeddingPosition % CLASSIFIER_WINDOW) * EMBEDDING_SIZE;
        System.arraycopy(vector, 0, embeddingRing, embedDst, EMBEDDING_SIZE);
        embeddingPosition++;
        embeddingCount = Math.min(CLASSIFIER_WINDOW, embeddingCount + 1);
        if (embeddingCount < CLASSIFIER_WINDOW) return;

        float[] window = new float[CLASSIFIER_WINDOW * EMBEDDING_SIZE];
        int firstEmbedding = embeddingPosition % CLASSIFIER_WINDOW;
        for (int row = 0; row < CLASSIFIER_WINDOW; row++) {
            int source = ((firstEmbedding + row) % CLASSIFIER_WINDOW) * EMBEDDING_SIZE;
            System.arraycopy(embeddingRing, source, window, row * EMBEDDING_SIZE, EMBEDDING_SIZE);
        }
        float[] score = classifier.run(window, 1, CLASSIFIER_WINDOW, EMBEDDING_SIZE);
        if (score.length != 1 || !Float.isFinite(score[0]) || score[0] < 0f || score[0] > 1f)
            throw new IOException("invalid_classifier_score");
        listener.onScore(score[0], Math.max(0, (System.nanoTime() - started) / 1_000_000));
    }

    /** LiveKit's trained protocol is stateless: normalize a full rolling two-second window,
     * recompute all mel frames and all 16 embeddings at each 160 ms observation. */
    private void processLivekit(short[] pcm, int count, long started) throws Exception {
        for (int i = 0; i < count; i++) {
            livekitAudio[livekitPosition] = pcm[i] / 32768f;
            livekitPosition = (livekitPosition + 1) % LIVEKIT_WINDOW_SAMPLES;
        }
        livekitSize = Math.min(LIVEKIT_WINDOW_SAMPLES, livekitSize + count);
        livekitSamplesSeen += count;
        if (livekitSamplesSeen < LIVEKIT_WINDOW_SAMPLES) return;

        float[] window = new float[LIVEKIT_WINDOW_SAMPLES];
        int first = (livekitPosition - livekitSize + LIVEKIT_WINDOW_SAMPLES) % LIVEKIT_WINDOW_SAMPLES;
        int leftPadding = LIVEKIT_WINDOW_SAMPLES - livekitSize;
        for (int i = 0; i < livekitSize; i++)
            window[leftPadding + i] = livekitAudio[(first + i) % LIVEKIT_WINDOW_SAMPLES];
        runLivekit(window, started);
    }

    /** Score an already captured complete rolling window. Used by the stateless engine's
     * latest-window worker so an overloaded model never corrupts or partially drops history. */
    void processLivekitWindow(short[] pcm, int count) throws Exception {
        if (!"livekit".equals(modeId)) throw new IllegalStateException("not_livekit_frontend");
        if (closed) throw new IllegalStateException("wake_frontend_closed");
        if (pcm == null || count != LIVEKIT_WINDOW_SAMPLES || count > pcm.length)
            throw new IllegalArgumentException("livekit_window_size");
        long started = System.nanoTime();
        float[] window = new float[count];
        for (int i = 0; i < count; i++) window[i] = pcm[i] / 32768f;
        runLivekit(window, started);
    }

    private void runLivekit(float[] window, long started) throws Exception {
        float[] melOutput = mel.run(window, 1, LIVEKIT_WINDOW_SAMPLES);
        if (melOutput.length == 0 || melOutput.length % MEL_BINS != 0)
            throw new IOException("invalid_melspectrogram_shape");
        int frames = melOutput.length / MEL_BINS;
        if (frames < MEL_WINDOW) throw new IOException("insufficient_livekit_mels");
        int windows = (frames - MEL_WINDOW) / 8 + 1;
        if (windows < CLASSIFIER_WINDOW) throw new IOException("insufficient_livekit_embeddings");
        float[] melFeatures = new float[melOutput.length];
        for (int i = 0; i < melOutput.length; i++) melFeatures[i] = melOutput[i] / 10f + 2f;
        float[] melWindows = new float[windows * MEL_WINDOW * MEL_BINS];
        for (int index = 0; index < windows; index++)
            System.arraycopy(melFeatures, index * 8 * MEL_BINS,
                    melWindows, index * MEL_WINDOW * MEL_BINS, MEL_WINDOW * MEL_BINS);
        float[] allEmbeddings = embedding.run(melWindows, windows, MEL_WINDOW, MEL_BINS, 1);
        if (allEmbeddings.length != windows * EMBEDDING_SIZE)
            throw new IOException("invalid_embedding_shape");
        float[] sequence = new float[CLASSIFIER_WINDOW * EMBEDDING_SIZE];
        System.arraycopy(allEmbeddings, (windows - CLASSIFIER_WINDOW) * EMBEDDING_SIZE,
                sequence, 0, sequence.length);
        float[] score = classifier.run(sequence, 1, CLASSIFIER_WINDOW, EMBEDDING_SIZE);
        if (score.length != 1 || !Float.isFinite(score[0]) || score[0] < 0f || score[0] > 1f)
            throw new IOException("invalid_classifier_score");
        listener.onScore(score[0], Math.max(0, (System.nanoTime() - started) / 1_000_000));
    }

    int realEmbeddingCount() { return embeddingCount; }

    void reset() {
        Arrays.fill(rawRing, (short) 0);
        Arrays.fill(livekitAudio, 0f);
        Arrays.fill(melRing, 1f);
        Arrays.fill(embeddingRing, 0f);
        rawPosition = rawSize = embeddingPosition = embeddingCount = melWritePosition = 0;
        livekitPosition = livekitSize = 0;
        livekitSamplesSeen = 0;
        melFrames = MEL_WINDOW;
    }

    private void appendRaw(short[] pcm, int count) {
        for (int i = 0; i < count; i++) {
            rawRing[rawPosition] = pcm[i];
            rawPosition = (rawPosition + 1) % rawRing.length;
        }
        rawSize = Math.min(rawRing.length, rawSize + count);
    }

    @Override public void close() throws Exception {
        if (closed) return;
        closed = true;
        Arrays.fill(rawRing, (short) 0);
        Arrays.fill(livekitAudio, 0f);
        Arrays.fill(melRing, 0f);
        Arrays.fill(embeddingRing, 0f);
        Exception failure = null;
        for (Runner runner : new Runner[]{classifier, embedding, mel}) {
            try { runner.close(); }
            catch (Exception error) { if (failure == null) failure = error; }
        }
        if (failure != null) throw failure;
    }

    static float[] flatten(Object value) throws IOException {
        int size = nestedLength(value);
        float[] result = new float[size];
        int at = flattenInto(value, result, 0);
        if (at != size) throw new IOException("invalid_onnx_tensor");
        return result;
    }
    private static int nestedLength(Object value) throws IOException {
        if (value == null) throw new IOException("null_onnx_tensor");
        if (!value.getClass().isArray()) {
            if (!(value instanceof Number)) throw new IOException("invalid_onnx_tensor");
            return 1;
        }
        int n = Array.getLength(value), total = 0;
        for (int i = 0; i < n; i++) total += nestedLength(Array.get(value, i));
        return total;
    }
    private static int flattenInto(Object value, float[] out, int at) throws IOException {
        if (!value.getClass().isArray()) {
            if (!(value instanceof Number)) throw new IOException("invalid_onnx_tensor");
            out[at++] = ((Number) value).floatValue();
            return at;
        }
        for (int i = 0; i < Array.getLength(value); i++) at = flattenInto(Array.get(value, i), out, at);
        return at;
    }
}
