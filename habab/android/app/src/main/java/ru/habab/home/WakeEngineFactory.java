package ru.habab.home;

import android.content.Context;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import org.json.JSONArray;
import org.json.JSONObject;

/** Selects exactly one detector. Experimental weights are an explicit, checked build input. */
final class WakeEngineFactory {
    static final String[] IDS = {"vosk", "microwakeword", "openwakeword", "livekit"};
    private static final String BUNDLE = "voice/experiments/";

    static boolean known(String id) {
        for (String value : IDS) if (value.equals(id)) return true;
        return false;
    }

    static WakeEngine create(Context context, String id, String phrase,
            WakeDetector.Diagnostics diagnostics) throws Exception {
        if (!known(id)) throw new VoiceFailure("unsupported_wake_engine");
        if ("vosk".equals(id)) {
            JSONObject model = readJson(context, "voice/model.json");
            WakeEngineInfo info = new WakeEngineInfo(id, model.getString("name"), "vosk-0.3.75",
                    model.getString("sha256"), model.getString("sha256"),
                    "эй дом".equals(phrase) ? WakeMatchGate.MIN_WORD_CONFIDENCE_PERMILLE / 1000.0 : 0.0,
                    1, phrase, 0, 20);
            File directory = VoiceModel.prepare(context);
            return new VoskEngine(new WakeDetector(directory.toString(), phrase, diagnostics), info);
        }
        if (!"эй дом".equals(WakeDetector.normalize(phrase)))
            throw new VoiceFailure("wake_model_phrase");
        JSONObject manifest;
        try { manifest = readJson(context, BUNDLE + "manifest.json"); }
        catch (IOException missing) { throw new VoiceFailure("wake_model_unavailable"); }
        if (manifest.optInt("schema") != 1) throw new VoiceFailure("wake_model_unavailable");
        JSONArray engines = manifest.getJSONArray("engines");
        for (int i = 0; i < engines.length(); i++) {
            JSONObject entry = engines.getJSONObject(i);
            if (!id.equals(entry.optString("engine_id"))) continue;
            if (!entry.optString("frontend_sha256").matches("[a-f0-9]{64}"))
                throw new VoiceFailure("wake_model_checksum");
            WakeEngineInfo info = new WakeEngineInfo(id, entry.getString("model_id"),
                    entry.getString("version"), entry.getString("model_sha256"),
                    entry.getString("frontend_sha256"), entry.getDouble("threshold"),
                    entry.getInt("sliding_window"), entry.getString("phrase"),
                    entry.getInt("context_frames"), entry.getInt("hop_ms"));
            if (!phrase.equals(info.phrase)) throw new VoiceFailure("wake_model_phrase");
            File directory = new File(context.getFilesDir(), "wake-models/" + id + "/"
                    + info.modelSha256 + "-" + info.frontendSha256.substring(0, 12));
            if (!directory.isDirectory() && !directory.mkdirs()) throw new IOException("wake_model_directory");
            JSONArray files = entry.getJSONArray("files");
            for (int n = 0; n < files.length(); n++) copyChecked(context, directory, id, files.getJSONObject(n));
            try {
                if ("microwakeword".equals(id)) return new MicroWakeEngine(directory, info, diagnostics);
                return new OnnxWakeEngine(directory, info, diagnostics, id);
            } catch (Exception | LinkageError error) {
                VoiceFailure failure = new VoiceFailure("wake_engine_failed");
                failure.initCause(error); throw failure;
            }
        }
        throw new VoiceFailure("wake_model_unavailable");
    }

    static String bundleStatus(Context context) {
        try {
            JSONArray entries = readJson(context, BUNDLE + "manifest.json").getJSONArray("engines");
            return entries.length() == 3 ? "experimental_models_installed" : "experimental_models_incomplete";
        } catch (Exception error) { return "experimental_models_missing"; }
    }

    private static JSONObject readJson(Context context, String asset) throws Exception {
        try (InputStream input = context.getAssets().open(asset)) {
            java.io.ByteArrayOutputStream bytes = new java.io.ByteArrayOutputStream();
            byte[] chunk = new byte[4096]; int count;
            while ((count = input.read(chunk)) != -1) {
                if (bytes.size() + count > 128 * 1024) throw new IOException("wake_manifest_size");
                bytes.write(chunk, 0, count);
            }
            return new JSONObject(new String(bytes.toByteArray(), StandardCharsets.UTF_8));
        }
    }

    private static void copyChecked(Context context, File directory, String id, JSONObject item) throws Exception {
        String name = item.getString("name"), hash = item.getString("sha256");
        long bytes = item.getLong("bytes");
        if (!name.matches("(classifier\\.(onnx|tflite)|melspectrogram\\.onnx|embedding_model\\.onnx)")
                || !hash.matches("[a-f0-9]{64}") || bytes < 1 || bytes > 16 * 1024 * 1024)
            throw new VoiceFailure("wake_model_checksum");
        File target = new File(directory, name);
        if (target.isFile() && target.length() == bytes && hash.equals(sha256(target))) return;
        File temporary = new File(directory, name + ".tmp");
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        long written = 0;
        try (InputStream input = context.getAssets().open(BUNDLE + id + "/" + name);
                FileOutputStream output = new FileOutputStream(temporary)) {
            byte[] buffer = new byte[16384]; int count;
            while ((count = input.read(buffer)) != -1) {
                written += count;
                if (written > bytes) throw new VoiceFailure("wake_model_checksum");
                digest.update(buffer, 0, count); output.write(buffer, 0, count);
            }
        } catch (Exception error) { temporary.delete(); throw error; }
        if (written != bytes || !hash.equals(hex(digest.digest()))) {
            temporary.delete(); throw new VoiceFailure("wake_model_checksum");
        }
        if (!temporary.renameTo(target)) { temporary.delete(); throw new IOException("wake_model_copy"); }
    }

    private static String sha256(File file) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        try (InputStream input = new java.io.FileInputStream(file)) {
            byte[] data = new byte[16384]; int count;
            while ((count = input.read(data)) != -1) digest.update(data, 0, count);
        }
        return hex(digest.digest());
    }

    private static String hex(byte[] bytes) {
        StringBuilder result = new StringBuilder();
        for (byte value : bytes) result.append(String.format(java.util.Locale.ROOT, "%02x", value & 255));
        return result.toString();
    }

    private static final class VoskEngine implements WakeEngine {
        private final WakeDetector detector;
        private final WakeEngineInfo info;
        VoskEngine(WakeDetector detector, WakeEngineInfo info) { this.detector = detector; this.info = info; }
        public boolean accept(short[] samples, int count, long now) throws Exception { return detector.accept(samples, count, now); }
        public void reset(long now, VoiceLifecycleTrace.Reason reason) { detector.reset(now, reason); }
        public WakeMatchGate.MatchSource lastMatchSource() { return detector.lastMatchSource(); }
        public long lastMatchAttempt() { return detector.lastMatchAttempt(); }
        public void setDiagnosticCaptureEnabled(boolean enabled) { detector.setDiagnosticCaptureEnabled(enabled); }
        public WakeVerifier.Result consumeDiagnosticResult() { return detector.consumeDiagnosticResult(); }
        public short[] consumeVerifiedAudio() { return detector.consumeVerifiedAudio(); }
        public WakeEngineInfo info() { return info; }
        public void close() { detector.close(); }
    }
}
