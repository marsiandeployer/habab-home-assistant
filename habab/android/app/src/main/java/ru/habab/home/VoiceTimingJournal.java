package ru.habab.home;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import org.json.JSONException;
import org.json.JSONObject;

/** Local bounded metadata only. Call from the existing diagnostic writer, never capture. */
final class VoiceTimingJournal {
    static final long MAX_BYTES = 512 * 1024;

    static synchronized void append(File directory, String event, long attempt, long capturedAt,
            long wallclock, long elapsedAt, long fromWakeMs, long value) throws IOException {
        append(directory, event, attempt, capturedAt, wallclock, elapsedAt, fromWakeMs, value, null);
    }

    /** Appends timing with the engine identity captured for this attempt, not a later preference read. */
    static synchronized void append(File directory, String event, long attempt, long capturedAt,
            long wallclock, long elapsedAt, long fromWakeMs, long value,
            WakeEngineInfo wakeEngine) throws IOException {
        if (event == null || !event.matches("[A-Za-z][A-Za-z0-9_]{0,63}"))
            throw new IllegalArgumentException("diagnostic_event");
        if (!directory.isDirectory() && !directory.mkdirs()) throw new IOException("diagnostic_directory");
        File current = new File(directory, "voice-timing.jsonl");
        JSONObject row = new JSONObject();
        try {
            row.put("event", event).put("attempt", attempt)
                    .put("captured_at_ms", capturedAt).put("wallclock_ms", wallclock)
                    .put("elapsed_at_ms", elapsedAt).put("from_wake_ms", fromWakeMs)
                    .put("value", value);
            if (wakeEngine != null) row.put("wake_engine", timingIdentity(wakeEngine));
        } catch (JSONException error) { throw new IOException("diagnostic_json", error); }
        byte[] line = (row.toString() + "\n").getBytes(StandardCharsets.UTF_8);
        if (current.length() + line.length > MAX_BYTES) {
            File previous = new File(directory, "voice-timing.previous.jsonl");
            if (previous.exists() && !previous.delete()) throw new IOException("diagnostic_rotation");
            if (!current.renameTo(previous)) throw new IOException("diagnostic_rotation");
        }
        try (FileOutputStream output = new FileOutputStream(current, true)) { output.write(line); }
    }

    private static JSONObject timingIdentity(WakeEngineInfo info) throws JSONException {
        return new JSONObject().put("engine", info.engineId).put("model_id", info.modelId)
                .put("context_unit", info.contextUnit()).put("app_version_code", BuildConfig.VERSION_CODE)
                .put("model_version", info.modelVersion).put("model_sha256", info.modelSha256)
                .put("frontend_sha256", info.frontendSha256)
                .put("context_frames", info.contextFrames).put("hop_ms", info.hopMs)
                .put("threshold", info.threshold).put("sliding_window", info.slidingWindow);
    }
}
