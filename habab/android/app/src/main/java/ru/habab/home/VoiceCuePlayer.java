package ru.habab.home;

import android.media.*;
import java.util.concurrent.*;
import java.util.function.BooleanSupplier;

/** Completion means all cue frames were played, not merely queued to Android. */
final class VoiceCuePlayer implements AutoCloseable {
    interface Sink extends AutoCloseable { void play(); long played(); void close(); }
    interface Factory { Sink create(short[] samples); }
    private final Factory factory;
    private final BooleanSupplier audible;
    private final ExecutorService worker = Executors.newSingleThreadExecutor();

    VoiceCuePlayer(AudioManager manager) {
        this(samples -> new Sink() {
            final AudioTrack track = new AudioTrack.Builder()
                    .setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build())
                    .setAudioFormat(new AudioFormat.Builder().setSampleRate(VoiceCuePcm.RATE)
                            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO).setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                    .setBufferSizeInBytes(samples.length * 2).setTransferMode(AudioTrack.MODE_STATIC).build();
            public void play() {
                // Static tracks become INITIALIZED only after their PCM has been written.
                if (track.getState() == AudioTrack.STATE_UNINITIALIZED
                        || track.write(samples, 0, samples.length, AudioTrack.WRITE_BLOCKING) != samples.length
                        || track.getState() != AudioTrack.STATE_INITIALIZED)
                    throw new IllegalStateException("cue_unavailable");
                track.play();
            }
            public long played() { return Integer.toUnsignedLong(track.getPlaybackHeadPosition()); }
            public void close() { try { track.stop(); } catch (IllegalStateException ignored) {} finally { track.release(); } }
        }, () -> !manager.isStreamMute(AudioManager.STREAM_MUSIC) && manager.getStreamVolume(AudioManager.STREAM_MUSIC) > 0);
    }

    VoiceCuePlayer(Factory factory, BooleanSupplier audible) { this.factory = factory; this.audible = audible; }

    CompletableFuture<Boolean> play(VoiceSessionSignals.Cue cue) {
        CompletableFuture<Boolean> completion = new CompletableFuture<>();
        if (cue == VoiceSessionSignals.Cue.NONE) { completion.complete(false); return completion; }
        try {
            worker.execute(() -> {
                boolean complete = false;
                try {
                    if (audible.getAsBoolean()) {
                        short[] pcm = VoiceCuePcm.create(cue);
                        try (Sink sink = factory.create(pcm)) {
                            sink.play(); long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(2);
                            while (System.nanoTime() < deadline && sink.played() < pcm.length) Thread.sleep(5);
                            complete = sink.played() >= pcm.length;
                        }
                    }
                } catch (Exception error) { if (error instanceof InterruptedException) Thread.currentThread().interrupt(); }
                completion.complete(complete);
            });
        } catch (RejectedExecutionException closed) { completion.complete(false); }
        return completion;
    }

    // Drain the final END cue even when the foreground microphone service is stopping.
    @Override public void close() { worker.shutdown(); }
}
