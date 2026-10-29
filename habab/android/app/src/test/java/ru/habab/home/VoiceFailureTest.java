package ru.habab.home;

import java.io.IOException;
import java.net.SocketTimeoutException;
import org.junit.Test;
import static org.junit.Assert.*;

public class VoiceFailureTest {
    @Test public void preservesSafeBackendReasonThroughBothNativeLayers() {
        assertEquals("habab_voice_not_ready", VoiceFailure.code(VoiceFailure.fromHttp(503, "habab_voice_not_ready")));
        VoiceFailure failure = VoiceFailure.fromHttp(400, "openai_required");
        assertEquals("openai_required", VoiceFailure.code(failure));
        assertEquals("realtime_provider_rejected", VoiceFailure.code(VoiceFailure.fromHttp(502, "realtime_provider_rejected")));
    }
    @Test public void neverPropagatesRawBackendOrExceptionText() {
        String privateValue = "provider body includes a private token";
        assertEquals("realtime_unavailable", VoiceFailure.code(VoiceFailure.fromHttp(502, privateValue)));
        assertEquals("voice_connection_failed", VoiceFailure.code(new IOException(privateValue)));
        assertEquals("voice_authorization_expired", VoiceFailure.code(VoiceFailure.fromHttp(401, privateValue)));
    }
    @Test public void classifiesTimeoutAndRetainsExistingKnownLocalFailures() {
        assertEquals("voice_timeout", VoiceFailure.code(new SocketTimeoutException("private URL")));
        assertEquals("unsupported_wake_word", VoiceFailure.code(new IOException("unsupported_wake_word")));
        assertEquals("voice_model_unavailable", VoiceFailure.code(new UnsatisfiedLinkError("private library path")));
        assertEquals("voice_connection_failed", VoiceFailure.normalize(null));
    }
}
