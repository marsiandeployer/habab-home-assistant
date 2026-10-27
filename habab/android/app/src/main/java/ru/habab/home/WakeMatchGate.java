package ru.habab.home;

import java.text.Normalizer;
import java.util.Locale;

/** Accepts only one whole-phrase match from a finalized recognizer result. */
final class WakeMatchGate {
    enum ResultKind { PARTIAL, ENDPOINT, VAD_END }
    enum MatchSource {
        ENDPOINT_FINAL("endpoint_final"),
        VAD_END_FINAL("vad_end_final");

        private final String statusValue;

        MatchSource(String statusValue) { this.statusValue = statusValue; }

        String statusValue() { return statusValue; }
    }

    private final String phrase;
    private boolean finalResultHandled;
    private MatchSource matchSource;

    WakeMatchGate(String normalizedPhrase) { phrase = normalize(normalizedPhrase); }

    static String normalize(String value) {
        if (value == null) return "";
        return Normalizer.normalize(value, Normalizer.Form.NFKC)
                .toLowerCase(new Locale("ru"))
                .replaceAll("[^\\p{L}\\s]", " ").trim().replaceAll("\\s+", " ");
    }

    boolean accept(String text, ResultKind kind) {
        if (kind == null || kind == ResultKind.PARTIAL || finalResultHandled) return false;
        finalResultHandled = true;
        if (!phrase.equals(normalize(text))) return false;
        matchSource = kind == ResultKind.ENDPOINT ? MatchSource.ENDPOINT_FINAL : MatchSource.VAD_END_FINAL;
        return true;
    }

    MatchSource matchSource() { return matchSource; }

    void reset() { finalResultHandled = false; matchSource = null; }
}
