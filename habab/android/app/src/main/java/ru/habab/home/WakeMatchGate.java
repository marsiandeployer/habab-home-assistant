package ru.habab.home;

import java.text.Normalizer;
import java.util.Locale;

/** Accepts only one whole-phrase match from a finalized recognizer result. */
final class WakeMatchGate {
    enum ResultKind { PARTIAL, ENDPOINT, VAD_END }

    private final String phrase;
    private boolean finalResultHandled;

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
        return phrase.equals(normalize(text));
    }

    void reset() { finalResultHandled = false; }
}
