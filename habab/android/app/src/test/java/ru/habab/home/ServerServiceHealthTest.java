package ru.habab.home;

import static org.junit.Assert.*;
import android.content.SharedPreferences;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.Test;

public class ServerServiceHealthTest {
    @Test public void voiceStartsOnlyAfterSavedConsentAndServerReadiness() {
        assertTrue(ServerService.voiceAutoStartAllowed(true, true, true, false));
        assertFalse(ServerService.voiceAutoStartAllowed(true, true, true, true));
        assertFalse(ServerService.voiceAutoStartAllowed(false, true, true, false));
        assertFalse(ServerService.voiceAutoStartAllowed(true, false, true, false));
        assertFalse(ServerService.voiceAutoStartAllowed(true, true, false, false));
        assertTrue(ServerService.serverStartAllowed(true, true, false));
        assertFalse(ServerService.serverStartAllowed(false, true, false));
        assertFalse(ServerService.serverStartAllowed(true, false, false));
        assertFalse(ServerService.serverStartAllowed(true, true, true));
        assertTrue(ServerService.voiceStartAllowed(true, true, true, true, false));
        assertFalse(ServerService.voiceStartAllowed(false, true, true, true, false));
        assertFalse(ServerService.voiceStartAllowed(true, false, true, true, false));
        assertFalse(ServerService.voiceStartAllowed(true, true, false, true, false));
        assertFalse(ServerService.voiceStartAllowed(true, true, true, false, false));
        assertFalse(ServerService.voiceStartAllowed(true, true, true, true, true));
    }

    @Test public void bootAutostartMigratesLegacySettingsAndHonorsExplicitChoice() {
        assertTrue(ServerService.resolveAutoStartOnBoot(false, false, false, true));
        assertTrue(ServerService.resolveAutoStartOnBoot(false, false, true, false));
        assertFalse(ServerService.resolveAutoStartOnBoot(false, false, false, false));
        assertTrue(ServerService.resolveAutoStartOnBoot(true, true, false, false));
        assertFalse(ServerService.resolveAutoStartOnBoot(true, false, true, true));
    }

    @Test public void bootStartRestoresServerAndVoiceAfterCurrentRunStop() {
        Map<String, Object> values = new HashMap<>();
        values.put("desired", false);
        values.put(ServerService.PREF_AUTO_START_SUPPRESSED, true);
        values.put(ServerService.PREF_AUTO_START_ATTEMPTED, true);
        SharedPreferences server = inMemoryPreferences(values);

        assertTrue(ServerService.bootStartAllowed(true, true));
        assertTrue(ServerService.applyBootStartPreferences(server, true));
        assertTrue(server.getBoolean("desired", false));
        assertFalse(server.getBoolean(ServerService.PREF_AUTO_START_SUPPRESSED, true));
        assertFalse(server.getBoolean(ServerService.PREF_AUTO_START_ATTEMPTED, true));
        assertTrue(ServerService.serverStartAllowed(true, true,
                server.getBoolean(ServerService.PREF_AUTO_START_SUPPRESSED, true)));
        assertTrue(ServerService.voiceStartAllowed(true, true, true, true,
                server.getBoolean(ServerService.PREF_AUTO_START_SUPPRESSED, true)));
    }

    @Test public void disabledOrNonBootStartDoesNotMutatePreviousRunState() {
        Map<String, Object> values = new HashMap<>();
        values.put("desired", false);
        values.put(ServerService.PREF_AUTO_START_SUPPRESSED, true);
        values.put(ServerService.PREF_AUTO_START_ATTEMPTED, true);
        SharedPreferences server = inMemoryPreferences(values);

        assertFalse(ServerService.bootStartAllowed(true, false));
        assertFalse(ServerService.applyBootStartPreferences(server, false));
        assertFalse(ServerService.bootStartAllowed(false, true));
        assertFalse(server.getBoolean("desired", true));
        assertTrue(server.getBoolean(ServerService.PREF_AUTO_START_SUPPRESSED, false));
        assertTrue(server.getBoolean(ServerService.PREF_AUTO_START_ATTEMPTED, false));
    }

    private static SharedPreferences inMemoryPreferences(Map<String, Object> values) {
        InvocationHandler editorHandler = new InvocationHandler() {
            private final Map<String, Object> pending = new HashMap<>();
            @Override public Object invoke(Object proxy, Method method, Object[] args) {
                switch (method.getName()) {
                    case "putBoolean": pending.put((String) args[0], args[1]); return proxy;
                    case "apply": values.putAll(pending); pending.clear(); return null;
                    case "commit": values.putAll(pending); pending.clear(); return true;
                    default: throw new UnsupportedOperationException(method.getName());
                }
            }
        };
        SharedPreferences.Editor editor = (SharedPreferences.Editor) Proxy.newProxyInstance(
                SharedPreferences.Editor.class.getClassLoader(), new Class<?>[]{SharedPreferences.Editor.class}, editorHandler);
        InvocationHandler preferencesHandler = (proxy, method, args) -> {
            switch (method.getName()) {
                case "edit": return editor;
                case "contains": return values.containsKey(args[0]);
                case "getBoolean": return values.containsKey(args[0]) ? values.get(args[0]) : args[1];
                default: throw new UnsupportedOperationException(method.getName());
            }
        };
        return (SharedPreferences) Proxy.newProxyInstance(SharedPreferences.class.getClassLoader(),
                new Class<?>[]{SharedPreferences.class}, preferencesHandler);
    }

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
