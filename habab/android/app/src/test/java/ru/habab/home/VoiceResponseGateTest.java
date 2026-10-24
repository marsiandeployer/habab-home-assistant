package ru.habab.home;

import org.junit.Test;
import static org.junit.Assert.*;

public class VoiceResponseGateTest {
    @Test public void aFastToolWaitsForThePreambleResponseToFinish() {
        VoiceResponseGate gate = new VoiceResponseGate(); Runnable result = () -> {};
        gate.created("preamble");
        assertNull(gate.request(result));
        assertSame(result, gate.done("preamble"));
        assertNull(gate.done("preamble"));
    }

    @Test public void lateCompletionCannotFinishTheNextResponse() {
        VoiceResponseGate gate = new VoiceResponseGate(); Runnable result = () -> {};
        gate.created("old"); gate.done("old");
        assertSame(result, gate.request(result));
        assertNull(gate.done("old"));
        gate.created("new"); assertNull(gate.request(result));
        assertNull(gate.done("old")); assertSame(result, gate.done("new"));
    }

    @Test public void closingDropsPendingAndLateToolResponses() {
        VoiceResponseGate gate = new VoiceResponseGate(); Runnable result = () -> {};
        gate.created("preamble"); gate.request(result); gate.close();
        assertNull(gate.done("preamble")); assertNull(gate.request(result));
    }

    @Test public void bargeInKeepsALateToolFromSpeakingOverTheNewUserTurn() {
        VoiceResponseGate gate = new VoiceResponseGate(); Runnable stale = () -> {}, next = () -> {};
        gate.created("old"); assertNull(gate.request(stale));
        assertEquals("old", gate.interrupted()); assertNull(gate.done("old"));
        gate.created("new"); assertNull(gate.done("new"));
        assertSame(next, gate.request(next));
    }
}
