package ru.habab.home;

import com.sun.jna.Library;
import com.sun.jna.Native;
import com.sun.jna.Pointer;
import java.io.IOException;
import java.util.ArrayDeque;
import org.json.JSONArray;
import org.json.JSONObject;
import org.vosk.*;

final class WakeDetector implements AutoCloseable {
    public interface Dictionary extends Library { int vosk_model_find_word(Pointer model, String word); }
    private final Model model;
    private final Recognizer recognizer;
    private final String phrase;
    private final WakeMatchGate matchGate;
    private final VoiceActivityGate gate = new VoiceActivityGate();
    private final ArrayDeque<short[]> preRoll = new ArrayDeque<>();
    private boolean wasSpeech;
    private long utteranceStarted;
    private WakeMatchGate.MatchSource lastMatchSource;

    static String normalize(String value) {
        return WakeMatchGate.normalize(value);
    }

    WakeDetector(String directory, String requested) throws Exception {
        phrase = normalize(requested);
        if (phrase.isEmpty() || phrase.length() > 64 || phrase.split(" ").length > 5) throw new IOException("invalid_wake_phrase");
        matchGate = new WakeMatchGate(phrase);
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
        lastMatchSource = null;
        boolean speech = gate.speech(samples, count);
        boolean detected = false;
        if (speech) {
            if (!wasSpeech) {
                utteranceStarted = now;
                for (short[] frame : preRoll) {
                    if (acceptFrame(frame, frame.length, now)) { detected = true; break; }
                }
            }
            if (!detected) detected = acceptFrame(samples, count, now);
            if (now - utteranceStarted > 15000) { resetRecognizer(); utteranceStarted = now; }
        } else if (wasSpeech) {
            // The VAD closes an utterance after its hangover. Flush Vosk's remaining
            // frames before resetting, otherwise a final wake phrase can be discarded.
            JSONObject result = new JSONObject(recognizer.getFinalResult());
            detected = matchGate.accept(result.optString("text"), WakeMatchGate.ResultKind.VAD_END);
            if (detected) lastMatchSource = matchGate.matchSource();
            resetRecognizer();
        }
        if (detected) { resetRecognizer(); return true; }
        preRoll.add(java.util.Arrays.copyOf(samples, count));
        while (preRoll.size() > 15) preRoll.removeFirst();
        wasSpeech = speech;
        return false;
    }

    private boolean acceptFrame(short[] samples, int count, long now) throws Exception {
        if (!recognizer.acceptWaveForm(samples, count)) return false;
        JSONObject result = new JSONObject(recognizer.getResult());
        boolean detected = matchGate.accept(result.optString("text"), WakeMatchGate.ResultKind.ENDPOINT);
        if (detected) lastMatchSource = matchGate.matchSource();
        resetRecognizer();
        utteranceStarted = now;
        return detected;
    }

    private void resetRecognizer() { recognizer.reset(); matchGate.reset(); }

    WakeMatchGate.MatchSource lastMatchSource() { return lastMatchSource; }

    void reset() { lastMatchSource = null; resetRecognizer(); preRoll.clear(); wasSpeech = false; }
    @Override public void close() { recognizer.close(); model.close(); }
}
