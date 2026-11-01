package ru.habab.home;

import java.io.IOException;
import java.io.OutputStream;
import java.util.function.BooleanSupplier;

/** Copies only bytes received from an attached USB CDC endpoint to its socket peer. */
final class UsbByteRelay {
    private static final int BUFFER_SIZE = 4096;
    private static final long IDLE_PAUSE_MILLIS = 25L;

    private UsbByteRelay() { }

    interface Reader {
        int read(byte[] buffer) throws IOException;
    }

    interface Waiter {
        void pause(long millis) throws InterruptedException;
    }

    static final class PeerClosedException extends IOException {
        PeerClosedException(IOException cause) {
            super("USB relay peer closed", cause);
        }
    }

    static void copy(Reader reader, BooleanSupplier active,
            BooleanSupplier attachedAndPermitted, OutputStream output, Waiter waiter)
            throws IOException, InterruptedException {
        byte[] buffer = new byte[BUFFER_SIZE];
        while (active.getAsBoolean()) {
            if (!attachedAndPermitted.getAsBoolean()) {
                throw new IOException("USB device detached or permission lost");
            }

            int count = reader.read(buffer);
            if (count > buffer.length) throw new IOException("USB reader returned an invalid byte count");
            if (count > 0) {
                try {
                    output.write(buffer, 0, count);
                    output.flush();
                } catch (IOException error) {
                    throw new PeerClosedException(error);
                }
                continue;
            }

            // Android bulkTransfer commonly reports -1 on an idle endpoint. Treat both
            // nonpositive values as idle only while this exact device remains usable.
            if (!attachedAndPermitted.getAsBoolean()) {
                throw new IOException("USB device detached or permission lost");
            }
            if (active.getAsBoolean()) waiter.pause(IDLE_PAUSE_MILLIS);
        }
    }
}
