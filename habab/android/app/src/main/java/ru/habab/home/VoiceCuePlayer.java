package ru.habab.home;

import android.media.*;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.*;
import java.util.function.BooleanSupplier;

/** Completion means all cue frames were played, not merely queued to Android. */
final class VoiceCuePlayer implements AutoCloseable {
    interface Sink extends AutoCloseable { void play(); long played(); default long frameCount() { return -1; } void close(); }
    interface Factory { Sink create(short[] samples); }
    private final Factory factory;
    private final BooleanSupplier audible;
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private volatile Map<String, Object> diagnostics = Collections.singletonMap("stage", "idle");
    private volatile Map<String, Object> startDiagnostics = diagnostics;
    private boolean startCue;

    VoiceCuePlayer(AudioManager manager) {
        this(manager, () -> true);
    }

    VoiceCuePlayer(AudioManager manager, BooleanSupplier permitted) {
        this(manager, permitted, false);
    }

    VoiceCuePlayer(AudioManager manager, BooleanSupplier permitted, boolean legacyScoSampleRate) {
        this(manager, permitted, legacyScoSampleRate, () -> false);
    }

    VoiceCuePlayer(AudioManager manager, BooleanSupplier permitted, boolean legacyScoSampleRate, BooleanSupplier duplicateOutput) {
        factory = samples -> new Sink() {
            final short[] routedSamples = legacyScoSampleRate
                    ? new PcmRateConverter(VoiceCuePcm.RATE, 16000).convert(samples, samples.length) : samples;
            final int sampleRate = legacyScoSampleRate ? 16000 : VoiceCuePcm.RATE;
            final boolean duplicate = duplicateOutput.getAsBoolean();
            final AudioTrack track = createTrack(manager, duplicate, false, routedSamples.length * 2, sampleRate);
            final AudioTrack secondaryTrack = duplicate
                    ? createTrack(manager, true, true, routedSamples.length * 2, sampleRate) : null;
            public void play() {
                // Static tracks become INITIALIZED only after their PCM has been written.
                writeTrack(track, routedSamples, "primary");
                if (secondaryTrack != null) writeTrack(secondaryTrack, routedSamples, "secondary");
                diagnostic("play", "track", "primary");
                track.play();
                diagnostic("play", "playState", track.getPlayState());
                if (secondaryTrack != null) {
                    diagnostic("play", "track", "secondary");
                    secondaryTrack.play();
                    diagnostic("play", "playState", secondaryTrack.getPlayState());
                }
            }
            public long played() {
                long primaryPlayed = Integer.toUnsignedLong(track.getPlaybackHeadPosition());
                return secondaryTrack == null ? primaryPlayed
                        : Math.min(primaryPlayed, Integer.toUnsignedLong(secondaryTrack.getPlaybackHeadPosition()));
            }
            public long frameCount() { return routedSamples.length; }
            public void close() {
                try { track.stop(); } catch (IllegalStateException ignored) {} finally { track.release(); }
                if (secondaryTrack != null) {
                    try { secondaryTrack.stop(); } catch (IllegalStateException ignored) {} finally { secondaryTrack.release(); }
                }
            }
        };
        audible = () -> {
            boolean focus = permitted.getAsBoolean();
            boolean muted = manager.isStreamMute(AudioManager.STREAM_MUSIC);
            int volume = manager.getStreamVolume(AudioManager.STREAM_MUSIC);
            diagnostic("audibility", "focus", focus, "musicMuted", muted, "musicVolume", volume);
            return focus && !muted && volume > 0;
        };
    }

    private AudioTrack createTrack(AudioManager manager, boolean duplicate, boolean phoneOutput, int bytes, int sampleRate) {
        diagnostic("create", "track", phoneOutput ? "secondary" : "primary", "sampleRate", sampleRate);
        android.media.AudioDeviceInfo target = null;
        if (duplicate) {
            android.media.AudioDeviceInfo bluetooth = VoiceAudioRoute.bluetoothCommunicationOutput(manager);
            android.media.AudioDeviceInfo speaker = VoiceAudioRoute.phoneSpeaker(manager);
            if (bluetooth == null || speaker == null) throw new IllegalStateException("voice_duplicate_route_unavailable");
            target = phoneOutput ? speaker : bluetooth;
        }
        AudioTrack track = new AudioTrack.Builder()
                .setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build())
                .setAudioFormat(new AudioFormat.Builder().setSampleRate(sampleRate).setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                .setBufferSizeInBytes(bytes).setTransferMode(AudioTrack.MODE_STATIC).build();
        try {
            int state = track.getState();
            diagnostic("create", "initialState", state);
            requireTrackState(state, false);
            if (target != null) {
                diagnostic("route");
                if (!track.setPreferredDevice(target)) throw new IllegalStateException("voice_duplicate_route_unavailable");
            }
            return track;
        } catch (RuntimeException error) {
            track.release(); throw error;
        }
    }

    static void requireTrackState(int state, boolean pcmWritten) {
        if (state != AudioTrack.STATE_INITIALIZED && (pcmWritten || state != AudioTrack.STATE_NO_STATIC_DATA))
            throw new IllegalStateException("cue_unavailable");
    }

    private void writeTrack(AudioTrack track, short[] samples, String output) {
        int state = track.getState();
        diagnostic("write", "track", output, "stateBeforeWrite", state, "expectedSamples", samples.length);
        requireTrackState(state, false);
        int written = track.write(samples, 0, samples.length, AudioTrack.WRITE_BLOCKING);
        state = track.getState();
        diagnostic("write", "writtenSamples", written, "stateAfterWrite", state);
        if (written != samples.length) throw new IllegalStateException("cue_unavailable");
        requireTrackState(state, true);
    }

    private void diagnostic(String stage, Object... fields) {
        Map<String, Object> next = new LinkedHashMap<>(diagnostics);
        next.put("stage", stage);
        for (int i = 0; i < fields.length; i += 2) next.put((String) fields[i], fields[i + 1]);
        diagnostics = Collections.unmodifiableMap(next);
        if (startCue) startDiagnostics = diagnostics;
    }

    Map<String, Object> diagnostics() { return diagnostics; }
    Map<String, Object> startDiagnostics() { return startDiagnostics; }

    VoiceCuePlayer(Factory factory, BooleanSupplier audible) { this.factory = factory; this.audible = audible; }

    CompletableFuture<Boolean> play(VoiceSessionSignals.Cue cue) {
        CompletableFuture<Boolean> completion = new CompletableFuture<>();
        if (cue == VoiceSessionSignals.Cue.NONE) { completion.complete(false); return completion; }
        try {
            worker.execute(() -> {
                startCue = cue == VoiceSessionSignals.Cue.START;
                diagnostics = Collections.singletonMap("stage", "audibility");
                if (startCue) startDiagnostics = diagnostics;
                boolean complete = false;
                try {
                    if (audible.getAsBoolean()) {
                        short[] pcm = VoiceCuePcm.create(cue);
                        try (Sink sink = factory.create(pcm)) {
                            long expectedFrames = sink.frameCount() >= 0 ? sink.frameCount() : pcm.length;
                            sink.play(); long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(2);
                            diagnostic("drain", "expectedFrames", expectedFrames);
                            long played = sink.played();
                            while (System.nanoTime() < deadline && played < expectedFrames) {
                                Thread.sleep(5); played = sink.played();
                            }
                            complete = played >= expectedFrames;
                            diagnostic(complete ? "complete" : "drain", "playedFrames", played,
                                    "failure", complete ? "none" : "drain_timeout");
                        }
                    } else diagnostic("audibility", "failure", "not_audible");
                } catch (Exception error) {
                    diagnostic((String) diagnostics.get("stage"), "failure", error.getClass().getSimpleName());
                    if (error instanceof InterruptedException) Thread.currentThread().interrupt();
                }
                completion.complete(complete);
            });
        } catch (RejectedExecutionException closed) { completion.complete(false); }
        return completion;
    }

    // Drain the final END cue even when the foreground microphone service is stopping.
    @Override public void close() { worker.shutdown(); }
}
