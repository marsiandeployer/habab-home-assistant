package ru.habab.home;

import org.junit.Test;
import java.util.concurrent.atomic.AtomicInteger;
import static org.junit.Assert.*;

public class VoiceRequestGateTest {
    @Test public void closeCancelsRegisteredCallsEvenBeforeTheirExecution() {
        VoiceRequestGate gate = new VoiceRequestGate(); AtomicInteger canceled = new AtomicInteger();
        assertNotNull(gate.register(canceled::incrementAndGet));
        gate.close(); gate.close(); assertEquals(1, canceled.get());
    }
    @Test public void aLateCallbackCannotRegisterANewRequest() {
        VoiceRequestGate gate = new VoiceRequestGate(); AtomicInteger canceled = new AtomicInteger();
        gate.close(); assertNull(gate.register(canceled::incrementAndGet)); assertEquals(1, canceled.get());
    }
    @Test public void completedCallsAreNotCanceledAgain() {
        VoiceRequestGate gate = new VoiceRequestGate(); AtomicInteger canceled = new AtomicInteger();
        VoiceRequestGate.Ticket ticket = gate.register(canceled::incrementAndGet);
        ticket.close(); gate.close(); assertEquals(0, canceled.get());
    }
}
