package ru.habab.home;

import org.json.JSONObject;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public final class UsbBridgeStatusTest {
    @Rule public final TemporaryFolder temporaryFolder = new TemporaryFolder();

    @Test
    public void writesContractSnapshotAsAtomicJsonWithUnixSeconds() throws Exception {
        Path ipcDirectory = temporaryFolder.getRoot().toPath().resolve("files/ipc");
        Files.createDirectories(ipcDirectory);
        Path target = ipcDirectory.resolve("usb-status.json");
        Files.write(target, "stale snapshot".getBytes(StandardCharsets.UTF_8));

        UsbBridgeStatus.write(ipcDirectory, 1_798_123_456_789L,
                true, true, true, "bridge_ready", "USB-мост готов");

        JSONObject status = new JSONObject(new String(Files.readAllBytes(target), StandardCharsets.UTF_8));
        assertEquals(1, status.getInt("schema"));
        assertEquals(1_798_123_456L, status.getLong("updated_at"));
        assertTrue(status.getBoolean("supported"));
        assertTrue(status.getBoolean("connected"));
        assertTrue(status.getBoolean("permission_granted"));
        assertTrue(status.getBoolean("bridge_ready"));
        assertEquals(1105, status.getInt("vid"));
        assertEquals(5800, status.getInt("pid"));
        assertEquals("unix:///run/habab-ipc/cc2531.sock", status.getString("device_path"));
        assertEquals("znp", status.getString("radio_type"));
        assertEquals("bridge_ready", status.getString("status"));
        assertEquals("USB-мост готов", status.getString("message"));
        assertEquals("CC2531", status.getString("product"));
        assertFalse(status.has("firmware_verified"));
        try (java.util.stream.Stream<Path> files = Files.list(ipcDirectory)) {
            assertEquals(1L, files.count());
        }
    }

    @Test
    public void missingDeviceSnapshotClearsPermissionAndBridgeReadiness() throws Exception {
        Path ipcDirectory = temporaryFolder.getRoot().toPath().resolve("files/ipc");

        UsbBridgeStatus.write(ipcDirectory, 1_798_123_456_789L,
                false, false, false, "device_missing", "Поддерживаемое устройство не подключено");

        JSONObject status = new JSONObject(new String(Files.readAllBytes(
                ipcDirectory.resolve("usb-status.json")), StandardCharsets.UTF_8));
        assertFalse(status.getBoolean("connected"));
        assertFalse(status.getBoolean("permission_granted"));
        assertFalse(status.getBoolean("bridge_ready"));
        assertTrue(status.getBoolean("supported"));
    }
}
