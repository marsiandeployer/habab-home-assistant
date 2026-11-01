package ru.habab.home;

import org.junit.Test;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.util.ArrayDeque;
import java.util.Queue;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

public final class UsbByteRelayTest {
    @Test
    public void idleBulkTransferThenDataIsForwardedUnchangedUntilStopped() throws Exception {
        byte[] expected = new byte[] {0, 1, -1, 42, 0x55};
        Queue<Integer> reads = new ArrayDeque<>();
        reads.add(-1);
        reads.add(expected.length);
        AtomicBoolean active = new AtomicBoolean(true);
        AtomicInteger pauses = new AtomicInteger();
        ByteArrayOutputStream output = new ByteArrayOutputStream();

        UsbByteRelay.copy(buffer -> {
            int result = reads.remove();
            if (result > 0) {
                System.arraycopy(expected, 0, buffer, 0, result);
                active.set(false);
            }
            return result;
        }, active::get, () -> true, output, millis -> {
            assertEquals(25L, millis);
            pauses.incrementAndGet();
        });

        assertEquals(1, pauses.get());
        assertArrayEquals(expected, output.toByteArray());
    }

    @Test
    public void repeatedImmediateIdleResultsAreRateLimitedAndDoNotBusyLoop() throws Exception {
        AtomicBoolean active = new AtomicBoolean(true);
        AtomicInteger reads = new AtomicInteger();
        AtomicInteger waits = new AtomicInteger();

        UsbByteRelay.copy(buffer -> {
            reads.incrementAndGet();
            return -1;
        }, active::get, () -> true, new ByteArrayOutputStream(), millis -> {
            assertEquals(25L, millis);
            if (waits.incrementAndGet() == 8) active.set(false);
        });

        assertTrue(reads.get() <= 8);
        assertEquals(8, waits.get());
    }

    @Test
    public void detachOrPermissionLossTerminatesRelay() {
        AtomicBoolean attached = new AtomicBoolean(true);
        AtomicInteger reads = new AtomicInteger();
        AtomicInteger waits = new AtomicInteger();
        assertThrows(IOException.class, () -> UsbByteRelay.copy(buffer -> {
            reads.incrementAndGet();
            attached.set(false);
            return -1;
        }, () -> true, attached::get, new ByteArrayOutputStream(), millis -> waits.incrementAndGet()));
        assertEquals(1, reads.get());
        assertEquals(0, waits.get());
    }

    @Test
    public void permissionLossBeforeReadDoesNotAccessEndpoint() {
        AtomicInteger reads = new AtomicInteger();
        assertThrows(IOException.class, () -> UsbByteRelay.copy(buffer -> {
            reads.incrementAndGet();
            return 0;
        }, () -> true, () -> false, new ByteArrayOutputStream(), millis -> { }));
        assertEquals(0, reads.get());
    }


    @Test
    public void closedPeerDoesNotPreventNextClientFromUsingSameUsbReader() throws Exception {
        byte[][] packets = new byte[][] {{10, 11}, {20, 21, 22}};
        AtomicInteger nextPacket = new AtomicInteger();
        AtomicBoolean secondClientActive = new AtomicBoolean(true);
        UsbByteRelay.Reader sharedReader = buffer -> {
            int packetIndex = nextPacket.getAndIncrement();
            byte[] packet = packets[packetIndex];
            System.arraycopy(packet, 0, buffer, 0, packet.length);
            if (packetIndex == 1) secondClientActive.set(false);
            return packet.length;
        };

        assertThrows(UsbByteRelay.PeerClosedException.class, () -> UsbByteRelay.copy(
                sharedReader, () -> true, () -> true, new OutputStream() {
                    @Override public void write(int value) throws IOException {
                        throw new IOException("closed socket");
                    }
                    @Override public void write(byte[] bytes, int offset, int length)
                            throws IOException {
                        throw new IOException("closed socket");
                    }
                }, millis -> { }));

        ByteArrayOutputStream secondClient = new ByteArrayOutputStream();
        UsbByteRelay.copy(sharedReader, secondClientActive::get, () -> true,
                secondClient, millis -> { });

        assertArrayEquals(packets[1], secondClient.toByteArray());
    }

    @Test
    public void rejectsReaderCountLargerThanBuffer() {
        assertThrows(IOException.class, () -> UsbByteRelay.copy(buffer -> buffer.length + 1,
                () -> true, () -> true, new ByteArrayOutputStream(), millis -> { }));
    }
}
