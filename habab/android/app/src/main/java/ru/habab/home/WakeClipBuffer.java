package ru.habab.home;

import java.io.File;
import java.io.FileOutputStream;
import java.io.BufferedOutputStream;
import java.io.OutputStream;
import java.io.IOException;
import java.util.Arrays;
import java.util.Comparator;
import org.json.JSONObject;

/** In-memory rolling audio; persisted only for accepted wakes or opt-in verifier diagnostics. */
final class WakeClipBuffer {
    static final int SAMPLE_RATE = 16000;
    static final int MAX_DIAGNOSTIC_SAMPLES = 16 * SAMPLE_RATE;
    static final int MAX_RETAINED_CLIPS = 50;
    static final long MAX_RETAINED_BYTES = 32L * 1024 * 1024;
    private final short[] samples;
    private int next, length;

    WakeClipBuffer(int seconds) { samples = new short[seconds * SAMPLE_RATE]; }

    void append(short[] input, int count) {
        for (int i = 0; i < count; i++) {
            samples[next] = input[i]; next = (next + 1) % samples.length;
            if (length < samples.length) length++;
        }
    }

    short[] snapshot() {
        short[] result = new short[length];
        int start = (next - length + samples.length) % samples.length;
        for (int i = 0; i < length; i++) result[i] = samples[(start + i) % samples.length];
        return result;
    }

    void clear() { Arrays.fill(samples, (short) 0); next = 0; length = 0; }

    static synchronized void save(File directory, short[] data, long timestamp, long attempt) throws IOException {
        if (data == null || data.length > MAX_DIAGNOSTIC_SAMPLES) throw new IOException("wake_clip_size");
        String name = "wake-" + timestamp + "-" + attempt + ".wav";
        writeWav(directory, name, data);
        retain(directory);
    }

    /** Saves an accepted clip with the engine identity captured for that attempt. */
    static synchronized void save(File directory, short[] data, long timestamp, long attempt,
            WakeEngineInfo wakeEngine) throws IOException {
        if (wakeEngine == null) throw new IOException("wake_engine_identity");
        if (data == null || data.length > MAX_DIAGNOSTIC_SAMPLES) throw new IOException("wake_clip_size");
        String name = "wake-" + timestamp + "-" + attempt + ".wav";
        writeWav(directory, name, data);
        JSONObject metadata = new JSONObject();
        try {
            metadata.put("recording_result", true)
                    .put("ground_truth", false)
                    .put("outcome", "ACCEPTED")
                    .put("attempt", attempt)
                    .put("timestamp_ms", timestamp)
                    .put("wallclock_ms", timestamp)
                    .put("wake_engine", wakeEngine.toJson());
        } catch (org.json.JSONException error) {
            throw new IOException("wake_clip_metadata", error);
        }
        writeJson(directory, name + ".json", metadata);
        retain(directory);
    }

    /** Saves the exact bounded verifier snapshot and numeric/enum outcome metadata. */
    static synchronized String saveDiagnostic(File directory, short[] data, long timestamp,
            long attempt, WakeVerifier.Result result) throws IOException {
        return saveDiagnostic(directory, data, timestamp, attempt, result, null);
    }

    /** Saves verifier output with the immutable engine identity captured for that attempt. */
    static synchronized String saveDiagnostic(File directory, short[] data, long timestamp,
            long attempt, WakeVerifier.Result result, WakeEngineInfo wakeEngine) throws IOException {
        if (result == null || result.diagnosticAudio == null || data == null
                || data.length > MAX_DIAGNOSTIC_SAMPLES) throw new IOException("wake_clip_size");
        String prefix = result.accepted() ? "wake-" : "wake-rejected-";
        String name = prefix + timestamp + "-" + attempt + ".wav";
        writeWav(directory, name, data);
        JSONObject metadata = new JSONObject();
        try {
            metadata.put("recording_result", true)
                    .put("ground_truth", false)
                    .put("outcome", result.outcome.name())
                    .put("attempt", result.context.attempt)
                    .put("attempt_started_elapsed_ms", result.context.attemptStartedAt)
                    .put("verify_elapsed_ms", result.elapsedMs)
                    .put("decoded_frames", result.decodedSamples / 320)
                    .put("rms_pcm16", result.context.rms)
                    .put("confidence_permille", result.context.confidence)
                    .put("wallclock_ms", result.wallClockMs);
            if (wakeEngine != null) metadata.put("wake_engine", wakeEngine.toJson());
        } catch (org.json.JSONException error) {
            throw new IOException("wake_clip_metadata", error);
        }
        writeJson(directory, name + ".json", metadata);
        retain(directory);
        return name;
    }

    private static void writeWav(File directory, String name, short[] data) throws IOException {
        if (!directory.isDirectory() && !directory.mkdirs()) throw new IOException("wake_clip_directory");
        File destination = new File(directory, name);
        File temporary = new File(directory, destination.getName() + ".tmp");
        try (OutputStream out = new BufferedOutputStream(new FileOutputStream(temporary))) {
            int bytes = data.length * 2;
            out.write(new byte[]{'R','I','F','F'}); le(out, 36 + bytes, 4);
            out.write(new byte[]{'W','A','V','E','f','m','t',' '}); le(out, 16, 4);
            le(out, 1, 2); le(out, 1, 2); le(out, SAMPLE_RATE, 4); le(out, SAMPLE_RATE * 2, 4);
            le(out, 2, 2); le(out, 16, 2); out.write(new byte[]{'d','a','t','a'}); le(out, bytes, 4);
            for (short sample : data) le(out, sample, 2);
        } catch (IOException error) { temporary.delete(); throw error; }
        if (!temporary.renameTo(destination)) { temporary.delete(); throw new IOException("wake_clip_rename"); }
    }

    private static void writeJson(File directory, String name, JSONObject metadata) throws IOException {
        File destination = new File(directory, name);
        File temporary = new File(directory, name + ".tmp");
        try (OutputStream out = new BufferedOutputStream(new FileOutputStream(temporary))) {
            out.write(metadata.toString().getBytes(java.nio.charset.StandardCharsets.UTF_8));
            out.flush();
        } catch (IOException error) { temporary.delete(); throw error; }
        if (!temporary.renameTo(destination)) { temporary.delete(); throw new IOException("wake_clip_metadata_rename"); }
    }

    private static void retain(File directory) {
        File[] clips = directory.listFiles((dir, name) -> name.startsWith("wake-") && name.endsWith(".wav"));
        if (clips == null) return;
        Arrays.sort(clips, Comparator.comparingLong(File::lastModified).thenComparing(File::getName));
        long bytes = 0;
        for (File clip : clips) bytes += clip.length();
        int count = clips.length;
        for (File clip : clips) {
            if (count <= MAX_RETAINED_CLIPS && bytes <= MAX_RETAINED_BYTES) break;
            long size = clip.length();
            if (clip.delete()) {
                count--; bytes -= size;
                new File(directory, clip.getName() + ".review").delete();
                new File(directory, clip.getName() + ".json").delete();
            }
        }
    }

    static synchronized void markForReview(File directory, long timestamp, long attempt) throws IOException {
        File clip = new File(directory, "wake-" + timestamp + "-" + attempt + ".wav");
        if (!clip.isFile()) throw new IOException("wake_clip_missing");
        File marker = new File(directory, clip.getName() + ".review");
        if (!marker.exists() && !marker.createNewFile()) throw new IOException("wake_clip_marker");
    }

    private static void le(OutputStream out, int value, int size) throws IOException {
        for (int i = 0; i < size; i++) out.write((value >> (8 * i)) & 0xff);
    }
}
