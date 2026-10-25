package ru.habab.home;

import org.junit.Test;
import static org.junit.Assert.*;

public class WakeMatchGateTest {
    @Test public void partialExactThenDifferentEndpointFinalDoesNotMatch() {
        WakeMatchGate gate = new WakeMatchGate("эй дом");
        assertFalse(gate.accept("эй дом", WakeMatchGate.ResultKind.PARTIAL));
        assertFalse(gate.accept("эй том", WakeMatchGate.ResultKind.ENDPOINT));
        assertFalse(gate.accept("эй дом", WakeMatchGate.ResultKind.ENDPOINT));
    }

    @Test public void partialExactThenMatchingEndpointFinalMatchesOnce() {
        WakeMatchGate gate = new WakeMatchGate("эй дом");
        assertFalse(gate.accept("эй дом", WakeMatchGate.ResultKind.PARTIAL));
        assertTrue(gate.accept("эй дом", WakeMatchGate.ResultKind.ENDPOINT));
        assertFalse(gate.accept("эй дом", WakeMatchGate.ResultKind.ENDPOINT));
    }

    @Test public void vadEndFlushCanConfirmExactPhrase() {
        WakeMatchGate gate = new WakeMatchGate("эй дом");
        assertFalse(gate.accept("эй дом", WakeMatchGate.ResultKind.PARTIAL));
        assertTrue(gate.accept("эй дом", WakeMatchGate.ResultKind.VAD_END));
    }

    @Test public void resetAllowsSamePhraseInTheNextUtterance() {
        WakeMatchGate gate = new WakeMatchGate("эй дом");
        assertTrue(gate.accept("эй дом", WakeMatchGate.ResultKind.ENDPOINT));
        assertFalse(gate.accept("эй дом", WakeMatchGate.ResultKind.ENDPOINT));
        gate.reset();
        assertTrue(gate.accept("эй дом", WakeMatchGate.ResultKind.VAD_END));
    }

    @Test public void userPhraseUsesWholeNormalizedTextAndKeepsWordBoundaries() {
        WakeMatchGate gate = new WakeMatchGate(WakeMatchGate.normalize("Эй, Дом!"));
        assertFalse(gate.accept("эй домик", WakeMatchGate.ResultKind.ENDPOINT));
        gate.reset();
        assertFalse(gate.accept("дом эй", WakeMatchGate.ResultKind.ENDPOINT));
        gate.reset();
        assertFalse(gate.accept("unk", WakeMatchGate.ResultKind.ENDPOINT));
        gate.reset();
        assertTrue(gate.accept("ЭЙ — ДОМ", WakeMatchGate.ResultKind.ENDPOINT));
    }
}
