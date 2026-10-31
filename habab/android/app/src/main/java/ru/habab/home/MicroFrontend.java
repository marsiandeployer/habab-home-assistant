package ru.habab.home;

import java.util.Arrays;

/** Streaming 16 kHz mono microWakeWord feature frontend backed by TensorFlow MicroFrontend. */
final class MicroFrontend implements AutoCloseable {
    static final int SAMPLE_RATE = 16000;
    static final int SAMPLES_PER_FEATURE = 160;
    static final int FEATURES_PER_FRAME = 40;

    static {
        System.loadLibrary("microfrontend_jni");
    }

    private long handle;

    MicroFrontend() {
        handle = nativeCreate();
        if (handle == 0) throw new IllegalStateException("microfrontend_create_failed");
    }

    /** Returns flattened, consecutive [frame, 40] feature rows. */
    synchronized float[] process(short[] pcm, int count) {
        ensureOpen();
        if (pcm == null || count <= 0 || count > pcm.length || count > SAMPLE_RATE)
            throw new IllegalArgumentException("microfrontend_audio_size");
        return nativeProcess(handle, pcm, count);
    }

    synchronized void reset() {
        ensureOpen();
        nativeReset(handle);
    }

    private void ensureOpen() {
        if (handle == 0) throw new IllegalStateException("microfrontend_closed");
    }

    @Override public synchronized void close() {
        if (handle == 0) return;
        nativeClose(handle);
        handle = 0;
    }

    static int frameCount(float[] features) {
        if (features == null || features.length % FEATURES_PER_FRAME != 0)
            throw new IllegalArgumentException("invalid_microfrontend_features");
        return features.length / FEATURES_PER_FRAME;
    }

    static void zero(float[] features) {
        if (features != null) Arrays.fill(features, 0f);
    }

    private static native long nativeCreate();
    private static native float[] nativeProcess(long handle, short[] pcm, int count);
    private static native void nativeReset(long handle);
    private static native void nativeClose(long handle);
}
