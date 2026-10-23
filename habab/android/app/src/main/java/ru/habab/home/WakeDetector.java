package ru.habab.home;

import com.sun.jna.Library;
import com.sun.jna.Native;
import com.sun.jna.Pointer;
import java.io.IOException;
import java.util.ArrayDeque;
import java.util.Locale;
import org.json.JSONArray;
import org.json.JSONObject;
import org.vosk.*;

final class WakeDetector implements AutoCloseable {
    public interface Dictionary extends Library { int vosk_model_find_word(Pointer model, String word); }
    private final Model model;
    private final Recognizer recognizer;
    private final String phrase;
    private final VoiceActivityGate gate = new VoiceActivityGate();
    private final ArrayDeque<short[]> preRoll = new ArrayDeque<>();
    private boolean wasSpeech;
    private long matchedAt;
    private long utteranceStarted;

    static String normalize(String value) {
        return java.text.Normalizer.normalize(value, java.text.Normalizer.Form.NFKC)
                .toLowerCase(new Locale("ru")).replaceAll("[^\\p{L}\\s]", " ").trim().replaceAll("\\s+", " ");
    }

    WakeDetector(String directory, String requested) throws Exception {
        phrase = normalize(requested);
        if (phrase.isEmpty() || phrase.length() > 64 || phrase.split(" ").length > 5) throw new IOException("invalid_wake_phrase");
        LibVosk.vosk_set_log_level(-1);
        model = new Model(directory);
        try {
            Dictionary dictionary = Native.load("vosk", Dictionary.class);
            for (String word : phrase.split(" "))
                if (dictionary.vosk_model_find_word(model.getPointer(), word) < 0) throw new IOException("unsupported_wake_word");
            recognizer = new Recognizer(model, 16000, new JSONArray().put(phrase).put("[unk]").toString());
            recognizer.setEndpointerMode(Recognizer.EndpointerMode.SHORT);
        } catch (Exception error) { model.close(); throw error; }
    }

    boolean accept(short[] samples, int count, long now) throws Exception {
        boolean speech = gate.speech(samples, count);
        boolean detected = false;
        if (speech) {
            if (!wasSpeech) {
                utteranceStarted = now;
                for (short[] frame : preRoll) recognizer.acceptWaveForm(frame, frame.length);
            }
            boolean ended = recognizer.acceptWaveForm(samples, count);
            JSONObject result = new JSONObject(ended ? recognizer.getResult() : recognizer.getPartialResult());
            String text = normalize(result.optString(ended ? "text" : "partial"));
            if (text.equals(phrase)) {
                if (matchedAt == 0) matchedAt = now;
                detected = ended || now - matchedAt >= 200;
            } else matchedAt = 0;
            if (ended || now - utteranceStarted > 15000) { recognizer.reset(); utteranceStarted = now; }
        } else if (wasSpeech) reset();
        preRoll.add(java.util.Arrays.copyOf(samples, count));
        while (preRoll.size() > 15) preRoll.removeFirst();
        wasSpeech = speech;
        if (detected) reset();
        return detected;
    }

    void reset() { recognizer.reset(); preRoll.clear(); wasSpeech = false; matchedAt = 0; }
    @Override public void close() { recognizer.close(); model.close(); }
}
