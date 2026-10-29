package ru.habab.home;

import java.util.Arrays;
import org.json.JSONArray;
import org.json.JSONObject;
import org.vosk.Model;
import org.vosk.Recognizer;

/** Full-vocabulary confirmation of a finalized default-wake candidate. Audio stays in memory. */
final class WakeVerifier implements AutoCloseable {
    static final String DEFAULT_PHRASE = "эй дом";
    static final int SAMPLE_RATE = 16000;
    static final int BUFFER_SAMPLES = 16 * SAMPLE_RATE;
    static final long BUDGET_MS = 5000;
    private static final int[] CONTEXT_SAMPLES = {2400, 0, 5600, 9600};
    enum Outcome { ACCEPTED, REJECTED, INVALID_AUDIO, RATE_LIMIT, TIME_LIMIT, ERROR }
    interface Decoder extends AutoCloseable {
        String decode(short[] audio) throws Exception;
        @Override default void close() {}
    }
    interface Clock { long nanos(); }

    static final class Result {
        final Context context;
        final Outcome outcome;
        final long elapsedMs;
        final long decodedSamples;
        final short[] acceptedAudio;
        Result(Context context, Outcome outcome, long elapsedMs, long decodedSamples, short[] acceptedAudio) {
            this.context = context;
            this.outcome = outcome; this.elapsedMs = elapsedMs; this.decodedSamples = decodedSamples;
            this.acceptedAudio = acceptedAudio;
        }
        boolean accepted() { return outcome == Outcome.ACCEPTED; }
    }

    static final class Context {
        final long attempt, attemptStartedAt;
        final WakeMatchGate.MatchSource source;
        final int rms, vadSpeech, confidence;
        Context(long attempt, long attemptStartedAt, WakeMatchGate.MatchSource source,
                int rms, int vadSpeech, int confidence) {
            this.attempt = attempt; this.attemptStartedAt = attemptStartedAt; this.source = source;
            this.rms = rms; this.vadSpeech = vadSpeech; this.confidence = confidence;
        }
    }

    private static final class Snapshot {
        final Context context;
        final short[] samples;
        final int wordStart, wordEnd;
        final long generation;
        Snapshot(Context context, short[] samples, int wordStart, int wordEnd, long generation) {
            this.context = context; this.samples = samples; this.wordStart = wordStart;
            this.wordEnd = wordEnd; this.generation = generation;
        }
    }

    private static final class VoskDecoder implements Decoder {
        private final Recognizer recognizer;
        private boolean failed;
        VoskDecoder(Model model) throws Exception {
            recognizer = new Recognizer(model, SAMPLE_RATE);
            recognizer.setMaxAlternatives(8);
        }
        @Override public String decode(short[] samples) throws Exception {
            if (failed) throw new IllegalStateException("verifier_failed");
            try {
                recognizer.acceptWaveForm(samples, samples.length);
                // FinalResult deletes the feature pipeline and decoder. The next crop
                // gets a fresh pipeline while retaining the expensive vocabulary graph.
                return recognizer.getFinalResult();
            } catch (Exception | LinkageError error) { failed = true; throw error; }
        }
        @Override public void close() { recognizer.close(); }
    }

    /** Indexes exactly the samples fed to this Recognizer, including replayed pre-roll.
     * Vosk word times remain cumulative across reset(), endpoints and getFinalResult().
     * Clearing retained audio must therefore preserve the lifetime sample counter.
     */
    static final class AudioWindow {
        private final short[] data;
        private long end;
        private int size;
        AudioWindow(int capacity) {
            if (capacity <= 0) throw new IllegalArgumentException("audio_capacity");
            data = new short[capacity];
        }
        void append(short[] input, int count) {
            if (count < 0 || count > input.length) throw new IllegalArgumentException("audio_count");
            for (int i = 0; i < count; i++) data[(int) ((end + i) % data.length)] = input[i];
            end += count;
            size = (int) Math.min(data.length, (long) size + count);
        }
        long end() { return end; }
        long start() { return end - size; }
        short[] crop(long first, long last) {
            if (first < start() || last > end || last <= first) return null;
            short[] crop = new short[(int) (last - first)];
            for (int i = 0; i < crop.length; i++) crop[i] = data[(int) ((first + i) % data.length)];
            return crop;
        }
        void clear() { Arrays.fill(data, (short) 0); size = 0; }
    }

    private final AudioWindow audio = new AudioWindow(BUFFER_SAMPLES);
    private final Decoder decoder;
    private final Clock clock;
    private long lastAttemptAtSample = -SAMPLE_RATE;
    private long lastCandidateEnd = -1;
    private final Object lock = new Object();
    private Snapshot pending;
    private Result completed;
    private long generation;
    private boolean closed, running;

    WakeVerifier(Model model) throws Exception {
        // Create before returning, so this Recognizer already owns a native Model
        // reference when WakeDetector eventually releases its own reference.
        this(new VoskDecoder(model), System::nanoTime);
    }

    WakeVerifier(Decoder decoder, Clock clock) {
        this.decoder = decoder; this.clock = clock;
        Thread worker = new Thread(this::run, "HababWakeVerifier");
        worker.setDaemon(true);
        try { worker.start(); }
        catch (RuntimeException | Error error) { decoder.close(); throw error; }
    }

    void append(short[] samples, int count) { audio.append(samples, count); }
    void clearAudio() {
        audio.clear();
        synchronized (lock) { generation++; pending = null; completed = null; }
    }

    /** Capture-thread only: copy one bounded region. Never waits for native decoding.
     * A null return means queued; an immediate result is a fail-closed rejection.
     */
    Result submit(JSONObject candidate, Context context) {
        long started = clock.nanos();
        try {
            JSONArray words = candidate.optJSONArray("result");
            if (words == null || words.length() != 2) return result(context, Outcome.INVALID_AUDIO, started, 0);
            double first = Double.NaN, last = -1;
            for (int i = 0; i < words.length(); i++) {
                JSONObject word = words.optJSONObject(i);
                if (word == null) return result(context, Outcome.INVALID_AUDIO, started, 0);
                double start = word.optDouble("start", Double.NaN);
                double end = word.optDouble("end", Double.NaN);
                if (!Double.isFinite(start) || !Double.isFinite(end) || start < 0 || end <= start
                        || start < last || end > (double) audio.end() / SAMPLE_RATE)
                    return result(context, Outcome.INVALID_AUDIO, started, 0);
                if (i == 0) first = start;
                last = end;
            }
            long start = (long) (first * SAMPLE_RATE), end = (long) (last * SAMPLE_RATE);
            // Never verify a truncated phrase using an unrelated, newer part of the ring.
            if (start < audio.start() || end > audio.end() || end <= start)
                return result(context, Outcome.INVALID_AUDIO, started, 0);
            if (end <= lastCandidateEnd || audio.end() - lastAttemptAtSample < SAMPLE_RATE)
                return result(context, Outcome.RATE_LIMIT, started, 0);
            lastAttemptAtSample = audio.end();
            lastCandidateEnd = end;
            long cropStart = Math.max(audio.start(), start - 9600);
            long cropEnd = Math.min(audio.end(), end + 9600);
            short[] snapshot = audio.crop(cropStart, cropEnd);
            if (snapshot == null) return result(context, Outcome.INVALID_AUDIO, started, 0);
            synchronized (lock) {
                if (closed) return result(context, Outcome.ERROR, started, 0);
                // One running crop and one latest candidate; no unbounded work queue.
                pending = new Snapshot(context, snapshot, (int) (start - cropStart),
                        (int) (end - cropStart), generation);
                lock.notifyAll();
            }
            return null;
        } catch (Exception | LinkageError error) {
            return result(context, Outcome.ERROR, started, 0);
        }
    }

    Result poll() {
        synchronized (lock) { Result result = completed; completed = null; return result; }
    }

    boolean hasPending() {
        synchronized (lock) { return running || pending != null || completed != null; }
    }

    private void run() {
        try {
            while (true) {
                Snapshot task;
                synchronized (lock) {
                    while (!closed && pending == null) lock.wait();
                    if (closed) return;
                    task = pending; pending = null; running = true;
                }
                Result result = verify(task);
                synchronized (lock) {
                    running = false;
                    if (!closed && task.generation == generation
                            && (completed == null || !completed.accepted())) completed = result;
                }
            }
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        } finally {
            synchronized (lock) { running = false; }
            // close() only signals; a native call always finishes before its owner is freed.
            decoder.close();
        }
    }

    private Result verify(Snapshot task) {
        long started = clock.nanos(), decoded = 0;
        try {
            long previousStart = -1, previousEnd = -1;
            for (int context : CONTEXT_SAMPLES) {
                if (cancelled(task)) return result(task.context, Outcome.REJECTED, started, decoded);
                int cropStart = Math.max(0, task.wordStart - context);
                int cropEnd = Math.min(task.samples.length, task.wordEnd + context);
                if (cropStart == previousStart && cropEnd == previousEnd) continue;
                previousStart = cropStart; previousEnd = cropEnd;
                short[] crop = Arrays.copyOfRange(task.samples, cropStart, cropEnd);
                if (elapsed(started) >= BUDGET_MS) return result(task.context, Outcome.TIME_LIMIT, started, decoded);
                decoded += crop.length;
                JSONObject response = new JSONObject(decoder.decode(crop));
                // A native decode cannot be interrupted safely; stop further work and fail closed
                // if this bounded decode exhausted the budget, even when it returned a match.
                if (elapsed(started) >= BUDGET_MS) return result(task.context, Outcome.TIME_LIMIT, started, decoded);
                JSONArray alternatives = response.optJSONArray("alternatives");
                if (alternatives == null || alternatives.length() == 0 || alternatives.length() > 8)
                    return result(task.context, Outcome.ERROR, started, decoded);
                for (int i = 0; i < alternatives.length(); i++) {
                    JSONObject alternative = alternatives.optJSONObject(i);
                    if (alternative == null || !alternative.has("text")) return result(task.context, Outcome.ERROR, started, decoded);
                    if (DEFAULT_PHRASE.equals(WakeMatchGate.normalize(alternative.optString("text"))))
                        return new Result(task.context, Outcome.ACCEPTED, elapsed(started), decoded, task.samples);
                }
            }
            return result(task.context, Outcome.REJECTED, started, decoded);
        } catch (Exception | LinkageError error) {
            return result(task.context, Outcome.ERROR, started, decoded);
        }
    }

    private boolean cancelled(Snapshot task) {
        synchronized (lock) { return closed || task.generation != generation; }
    }

    @Override public void close() {
        audio.clear();
        synchronized (lock) {
            closed = true; generation++; pending = null; completed = null; lock.notifyAll();
        }
    }

    private long elapsed(long started) { return Math.max(0, (clock.nanos() - started) / 1000000); }
    private Result result(Context context, Outcome outcome, long started, long decoded) {
        return new Result(context, outcome, elapsed(started), decoded, null);
    }
}
