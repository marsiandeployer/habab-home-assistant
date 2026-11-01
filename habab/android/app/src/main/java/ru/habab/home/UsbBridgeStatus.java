package ru.habab.home;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

/** Publishes the latest physical USB CDC bridge state for the guest runtime. */
final class UsbBridgeStatus {
    private static final String FILE_NAME = "usb-status.json";
    private static final String DEVICE_PATH = "unix:///run/habab-ipc/cc2531.sock";

    private UsbBridgeStatus() { }

    static void write(Path ipcDirectory, long updatedAtMillis, boolean connected,
            boolean permissionGranted, boolean bridgeReady, String status, String message)
            throws IOException {
        if (ipcDirectory == null) throw new IOException("USB status directory is unavailable");
        final byte[] contents;
        try {
            JSONObject snapshot = new JSONObject()
                    .put("schema", 1)
                    .put("updated_at", updatedAtMillis / 1000L)
                    .put("supported", true)
                    .put("connected", connected)
                    .put("permission_granted", permissionGranted)
                    .put("bridge_ready", bridgeReady)
                    .put("vid", 1105)
                    .put("pid", 5800)
                    .put("device_path", DEVICE_PATH)
                    .put("radio_type", "znp")
                    .put("status", status)
                    .put("message", message)
                    .put("product", "CC2531");
            contents = snapshot.toString().getBytes(StandardCharsets.UTF_8);
        } catch (JSONException error) {
            throw new IOException("Could not encode USB bridge status", error);
        }

        Files.createDirectories(ipcDirectory);
        VoiceStatusFile.write(ipcDirectory.resolve(FILE_NAME), contents);
    }
}
