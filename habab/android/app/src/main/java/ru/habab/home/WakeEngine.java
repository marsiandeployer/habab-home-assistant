package ru.habab.home;

/** One selected local detector consumes the existing 16 kHz capture stream. */
interface WakeEngine extends AutoCloseable {
    boolean accept(short[] samples, int count, long now) throws Exception;
    void reset(long now, VoiceLifecycleTrace.Reason reason);
    WakeMatchGate.MatchSource lastMatchSource();
    long lastMatchAttempt();
    void setDiagnosticCaptureEnabled(boolean enabled);
    WakeVerifier.Result consumeDiagnosticResult();
    short[] consumeVerifiedAudio();
    WakeEngineInfo info();
    @Override void close();
}
