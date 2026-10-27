package ru.habab.home;

import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioTrack;
import android.media.AudioDeviceInfo;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
import org.json.JSONException;
import org.json.JSONObject;

/** Keeps a bounded silent call track alive for legacy SCO capture. */
final class SilentVoiceCallPlayback implements AutoCloseable {
    static final int SAMPLE_RATE = 8000;
    static final int SILENCE_SAMPLES = 160;
    private static final int MIN_BUFFER_BYTES = SILENCE_SAMPLES * 2;
    private static final int MAX_BUFFER_BYTES = 4096;
    private static final long FIRST_RETRY_MS = 100;
    private static final long MAX_RETRY_MS = 2000;

    interface Track {
        void play();
        int write(short[] silence);
        int playbackState();
        int routedDeviceType();
        void close();
    }

    interface TrackFactory { Track create(); }

    private final AtomicBoolean running = new AtomicBoolean(true);
    private final AtomicReference<Track> activeTrack = new AtomicReference<>();
    private final TrackFactory trackFactory;
    private final Thread writer;
    private volatile int restartCount;
    private volatile int lastWriteResult = Integer.MIN_VALUE;
    private volatile int lastPlaybackState = AudioTrack.PLAYSTATE_STOPPED;
    private volatile int lastRoutedDeviceType = -1;
    private volatile String lastFailure = "none";

    SilentVoiceCallPlayback() { this(SilentVoiceCallPlayback::createPlatformTrack); }

    SilentVoiceCallPlayback(TrackFactory trackFactory) {
        this.trackFactory = trackFactory;
        Track initial = trackFactory.create();
        try {
            initial.play();
            activeTrack.set(initial);
            lastPlaybackState = initial.playbackState();
            lastRoutedDeviceType = initial.routedDeviceType();
        } catch (RuntimeException | LinkageError error) {
            closeQuietly(initial);
            throw error;
        }
        writer = new Thread(this::writeSilence, "habab-silent-sco-call");
        writer.setDaemon(true);
        writer.start();
    }

    private static Track createPlatformTrack() {
        int minimum = AudioTrack.getMinBufferSize(SAMPLE_RATE, AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT);
        if (minimum <= 0 || minimum > MAX_BUFFER_BYTES)
            throw new IllegalStateException("silent_call_track_buffer_unavailable");
        int bufferBytes = Math.max(MIN_BUFFER_BYTES, minimum);
        AudioTrack track = new AudioTrack(AudioManager.STREAM_VOICE_CALL, SAMPLE_RATE,
                AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT,
                bufferBytes, AudioTrack.MODE_STREAM);
        if (track.getState() != AudioTrack.STATE_INITIALIZED) {
            track.release();
            throw new IllegalStateException("silent_call_track_unavailable");
        }
        return new PlatformTrack(track);
    }

    private void writeSilence() {
        short[] silence = new short[SILENCE_SAMPLES];
        long retryMs = FIRST_RETRY_MS;
        while (running.get()) {
            Track track = activeTrack.get();
            if (track == null) {
                try {
                    track = trackFactory.create();
                    if (!running.get()) { closeQuietly(track); break; }
                    track.play();
                    if (!running.get()) { closeQuietly(track); break; }
                    activeTrack.set(track);
                    if (!running.get()) {
                        if (activeTrack.compareAndSet(track, null)) closeQuietly(track);
                        break;
                    }
                    lastPlaybackState = track.playbackState();
                    lastRoutedDeviceType = track.routedDeviceType();
                } catch (RuntimeException | LinkageError error) {
                    lastFailure = error.getClass().getSimpleName();
                    restartCount++;
                    retryMs = waitBeforeRetry(retryMs);
                    continue;
                }
            }
            try {
                int result = track.write(silence);
                lastWriteResult = result;
                lastPlaybackState = track.playbackState();
                lastRoutedDeviceType = track.routedDeviceType();
                if (result > 0 && lastPlaybackState == AudioTrack.PLAYSTATE_PLAYING) {
                    lastFailure = "none";
                    retryMs = FIRST_RETRY_MS;
                    continue;
                }
                lastFailure = result <= 0 ? "write_result_" + result
                        : "playback_state_" + lastPlaybackState;
            } catch (RuntimeException | LinkageError error) {
                lastFailure = error.getClass().getSimpleName();
            }
            if (activeTrack.compareAndSet(track, null)) closeQuietly(track);
            lastPlaybackState = AudioTrack.PLAYSTATE_STOPPED;
            if (running.get()) {
                restartCount++;
                retryMs = waitBeforeRetry(retryMs);
            }
        }
    }

    private long waitBeforeRetry(long retryMs) {
        try { Thread.sleep(retryMs); }
        catch (InterruptedException interrupted) { if (!running.get()) return retryMs; }
        return Math.min(MAX_RETRY_MS, retryMs * 2);
    }

    private static void closeQuietly(Track track) {
        try { track.close(); } catch (RuntimeException | LinkageError ignored) { }
    }

    void addDiagnostics(JSONObject audio) throws JSONException {
        audio.put("silentCallTrackRestarts", restartCount)
                .put("silentCallTrackLastWriteResult", lastWriteResult)
                .put("silentCallTrackLastFailure", lastFailure)
                .put("silentCallTrackState", lastPlaybackState)
                .put("silentCallTrackRoutedDeviceType", lastRoutedDeviceType);
    }

    @Override public void close() {
        if (!running.getAndSet(false)) return;
        Track current = activeTrack.getAndSet(null);
        if (current != null) closeQuietly(current);
        writer.interrupt();
        if (writer != Thread.currentThread()) {
            try { writer.join(500); }
            catch (InterruptedException interrupted) { Thread.currentThread().interrupt(); }
        }
    }

    private static final class PlatformTrack implements Track {
        private final AudioTrack track;
        private final AtomicBoolean closed = new AtomicBoolean();

        PlatformTrack(AudioTrack track) { this.track = track; }
        @Override public void play() { track.play(); }
        @Override public int write(short[] silence) {
            return track.write(silence, 0, silence.length, AudioTrack.WRITE_BLOCKING);
        }
        @Override public int playbackState() { return track.getPlayState(); }
        @Override public int routedDeviceType() {
            AudioDeviceInfo device = track.getRoutedDevice();
            return device == null ? -1 : device.getType();
        }
        @Override public void close() {
            if (!closed.compareAndSet(false, true)) return;
            try { track.pause(); } catch (RuntimeException ignored) { }
            try { track.flush(); } catch (RuntimeException ignored) { }
            try { track.stop(); } catch (RuntimeException ignored) { }
            track.release();
        }
    }
}
