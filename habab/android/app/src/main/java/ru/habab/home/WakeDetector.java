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
    interface Diagnostics {
        void record(VoiceLifecycleTrace.Event event, VoiceLifecycleTrace.Reason reason,
                long attempt, long elapsedAt, int rmsPcm16, int vadSpeech, int partialPresent,
                int phraseMatch, int confidencePermille, long elapsedFromWakeMs);
    }
    private final Model model;
    private final Recognizer recognizer;
    private final String phrase;
    private final WakeMatchGate matchGate;
    private final WakeVerifier verifier;
    private final VoiceActivityGate gate = new VoiceActivityGate();
    private final Diagnostics diagnostics;
    private final ArrayDeque<short[]> preRoll = new ArrayDeque<>();
    private boolean wasSpeech;
    private long utteranceStarted;
    private long attemptSequence;
    private long currentAttempt;
    private long attemptStartedAt;
    private long lastPartialSampleAt;
    private boolean partialStateKnown, partialNonempty, partialExact;
    private boolean recognizerHasInput;
    private int lastRmsPcm16;
    private VoiceLifecycleTrace.Reason lastResetReason;
    private WakeMatchGate.MatchSource lastMatchSource;
    private long lastMatchAttempt;
    private short[] verifiedAudio;
    private WakeVerifier.Result diagnosticResult;

    static String normalize(String value) {
        return WakeMatchGate.normalize(value);
    }

    WakeDetector(String directory, String requested, Diagnostics diagnostics) throws Exception {
        phrase = normalize(requested);
        if (phrase.isEmpty() || phrase.length() > 64 || phrase.split(" ").length > 5) throw new IOException("invalid_wake_phrase");
        this.diagnostics = diagnostics;
        matchGate = new WakeMatchGate(phrase);
        LibVosk.vosk_set_log_level(-1);
        model = new Model(directory);
        Recognizer created = null;
        try {
            Dictionary dictionary = Native.load("vosk", Dictionary.class);
            for (String word : phrase.split(" "))
                if (dictionary.vosk_model_find_word(model.getPointer(), word) < 0) throw new IOException("unsupported_wake_word");
            // Vosk estimates a language model from grammar occurrence counts. A single
            // known phrase otherwise dominates unrelated speech, even with confidence 1.0.
            // Keep custom phrases unchanged until they have their own labelled evidence.
            JSONArray grammar = new JSONArray().put(phrase);
            int unknownCount = phrase.equals("эй дом") ? 16 : 1;
            for (int i = 0; i < unknownCount; i++) grammar.put("[unk]");
            recognizer = created = new Recognizer(model, 16000, grammar.toString());
            recognizer.setWords(true);
            recognizer.setEndpointerMode(Recognizer.EndpointerMode.SHORT);
            verifier = phrase.equals(WakeVerifier.DEFAULT_PHRASE) ? new WakeVerifier(model) : null;
        } catch (Exception | LinkageError error) {
            if (created != null) created.close();
            model.close(); throw error;
        }
    }

    boolean accept(short[] samples, int count, long now) throws Exception {
        lastMatchSource = null;
        if (pollVerification(now)) return true;
        lastRmsPcm16 = rmsPcm16(samples, count);
        boolean speech = gate.speech(samples, count);
        boolean detected = false;
        if (speech) {
            if (!wasSpeech) {
                currentAttempt = ++attemptSequence;
                attemptStartedAt = now;
                utteranceStarted = now;
                lastPartialSampleAt = 0;
                partialStateKnown = false;
                record(VoiceLifecycleTrace.Event.WAKE_VAD_START, VoiceLifecycleTrace.Reason.VAD,
                        now, lastRmsPcm16, 1, -1, -1, -1);
                for (short[] frame : preRoll) {
                    if (acceptFrame(frame, frame.length, now, 1, rmsPcm16(frame, frame.length))) {
                        detected = true; break;
                    }
                }
            }
            if (!detected) detected = acceptFrame(samples, count, now, 1, lastRmsPcm16);
            if (now - utteranceStarted > 15000) {
                resetRecognizer(VoiceLifecycleTrace.Reason.TIME_LIMIT, now, lastRmsPcm16, 1);
                utteranceStarted = now;
            }
        } else if (wasSpeech) {
            record(VoiceLifecycleTrace.Event.WAKE_VAD_END, VoiceLifecycleTrace.Reason.VAD,
                    now, lastRmsPcm16, 0, -1, -1, -1);
            // The VAD closes an utterance after its hangover. Flush Vosk's remaining
            // frames before resetting, otherwise a final wake phrase can be discarded.
            detected = acceptFinal(recognizer.getFinalResult(), WakeMatchGate.ResultKind.VAD_END,
                    VoiceLifecycleTrace.Reason.VAD_END, now, lastRmsPcm16, 0);
            resetRecognizer(VoiceLifecycleTrace.Reason.VAD_END, now, lastRmsPcm16, 0);
        }
        if (detected) return true;
        preRoll.add(java.util.Arrays.copyOf(samples, count));
        while (preRoll.size() > 15) preRoll.removeFirst();
        wasSpeech = speech;
        return false;
    }

    private boolean acceptFrame(short[] samples, int count, long now, int vadSpeech, int rms) throws Exception {
        recognizerHasInput = true;
        if (verifier != null) verifier.append(samples, count);
        if (!recognizer.acceptWaveForm(samples, count)) {
            samplePartial(now, rms, vadSpeech);
            return false;
        }
        boolean detected = acceptFinal(recognizer.getResult(), WakeMatchGate.ResultKind.ENDPOINT,
                VoiceLifecycleTrace.Reason.ENDPOINT, now, rms, vadSpeech);
        resetRecognizer(VoiceLifecycleTrace.Reason.ENDPOINT, now, rms, vadSpeech);
        utteranceStarted = now;
        return detected;
    }

    private boolean acceptFinal(String json, WakeMatchGate.ResultKind kind,
            VoiceLifecycleTrace.Reason reason, long now, int rms, int vadSpeech) throws Exception {
        JSONObject result = new JSONObject(json);
        String text = result.optString("text");
        boolean exact = phrase.equals(normalize(text));
        int confidence = confidencePermille(result);
        int minimumConfidence = minimumConfidencePermille(result);
        boolean confidenceAccepted = !phrase.equals("эй дом") || WakeMatchGate.confidenceAccepted(minimumConfidence);
        boolean detected = confidenceAccepted && matchGate.accept(text, kind);
        record(VoiceLifecycleTrace.Event.WAKE_FINAL, reason, now, rms, vadSpeech,
                -1, exact ? 1 : 0, confidence);
        if (detected && verifier != null) {
            WakeVerifier.Result immediate = verifier.submit(result,
                    new WakeVerifier.Context(currentAttempt, attemptStartedAt, matchGate.matchSource(),
                            rms, vadSpeech, confidence));
            if (immediate != null) recordVerification(immediate, now);
            return false;
        }
        if (detected) {
            lastMatchSource = matchGate.matchSource();
            lastMatchAttempt = currentAttempt;
            record(VoiceLifecycleTrace.Event.WAKE_ACCEPTED, VoiceLifecycleTrace.Reason.EXACT_FINAL,
                    now, rms, vadSpeech, -1, 1, confidence);
        } else {
            record(VoiceLifecycleTrace.Event.WAKE_REJECTED,
                    exact && !confidenceAccepted ? VoiceLifecycleTrace.Reason.LOW_CONFIDENCE : reason,
                    now, rms, vadSpeech, -1, exact ? 1 : 0,
                    exact && !confidenceAccepted ? minimumConfidence : confidence);
        }
        return detected;
    }

    /** Poll without submitting synthetic audio; also used by native replay drain. */
    boolean hasPendingVerification() { return verifier != null && verifier.hasPending(); }

    void setDiagnosticCaptureEnabled(boolean enabled) {
        if (verifier != null) verifier.setDiagnosticCaptureEnabled(enabled);
        if (!enabled) clearDiagnosticResult();
    }

    /** Consume the opt-in verification snapshot. Caller owns and must zeroize diagnosticAudio. */
    WakeVerifier.Result consumeDiagnosticResult() {
        WakeVerifier.Result result = diagnosticResult; diagnosticResult = null; return result;
    }

    boolean pollVerification(long now) {
        if (verifier == null) return false;
        WakeVerifier.Result result = verifier.poll();
        if (result == null) return false;
        recordVerification(result, now);
        if (result.diagnosticAudio != null) {
            clearDiagnosticResult();
            diagnosticResult = result;
        }
        if (!result.accepted()) return false;
        lastMatchSource = result.context.source;
        lastMatchAttempt = result.context.attempt;
        verifiedAudio = result.acceptedAudio;
        return true;
    }

    /** Caller consumes even when opt-in clip recording is disabled. */
    short[] consumeVerifiedAudio() {
        short[] audio = verifiedAudio; verifiedAudio = null; return audio;
    }

    private void recordVerification(WakeVerifier.Result result, long now) {
        android.util.Log.i("HababWakeVerify", "outcome=" + result.outcome.name()
                + " elapsed_ms=" + result.elapsedMs + " decoded_frames=" + (result.decodedSamples / 320));
        WakeVerifier.Context context = result.context;
        diagnostics.record(result.accepted() ? VoiceLifecycleTrace.Event.WAKE_ACCEPTED : VoiceLifecycleTrace.Event.WAKE_REJECTED,
                result.accepted() ? VoiceLifecycleTrace.Reason.EXACT_FINAL
                        : result.outcome == WakeVerifier.Outcome.ERROR ? VoiceLifecycleTrace.Reason.VERIFIER_ERROR
                        : VoiceLifecycleTrace.Reason.VERIFIER_REJECTED,
                context.attempt, now, context.rms, context.vadSpeech, -1, 1, context.confidence,
                context.attemptStartedAt == 0 ? -1 : Math.max(0, now - context.attemptStartedAt));
    }

    private void samplePartial(long now, int rms, int vadSpeech) throws Exception {
        if (now - lastPartialSampleAt < 500) return;
        lastPartialSampleAt = now;
        JSONObject partialResult = new JSONObject(recognizer.getPartialResult());
        String partial = normalize(partialResult.optString("partial"));
        boolean nonempty = !partial.isEmpty();
        boolean exact = phrase.equals(partial);
        if (!partialStateKnown || nonempty != partialNonempty || exact != partialExact) {
            record(VoiceLifecycleTrace.Event.WAKE_PARTIAL_STATE, VoiceLifecycleTrace.Reason.PARTIAL,
                    now, rms, vadSpeech, nonempty ? 1 : 0, exact ? 1 : 0, -1);
            partialStateKnown = true;
            partialNonempty = nonempty;
            partialExact = exact;
        }
    }

    private int confidencePermille(JSONObject result) {
        JSONArray words = result.optJSONArray("result");
        if (words == null || words.length() == 0) return -1;
        double total = 0;
        int count = 0;
        for (int i = 0; i < words.length(); i++) {
            JSONObject word = words.optJSONObject(i);
            if (word == null) continue;
            double confidence = word.optDouble("conf", Double.NaN);
            if (Double.isNaN(confidence) || Double.isInfinite(confidence)) continue;
            total += Math.max(0, Math.min(1, confidence));
            count++;
        }
        return count == 0 ? -1 : (int) Math.round(total * 1000 / count);
    }

    private int minimumConfidencePermille(JSONObject result) {
        JSONArray words = result.optJSONArray("result");
        if (words == null || words.length() != phrase.split(" ").length) return -1;
        double minimum = 1;
        for (int i = 0; i < words.length(); i++) {
            JSONObject word = words.optJSONObject(i);
            if (word == null) return -1;
            double confidence = word.optDouble("conf", Double.NaN);
            if (Double.isNaN(confidence) || Double.isInfinite(confidence)) return -1;
            minimum = Math.min(minimum, Math.max(0, Math.min(1, confidence)));
        }
        return (int) Math.round(minimum * 1000);
    }

    private int rmsPcm16(short[] samples, int count) {
        if (count <= 0) return 0;
        double power = 0;
        for (int i = 0; i < count; i++) power += (double) samples[i] * samples[i];
        return (int) Math.round(Math.sqrt(power / count));
    }

    private void record(VoiceLifecycleTrace.Event event, VoiceLifecycleTrace.Reason reason,
            long now, int rms, int vadSpeech, int partialPresent,
            int phraseMatch, int confidencePermille) {
        diagnostics.record(event, reason, currentAttempt, now, rms, vadSpeech, partialPresent, phraseMatch,
                confidencePermille, attemptStartedAt == 0 ? -1 : Math.max(0, now - attemptStartedAt));
    }

    private void resetRecognizer(VoiceLifecycleTrace.Reason reason, long now, int rms, int vadSpeech) {
        boolean recordReset = recognizerHasInput || partialStateKnown || wasSpeech || lastResetReason != reason;
        recognizer.reset();
        matchGate.reset();
        recognizerHasInput = false;
        partialStateKnown = false;
        lastResetReason = reason;
        if (recordReset) record(VoiceLifecycleTrace.Event.RECOGNIZER_RESET, reason, now, rms,
                vadSpeech, -1, -1, -1);
    }

    WakeMatchGate.MatchSource lastMatchSource() { return lastMatchSource; }
    long lastMatchAttempt() { return lastMatchAttempt; }

    void reset(long now, VoiceLifecycleTrace.Reason reason) {
        lastMatchSource = null;
        if (verifiedAudio != null) java.util.Arrays.fill(verifiedAudio, (short) 0);
        verifiedAudio = null;
        clearDiagnosticResult();
        resetRecognizer(reason, now, lastRmsPcm16, wasSpeech ? 1 : 0);
        preRoll.clear(); wasSpeech = false;
        if (verifier != null) verifier.clearAudio();
    }
    @Override public void close() {
        if (verifiedAudio != null) java.util.Arrays.fill(verifiedAudio, (short) 0);
        verifiedAudio = null;
        clearDiagnosticResult();
        if (verifier != null) verifier.close();
        recognizer.close(); model.close();
    }

    private void clearDiagnosticResult() {
        if (diagnosticResult != null && diagnosticResult.diagnosticAudio != null)
            java.util.Arrays.fill(diagnosticResult.diagnosticAudio, (short) 0);
        diagnosticResult = null;
    }
}
