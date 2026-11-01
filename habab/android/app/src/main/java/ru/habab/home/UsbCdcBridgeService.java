package ru.habab.home;

import android.annotation.SuppressLint;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.hardware.usb.UsbConstants;
import android.hardware.usb.UsbDevice;
import android.hardware.usb.UsbDeviceConnection;
import android.hardware.usb.UsbEndpoint;
import android.hardware.usb.UsbInterface;
import android.hardware.usb.UsbManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.Parcelable;
import android.os.PowerManager;
import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.net.LocalSocketAddress;
import android.system.ErrnoException;
import android.system.Os;
import android.system.OsConstants;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.File;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
import java.util.UUID;

/**
 * CC2531 USB CDC ACM to app-private ZHA unix:// byte bridge.
 *
 * The filesystem Unix socket lives in app-private storage and is bind-mounted into the PRoot guest.
 * No ZNP commands are generated here; bytes are forwarded unchanged.
 */
public final class UsbCdcBridgeService extends Service {
    static final String ACTION_WITH_SERVER = "ru.habab.home.USB_CDC_WITH_SERVER";
    public static final String ACTION_STOP = "ru.habab.home.USB_CDC_BRIDGE_STOP";
    private static final String SOCKET_NAME = "cc2531.sock";

    private static final String CHANNEL_ID = "usb_cdc_bridge";
    private static final int NOTIFICATION_ID = 44;
    private static final int TI_VENDOR_ID = 0x0451;
    private static final int CC2531_PRODUCT_ID = 0x16a8;
    private static final String PERMISSION_ACTION = "ru.habab.home.USB_CDC_PERMISSION";
    private static final String EXTRA_PERMISSION_TOKEN = "ru.habab.home.USB_CDC_PERMISSION_TOKEN";
    private static final int USB_TIMEOUT_MS = 300;

    private final Handler main = new Handler(Looper.getMainLooper());
    private final AtomicBoolean running = new AtomicBoolean();
    private final AtomicBoolean reconnectPending = new AtomicBoolean();
    private final Object connectLock = new Object();
    private volatile UsbManager usbManager;
    private volatile BridgeSession bridge;
    private volatile UsbDeviceConnection openingConnection;
    private volatile PendingIntent pendingPermissionIntent;
    private volatile String connectingDeviceName;
    private volatile String permissionDeviceName;
    private volatile String permissionToken;
    private volatile boolean connecting;
    private volatile boolean permissionRequested;
    static volatile String status = "";
    static volatile boolean active;
    private volatile String statusCode = "absent";
    private volatile boolean enabled;
    private String observedDeviceName;

    static void startWithServer(Context context) {
        context.startForegroundService(new Intent(context, UsbCdcBridgeService.class)
                .setAction(ACTION_WITH_SERVER));
    }

    private final Runnable heartbeat = new Runnable() {
        @Override public void run() {
            if (!running.get()) return;
            UsbDevice device = findSupportedDevice();
            String name = device == null ? null : device.getDeviceName();
            BridgeSession current = bridge;
            if (current != null && !current.closed.get() && !deviceUsable(current.device)) {
                current.close();
                updateStatus(device == null ? R.string.usb_bridge_state_device_detached
                        : R.string.usb_bridge_state_error_permission);
            }
            boolean changed = !java.util.Objects.equals(observedDeviceName, name);
            observedDeviceName = name;
            if (device == null && enabled) {
                clearPermissionRequest();
                updateStatus(R.string.usb_bridge_state_waiting_supported);
            } else if (changed && enabled) {
                tryConnect();
            }
            publishSnapshot();
            main.postDelayed(this, 5000);
        }
    };
    private PowerManager.WakeLock wakeLock;
    private boolean receiverRegistered;

    private final BroadcastReceiver usbReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            String action = intent.getAction();
            UsbDevice device = usbDeviceFrom(intent);
            if (PERMISSION_ACTION.equals(action)) {
                String resultToken = intent.getStringExtra(EXTRA_PERMISSION_TOKEN);
                boolean expected = UsbBridgeLifecycle.matchesPermissionResult(
                        permissionRequested, running.get(), permissionDeviceName,
                        device == null ? null : device.getDeviceName(), permissionToken, resultToken);
                if (!expected) return;
                permissionRequested = false;
                permissionDeviceName = null;
                permissionToken = null;
                cancelPendingPermission();
                boolean granted = intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false);
                if (granted && running.get()) {
                    updateStatus(R.string.usb_bridge_state_permission_granted);
                    tryConnect();
                } else {
                    updateStatus(R.string.usb_bridge_state_permission_denied);
                }
                return;
            }
            if (UsbManager.ACTION_USB_DEVICE_DETACHED.equals(action) && isSupportedDevice(device)) {
                String detachedName = device == null ? null : device.getDeviceName();
                if (UsbBridgeLifecycle.matchesPendingDevice(permissionDeviceName, detachedName)) {
                    permissionRequested = false;
                    permissionDeviceName = null;
                    permissionToken = null;
                    cancelPendingPermission();
                    updateStatus(R.string.usb_bridge_state_permission_cancelled);
                }
                BridgeSession current = bridge;
                if (current != null && current.device.getDeviceName().equals(detachedName)) {
                    current.close();
                    updateStatus(R.string.usb_bridge_state_device_detached);
                }
                if (detachedName != null && detachedName.equals(connectingDeviceName)) {
                    UsbDeviceConnection opening = openingConnection;
                    if (opening != null) opening.close();
                    updateStatus(R.string.usb_bridge_state_opening_detached);
                }
                return;
            }
            if (UsbManager.ACTION_USB_DEVICE_ATTACHED.equals(action) && enabled && isSupportedDevice(device)) {
                reconnectPending.set(true);
                updateStatus(R.string.usb_bridge_state_device_attached);
                main.postDelayed(UsbCdcBridgeService.this::tryConnect, 500);
            }
        }
    };

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    @SuppressLint("UnspecifiedRegisterReceiverFlag") // The flagged overload exists only on API 33+.
    public void onCreate() {
        super.onCreate();
        usbManager = (UsbManager) getSystemService(Context.USB_SERVICE);
        createNotificationChannel();
        IntentFilter filter = new IntentFilter();
        filter.addAction(PERMISSION_ACTION);
        filter.addAction(UsbManager.ACTION_USB_DEVICE_ATTACHED);
        filter.addAction(UsbManager.ACTION_USB_DEVICE_DETACHED);
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(usbReceiver, filter, Context.RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(usbReceiver, filter);
        }
        receiverRegistered = true;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        boolean stop = intent != null && ACTION_STOP.equals(intent.getAction());
        boolean automatic = intent == null || ACTION_WITH_SERVER.equals(intent.getAction());
        enabled = !stop && (automatic
                ? getSharedPreferences(UsbBridgeLifecycle.PREFERENCES, MODE_PRIVATE)
                        .getBoolean(UsbBridgeLifecycle.ENABLED, true)
                : true);
        if (!automatic) getSharedPreferences(UsbBridgeLifecycle.PREFERENCES, MODE_PRIVATE).edit()
                .putBoolean(UsbBridgeLifecycle.ENABLED, enabled).apply();
        if (stop && !ServerService.active) {
            updateStatus(R.string.usb_bridge_state_stopped);
            stopSelf(startId);
            return START_NOT_STICKY;
        }
        if (running.compareAndSet(false, true)) {
            active = true;
            status = getString(enabled ? R.string.usb_bridge_state_waiting : R.string.usb_bridge_state_stopped);
            statusCode = enabled ? "absent" : "disabled";
            startForeground(NOTIFICATION_ID, notification());
            PowerManager power = getSystemService(PowerManager.class);
            wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "Habab:UsbCdcBridge");
            wakeLock.setReferenceCounted(false);
            main.post(heartbeat);
        }
        if (enabled) {
            tryConnect();
        } else {
            clearPermissionRequest();
            synchronized (connectLock) {
                if (bridge != null) bridge.close();
                if (openingConnection != null) openingConnection.close();
            }
            updateStatus(R.string.usb_bridge_state_stopped);
        }
        return START_STICKY;
    }

    private void tryConnect() {
        synchronized (connectLock) {
            if (!running.get() || !enabled || connecting || bridge != null || permissionRequested) return;
            UsbDevice device = findSupportedDevice();
            if (device == null) {
                updateStatus(R.string.usb_bridge_state_waiting_supported);
                return;
            }
            reconnectPending.set(false);
            if (!hasUsbPermission(device)) {
                permissionRequested = true;
                permissionDeviceName = device.getDeviceName();
                String token = UUID.randomUUID().toString();
                permissionToken = token;
                updateStatus(R.string.usb_bridge_state_permission_request);
                int permissionFlags = PendingIntent.FLAG_CANCEL_CURRENT;
                if (Build.VERSION.SDK_INT >= 31) permissionFlags |= PendingIntent.FLAG_MUTABLE;
                pendingPermissionIntent = PendingIntent.getBroadcast(
                        this,
                        token.hashCode(),
                        new Intent(PERMISSION_ACTION).setPackage(getPackageName())
                                .putExtra(EXTRA_PERMISSION_TOKEN, token),
                        permissionFlags);
                try {
                    usbManager.requestPermission(device, pendingPermissionIntent);
                } catch (RuntimeException error) {
                    clearPermissionRequest();
                    updateStatus(R.string.usb_bridge_state_error_permission);
                }
                return;
            }
            connecting = true;
            connectingDeviceName = device.getDeviceName();
            Thread thread = new Thread(() -> connectAndServe(device), "UsbCdcBridge");
            thread.start();
        }
    }

    private UsbDevice findSupportedDevice() {
        if (usbManager == null) return null;
        for (UsbDevice device : usbManager.getDeviceList().values()) {
            if (isSupportedDevice(device)) return device;
        }
        return null;
    }

    private boolean isSupportedDevice(UsbDevice device) {
        return device != null && device.getVendorId() == TI_VENDOR_ID
                && device.getProductId() == CC2531_PRODUCT_ID;
    }

    private void connectAndServe(UsbDevice device) {
        UsbDeviceConnection connection = null;
        LocalSocket boundSocket = null;
        LocalServerSocket server = null;
        File socketPath = null;
        BridgeSession opened = null;
        boolean claimedControl = false;
        boolean claimedData = false;
        boolean listenerReady = false;
        boolean failureReported = false;
        int failureState = R.string.usb_bridge_state_error;
        UsbInterface control = null;
        UsbInterface data = null;
        try {
            failureState = R.string.usb_bridge_state_error_permission;
            if (!running.get() || !hasUsbPermission(device)) {
                throw new IOException("USB permission is unavailable");
            }
            failureState = R.string.usb_bridge_state_error_open;
            connection = usbManager.openDevice(device);
            if (connection == null) throw new IOException("Android could not open the USB device");
            openingConnection = connection;

            failureState = R.string.usb_bridge_state_error_descriptors;
            CdcInterfaces cdc = findCdcInterfaces(device, connection);
            control = cdc.control;
            data = cdc.data;
            if (!isStillAttached(device)) throw new IOException("USB device detached during setup");
            failureState = R.string.usb_bridge_state_error_interface;
            if (!connection.claimInterface(control, true)) throw new IOException("Could not claim CDC control interface");
            claimedControl = true;
            if (!connection.claimInterface(data, true)) throw new IOException("Could not claim CDC data interface");
            claimedData = true;
            failureState = R.string.usb_bridge_state_error_line_coding;
            configureLineCoding(connection, control.getId());

            failureState = R.string.usb_bridge_state_error_ipc;
            File socketDirectory = new File(getFilesDir(), "ipc");
            if (!socketDirectory.isDirectory() && !socketDirectory.mkdirs()) {
                throw new IOException("Could not create app-private IPC directory");
            }
            Os.chmod(socketDirectory.getAbsolutePath(), 0700);
            socketPath = new File(socketDirectory, SOCKET_NAME);
            removeStaleSocket(socketPath);
            if (socketPath.getAbsolutePath().getBytes(java.nio.charset.StandardCharsets.UTF_8).length >= 108) {
                throw new IOException("App-private Unix socket path exceeds sun_path limit");
            }
            boundSocket = new LocalSocket();
            boundSocket.bind(new LocalSocketAddress(socketPath.getAbsolutePath(),
                    LocalSocketAddress.Namespace.FILESYSTEM));
            Os.chmod(socketPath.getAbsolutePath(), 0600);
            server = new LocalServerSocket(boundSocket.getFileDescriptor());
            opened = new BridgeSession(device, connection, control, data,
                    cdc.bulkIn, cdc.bulkOut, boundSocket, server, socketPath);
            connection = null;
            boundSocket = null;
            server = null;
            claimedControl = false;
            claimedData = false;
            synchronized (connectLock) {
                if (!running.get() || !enabled) throw new IOException("USB bridge stopped during startup");
                bridge = opened;
                acquireWakeLock();
                openingConnection = null;
            }
            if (!isStillAttached(device)) throw new IOException("USB coordinator detached during startup");
            listenerReady = true;
            updateStatus(R.string.usb_bridge_state_ready);
            failureState = R.string.usb_bridge_state_error_transport;
            serveClients(opened);
        } catch (Exception ignored) {
            failureReported = true;
            if (running.get() && enabled) {
                int state = isStillAttached(device) ? failureState
                        : R.string.usb_bridge_state_device_detached;
                updateStatus(state);
            }
        } finally {
            openingConnection = null;
            if (opened != null) {
                opened.close();
                if (bridge == opened) bridge = null;
                releaseWakeLock();
            } else {
                if (server != null) {
                    try { server.close(); } catch (IOException ignored) { }
                }
                if (boundSocket != null) {
                    try { boundSocket.close(); } catch (IOException ignored) { }
                }
                if (socketPath != null) {
                    try { removeStaleSocket(socketPath); } catch (IOException ignored) { }
                }
                if (connection != null) {
                    if (claimedData) connection.releaseInterface(data);
                    if (claimedControl) connection.releaseInterface(control);
                    connection.close();
                }
            }
            synchronized (connectLock) {
                connecting = false;
                connectingDeviceName = null;
            }
            if (running.get() && enabled && reconnectPending.getAndSet(false)) {
                main.postDelayed(this::tryConnect, 500);
            }
            if (running.get() && enabled && listenerReady && !failureReported && bridge == null) {
                updateStatus(R.string.usb_bridge_state_ended);
            }
        }
    }

    private void serveClients(BridgeSession current) throws IOException, InterruptedException {
        while (running.get() && enabled && !current.closed.get()) {
            LocalSocket client = current.server.accept();
            if (!running.get() || current.closed.get()) {
                closeQuietly(client);
                return;
            }
            current.client.set(client);
            updateStatus(R.string.usb_bridge_state_client_connected);
            AtomicBoolean clientRunning = new AtomicBoolean(true);
            CountDownLatch ended = new CountDownLatch(2);
            AtomicReference<IOException> failure = new AtomicReference<>();
            Thread usbToSocket = new Thread(() -> {
                try {
                    UsbByteRelay.copy(
                            buffer -> current.connection.bulkTransfer(current.bulkIn, buffer, 0,
                                    buffer.length, USB_TIMEOUT_MS),
                            () -> running.get() && enabled && clientRunning.get() && !current.closed.get(),
                            () -> deviceUsable(current.device), client.getOutputStream(), Thread::sleep);
                } catch (UsbByteRelay.PeerClosedException ignored) {
                    // ZHA probing may close its socket while USB data arrives. Keep the USB listener.
                } catch (IOException error) {
                    if (clientRunning.get() && running.get() && enabled && !current.closed.get()) {
                        failure.compareAndSet(null, error);
                    }
                } catch (InterruptedException error) {
                    Thread.currentThread().interrupt();
                } finally {
                    clientRunning.set(false);
                    ended.countDown();
                    closeQuietly(client);
                }
            }, "UsbCdcToSocket");
            Thread socketToUsb = new Thread(() -> {
                try {
                    byte[] buffer = new byte[4096];
                    InputStream input = client.getInputStream();
                    while (running.get() && enabled && clientRunning.get() && !current.closed.get()) {
                        int count = input.read(buffer);
                        if (count < 0) break;
                        int offset = 0;
                        while (offset < count && running.get() && enabled && clientRunning.get() && !current.closed.get()) {
                            int written = current.connection.bulkTransfer(
                                    current.bulkOut, buffer, offset, count - offset, 1000);
                            if (written <= 0) {
                                if (!isStillAttached(current.device)) throw new IOException("USB coordinator disconnected");
                                failure.compareAndSet(null, new IOException("USB CDC write failed"));
                                throw new IOException("USB CDC write failed");
                            }
                            offset += written;
                        }
                    }
                } catch (IOException error) {
                    if (clientRunning.get() && !isStillAttached(current.device)) {
                        failure.compareAndSet(null, new IOException("USB coordinator disconnected", error));
                    }
                } finally {
                    clientRunning.set(false);
                    ended.countDown();
                    closeQuietly(client);
                }
            }, "SocketToUsbCdc");

            usbToSocket.start();
            socketToUsb.start();
            ended.await();
            clientRunning.set(false);
            closeQuietly(client);
            usbToSocket.join(1500);
            socketToUsb.join(1500);
            current.client.compareAndSet(client, null);
            if (failure.get() != null) {
                throw failure.get();
            }
            if (!isStillAttached(current.device)) throw new IOException("USB coordinator disconnected");
            if (running.get() && !current.closed.get()) {
                updateStatus(R.string.usb_bridge_state_client_wait);
            }
        }
    }

    private CdcInterfaces findCdcInterfaces(UsbDevice device, UsbDeviceConnection connection) throws IOException {
        byte[] header = new byte[9];
        int headerBytes = connection.controlTransfer(0x80, 0x06, 0x0200, 0, header, header.length, 1000);
        if (headerBytes < header.length || (header[1] & 0xff) != 0x02) {
            throw new IOException("USB configuration descriptor is unavailable");
        }
        int totalLength = (header[2] & 0xff) | ((header[3] & 0xff) << 8);
        if (totalLength < 9 || totalLength > 4096) throw new IOException("Invalid USB configuration descriptor length");
        byte[] descriptors = new byte[totalLength];
        int descriptorBytes = connection.controlTransfer(0x80, 0x06, 0x0200, 0,
                descriptors, descriptors.length, 1000);
        if (descriptorBytes < totalLength) throw new IOException("Incomplete USB configuration descriptor");

        Map<Integer, UsbInterface> interfaces = new HashMap<>();
        List<CdcUnionParser.InterfaceInfo> interfaceInfo = new ArrayList<>();
        for (int i = 0; i < device.getInterfaceCount(); i++) {
            UsbInterface usbInterface = device.getInterface(i);
            if (usbInterface.getAlternateSetting() == 0
                    && interfaces.put(usbInterface.getId(), usbInterface) != null) {
                throw new IOException("Duplicate USB interface alternate setting zero");
            }
            boolean hasBulkIn = false;
            boolean hasBulkOut = false;
            for (int endpointIndex = 0; endpointIndex < usbInterface.getEndpointCount(); endpointIndex++) {
                UsbEndpoint endpoint = usbInterface.getEndpoint(endpointIndex);
                if (endpoint.getType() != UsbConstants.USB_ENDPOINT_XFER_BULK) continue;
                if (endpoint.getDirection() == UsbConstants.USB_DIR_IN) hasBulkIn = true;
                if (endpoint.getDirection() == UsbConstants.USB_DIR_OUT) hasBulkOut = true;
            }
            interfaceInfo.add(new CdcUnionParser.InterfaceInfo(usbInterface.getId(),
                    usbInterface.getInterfaceClass(), usbInterface.getInterfaceSubclass(),
                    usbInterface.getAlternateSetting(), hasBulkIn, hasBulkOut));
        }
        List<CdcUnionParser.Union> unions = CdcUnionParser.parse(descriptors);
        CdcUnionParser.InterfacePair pair = CdcUnionParser.selectInterfaces(unions, interfaceInfo);
        UsbInterface control = interfaces.get(pair.controlId);
        UsbInterface data = interfaces.get(pair.dataId);
        UsbEndpoint bulkIn = null;
        UsbEndpoint bulkOut = null;
        for (int endpointIndex = 0; endpointIndex < data.getEndpointCount(); endpointIndex++) {
            UsbEndpoint endpoint = data.getEndpoint(endpointIndex);
            if (endpoint.getType() != UsbConstants.USB_ENDPOINT_XFER_BULK) continue;
            if (endpoint.getDirection() == UsbConstants.USB_DIR_IN) bulkIn = endpoint;
            if (endpoint.getDirection() == UsbConstants.USB_DIR_OUT) bulkOut = endpoint;
        }
        return new CdcInterfaces(control, data, bulkIn, bulkOut);
    }

    private void configureLineCoding(UsbDeviceConnection connection, int controlInterfaceId) throws IOException {
        // CDC SET_LINE_CODING: 115200 baud, one stop bit, no parity, eight data bits.
        byte[] coding = new byte[] {(byte) 0x00, (byte) 0xc2, 0x01, 0x00, 0x00, 0x00, 0x08};
        int configured = connection.controlTransfer(0x21, 0x20, 0, controlInterfaceId,
                coding, coding.length, 1000);
        if (configured != coding.length) throw new IOException("CDC line coding request failed");
        // Assert DTR | RTS once for normal CDC operation; never pulse lines or send reset/ZNP commands.
        if (connection.controlTransfer(0x21, 0x22, 0x0003, controlInterfaceId,
                null, 0, 1000) < 0) {
            throw new IOException("CDC control line request failed");
        }
    }

    private static void removeStaleSocket(File path) throws IOException {
        try {
            int type = Os.lstat(path.getAbsolutePath()).st_mode & OsConstants.S_IFMT;
            if (type != OsConstants.S_IFSOCK) {
                throw new IOException("Refusing to remove a non-socket at the bridge path");
            }
            if (!path.delete()) throw new IOException("Could not remove stale bridge socket");
        } catch (ErrnoException error) {
            if (error.errno != OsConstants.ENOENT) {
                throw new IOException("Could not clean stale bridge socket", error);
            }
        }
    }

    private boolean isStillAttached(UsbDevice device) {
        if (usbManager == null) return false;
        return usbManager.getDeviceList().containsKey(device.getDeviceName());
    }

    private UsbDevice usbDeviceFrom(Intent intent) {
        Parcelable value = intent.getParcelableExtra(UsbManager.EXTRA_DEVICE);
        return value instanceof UsbDevice ? (UsbDevice) value : null;
    }

    private boolean hasUsbPermission(UsbDevice device) {
        try { return usbManager != null && usbManager.hasPermission(device); }
        catch (RuntimeException error) { return false; }
    }

    private boolean deviceUsable(UsbDevice device) {
        return isStillAttached(device) && hasUsbPermission(device);
    }

    private synchronized void publishSnapshot() {
        UsbDevice device = findSupportedDevice();
        boolean permitted = device != null && hasUsbPermission(device);
        BridgeSession current = bridge;
        boolean ready = running.get() && enabled && permitted && current != null
                && !current.closed.get() && current.device.getDeviceName().equals(device.getDeviceName());
        try {
            File directory = new File(getFilesDir(), "ipc");
            if (!directory.isDirectory() && !directory.mkdirs()) throw new IOException("IPC directory unavailable");
            Os.chmod(directory.getAbsolutePath(), 0700);
            UsbBridgeStatus.write(directory.toPath(), System.currentTimeMillis(), device != null,
                    permitted, ready, statusCode, status);
        } catch (IOException | ErrnoException error) {
            android.util.Log.w("HababUsbBridge", "status_write_failed: " + error.getClass().getSimpleName());
        }
    }

    private synchronized void updateStatus(int resource) {
        if ((!running.get() || !enabled) && resource != R.string.usb_bridge_state_stopped) return;
        status = getString(resource);
        statusCode = machineStatus(resource);
        publishSnapshot();
        main.post(() -> {
            if (running.get()) {
                NotificationManager notifications = getSystemService(NotificationManager.class);
                notifications.notify(NOTIFICATION_ID, notification());
            }
        });
    }

    private static String machineStatus(int resource) {
        if (resource == R.string.usb_bridge_state_stopped) return "disabled";
        if (resource == R.string.usb_bridge_state_waiting || resource == R.string.usb_bridge_state_waiting_supported) return "absent";
        if (resource == R.string.usb_bridge_state_permission_request) return "permission_required";
        if (resource == R.string.usb_bridge_state_permission_denied || resource == R.string.usb_bridge_state_error_permission) return "permission_denied";
        if (resource == R.string.usb_bridge_state_ready || resource == R.string.usb_bridge_state_client_connected || resource == R.string.usb_bridge_state_client_wait) return "ready";
        if (resource == R.string.usb_bridge_state_device_detached || resource == R.string.usb_bridge_state_permission_cancelled || resource == R.string.usb_bridge_state_opening_detached) return "detached";
        if (resource == R.string.usb_bridge_state_error_open) return "open_failed";
        if (resource == R.string.usb_bridge_state_error_descriptors) return "unsupported_cdc";
        if (resource == R.string.usb_bridge_state_error_interface) return "interface_busy";
        if (resource == R.string.usb_bridge_state_error_line_coding) return "line_coding_failed";
        if (resource == R.string.usb_bridge_state_error_ipc) return "ipc_failed";
        if (resource == R.string.usb_bridge_state_error_transport) return "transport_error";
        if (resource == R.string.usb_bridge_state_ended) return "disconnected";
        if (resource == R.string.usb_bridge_state_error) return "error";
        return "connecting";
    }

    private void clearPermissionRequest() {
        permissionRequested = false;
        permissionDeviceName = null;
        permissionToken = null;
        cancelPendingPermission();
    }

    private void cancelPendingPermission() {
        PendingIntent pending = pendingPermissionIntent;
        pendingPermissionIntent = null;
        if (pending != null) pending.cancel();
    }

    @SuppressLint("WakelockTimeout") // Held only for the active foreground USB bridge and released on shutdown.
    private synchronized void acquireWakeLock() {
        if (wakeLock != null && !wakeLock.isHeld()) wakeLock.acquire();
    }

    private synchronized void releaseWakeLock() {
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
    }

    private Notification notification() {
        Intent stop = new Intent(this, UsbCdcBridgeService.class).setAction(ACTION_STOP);
        PendingIntent stopIntent = PendingIntent.getService(this, 1, stop,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CHANNEL_ID)
                .setSmallIcon(android.R.drawable.ic_menu_manage)
                .setContentTitle(getString(R.string.usb_bridge_notification_title))
                .setContentText(status)
                .setStyle(new Notification.BigTextStyle().bigText(status))
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .addAction(new Notification.Action.Builder(null,
                        getString(R.string.usb_bridge_notification_stop), stopIntent).build())
                .build();
    }

    private void createNotificationChannel() {
        NotificationManager notifications = getSystemService(NotificationManager.class);
        notifications.createNotificationChannel(new NotificationChannel(
                CHANNEL_ID, getString(R.string.usb_bridge_notification_channel), NotificationManager.IMPORTANCE_LOW));
    }

    private static void closeQuietly(LocalSocket socket) {
        if (socket == null) return;
        try { socket.close(); } catch (IOException ignored) { }
    }

    @Override
    public void onDestroy() {
        running.set(false);
        active = false;
        main.removeCallbacks(heartbeat);
        status = getString(R.string.usb_bridge_state_stopped);
        statusCode = "stopped";
        permissionRequested = false;
        permissionDeviceName = null;
        permissionToken = null;
        cancelPendingPermission();
        if (receiverRegistered) {
            unregisterReceiver(usbReceiver);
            receiverRegistered = false;
        }
        synchronized (connectLock) {
            BridgeSession current = bridge;
            if (current != null) current.close();
            UsbDeviceConnection opening = openingConnection;
            if (opening != null) opening.close();
        }
        releaseWakeLock();
        publishSnapshot();
        stopForeground(true);
        super.onDestroy();
    }

    private static final class CdcInterfaces {
        final UsbInterface control;
        final UsbInterface data;
        final UsbEndpoint bulkIn;
        final UsbEndpoint bulkOut;

        CdcInterfaces(UsbInterface control, UsbInterface data, UsbEndpoint bulkIn, UsbEndpoint bulkOut) {
            this.control = control;
            this.data = data;
            this.bulkIn = bulkIn;
            this.bulkOut = bulkOut;
        }
    }

    private static final class BridgeSession implements AutoCloseable {
        final UsbDevice device;
        final UsbDeviceConnection connection;
        final UsbInterface control;
        final UsbInterface data;
        final UsbEndpoint bulkIn;
        final UsbEndpoint bulkOut;
        final LocalSocket boundSocket;
        final LocalServerSocket server;
        final File socketPath;
        final AtomicBoolean closed = new AtomicBoolean();
        final AtomicReference<LocalSocket> client = new AtomicReference<>();

        BridgeSession(UsbDevice device, UsbDeviceConnection connection, UsbInterface control,
                UsbInterface data, UsbEndpoint bulkIn, UsbEndpoint bulkOut,
                LocalSocket boundSocket, LocalServerSocket server, File socketPath) {
            this.device = device;
            this.connection = connection;
            this.control = control;
            this.data = data;
            this.bulkIn = bulkIn;
            this.bulkOut = bulkOut;
            this.boundSocket = boundSocket;
            this.server = server;
            this.socketPath = socketPath;
        }

        @Override
        public void close() {
            if (!closed.compareAndSet(false, true)) return;
            closeQuietly(client.getAndSet(null));
            LocalSocket wake = new LocalSocket();
            try {
                wake.connect(new LocalSocketAddress(socketPath.getAbsolutePath(),
                        LocalSocketAddress.Namespace.FILESYSTEM));
            } catch (IOException ignored) {
            } finally {
                closeQuietly(wake);
            }
            try { server.close(); } catch (IOException ignored) { }
            try { boundSocket.close(); } catch (IOException ignored) { }
            try { removeStaleSocket(socketPath); } catch (IOException ignored) { }
            try { connection.releaseInterface(data); } catch (RuntimeException ignored) { }
            try { connection.releaseInterface(control); } catch (RuntimeException ignored) { }
            connection.close();
        }
    }
}
