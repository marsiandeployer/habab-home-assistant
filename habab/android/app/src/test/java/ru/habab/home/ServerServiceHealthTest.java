package ru.habab.home;

import static org.junit.Assert.*;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.Test;

public class ServerServiceHealthTest {
    @Test public void acceptsUnauthorizedHealthResponseDelayedBeyondOldReadTimeout() throws Exception {
        try (ServerSocket listener = new ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))) {
            AtomicReference<Exception> serverFailure = new AtomicReference<>();
            Thread responder = new Thread(() -> {
                try (Socket client = listener.accept()) {
                    BufferedReader request = new BufferedReader(new InputStreamReader(
                            client.getInputStream(), StandardCharsets.US_ASCII));
                    String line;
                    do { line = request.readLine(); } while (line != null && !line.isEmpty());
                    Thread.sleep(2000);
                    byte[] body = "{}".getBytes(StandardCharsets.US_ASCII);
                    OutputStream response = client.getOutputStream();
                    response.write(("HTTP/1.1 401 Unauthorized\r\n"
                            + "Content-Type: application/json\r\n"
                            + "Content-Length: " + body.length + "\r\n"
                            + "Connection: close\r\n\r\n").getBytes(StandardCharsets.US_ASCII));
                    response.write(body);
                    response.flush();
                } catch (Exception error) {
                    serverFailure.set(error);
                }
            }, "server-service-health-test");
            responder.start();

            boolean healthy = ServerService.healthyAt(
                    "http://127.0.0.1:" + listener.getLocalPort() + "/api/habab_home/status");

            responder.join(5000);
            assertFalse("local health responder did not finish", responder.isAlive());
            if (serverFailure.get() != null) throw new AssertionError("local health responder failed", serverFailure.get());
            assertTrue("delayed unauthorized response should mark Core ready", healthy);
        }
    }
}
