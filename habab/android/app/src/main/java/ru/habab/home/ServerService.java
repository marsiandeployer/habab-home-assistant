package ru.habab.home;

import android.app.*;
import android.content.*;
import android.net.*;
import android.net.wifi.WifiManager;
import android.os.IBinder;
import android.os.PowerManager;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.TimeUnit;
import org.json.JSONObject;
import org.json.JSONArray;

public final class ServerService extends Service {
    static volatile boolean active;
    static volatile boolean stopping;
    static volatile String status = "stopped", tunnelStatus = "disabled";
    private volatile Process process;
    private Thread worker, tunnelThread;
    private Tunnel tunnel;
    private PowerManager.WakeLock wakeLock;
    private WifiManager.MulticastLock multicastLock;

    @Override public IBinder onBind(Intent intent) { return null; }

    @Override public void onCreate() {
        super.onCreate();
        NotificationManager manager = getSystemService(NotificationManager.class);
        manager.createNotificationChannel(new NotificationChannel("server", getString(R.string.server_channel), NotificationManager.IMPORTANCE_LOW));
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        startForeground(1, new Notification.Builder(this, "server").setSmallIcon(android.R.drawable.ic_menu_manage)
                .setContentTitle(getString(R.string.app_name)).setContentText(getString(R.string.server_starting))
                .setContentIntent(open).setOngoing(true).build());
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && "stop".equals(intent.getAction())) {
            getSharedPreferences("server", 0).edit().putBoolean("desired", false).apply();
            stopSelf(); return START_NOT_STICKY;
        }
        if (!getSharedPreferences("server", 0).getBoolean("desired", false)) { stopSelf(); return START_NOT_STICKY; }
        if (stopping || MaintenanceService.active) { stopSelf(); return START_NOT_STICKY; }
        if (worker != null) return START_STICKY;
        active = true; status = "starting";
        wakeLock = getSystemService(PowerManager.class).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "habab:server");
        wakeLock.acquire();
        WifiManager wifi = (WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE);
        if (wifi != null) { multicastLock = wifi.createMulticastLock("habab:discovery"); multicastLock.setReferenceCounted(false); multicastLock.acquire(); }
        worker = new Thread(this::supervise, "habab-core"); worker.start();
        return START_STICKY;
    }

    private List<String> command(RuntimeStore store) {
        File nativeDir = new File(getApplicationInfo().nativeLibraryDir);
        File temporary = new File(getFilesDir(), "tmp"); temporary.mkdirs();
        List<String> args = new ArrayList<>(Arrays.asList(new File(nativeDir, "libproot.so").toString(),
                "--kill-on-exit", "-0", "-r", store.root().toString(), "-b", "/dev", "-b", "/proc",
                "-b", temporary + ":/tmp", "-b", store.config() + ":/config",
                "-b", new File(getFilesDir(), "android-network.json") + ":/run/habab-network.json"));
        for (String path : new String[]{"/sys", "/system", "/vendor", "/apex"})
            if (new File(path).exists()) { args.add("-b"); args.add(path); }
        args.addAll(Arrays.asList("-w", "/config", "/usr/local/bin/python3", "-P", "/usr/local/share/habab-android/launch.py", "--config", "/config"));
        return args;
    }

    private void updateDns(RuntimeStore store) throws Exception {
        ConnectivityManager cm = getSystemService(ConnectivityManager.class);
        JSONArray interfaces = new JSONArray(); Set<String> seen = new HashSet<>();
        for (Network network : cm.getAllNetworks()) {
            LinkProperties links = cm.getLinkProperties(network);
            if (links == null || links.getInterfaceName() == null || !seen.add(links.getInterfaceName())) continue;
            String name = links.getInterfaceName(); int index = android.system.Os.if_nametoindex(name);
            if (index < 1) continue;
            JSONArray addresses = new JSONArray();
            for (LinkAddress address : links.getLinkAddresses()) addresses.put(new JSONObject()
                    .put("address", address.getAddress().getHostAddress().split("%")[0]).put("prefix", address.getPrefixLength()));
            interfaces.put(new JSONObject().put("name", name).put("index", index).put("addresses", addresses));
        }
        int loopback = android.system.Os.if_nametoindex("lo");
        if (loopback > 0) interfaces.put(new JSONObject().put("name", "lo").put("index", loopback).put("addresses", new JSONArray()
                .put(new JSONObject().put("address", "127.0.0.1").put("prefix", 8))
                .put(new JSONObject().put("address", "::1").put("prefix", 128))));
        JSONObject snapshot = new JSONObject().put("schema", 1).put("updatedAt", System.currentTimeMillis() / 1000).put("interfaces", interfaces);
        Path temporary = new File(getFilesDir(), "android-network.next").toPath();
        Files.write(temporary, snapshot.toString().getBytes(StandardCharsets.UTF_8));
        Files.move(temporary, new File(getFilesDir(), "android-network.json").toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
        LinkProperties properties = cm.getLinkProperties(cm.getActiveNetwork());
        if (properties == null || properties.getDnsServers().isEmpty()) return;
        StringBuilder dns = new StringBuilder();
        for (InetAddress address : properties.getDnsServers()) dns.append("nameserver ").append(address.getHostAddress().split("%")[0]).append('\n');
        Path file = new File(store.root(), "etc/resolv.conf").toPath();
        if (Files.isSymbolicLink(file)) Files.delete(file);
        byte[] bytes = dns.toString().getBytes(StandardCharsets.US_ASCII);
        if (!Files.exists(file) || !Arrays.equals(Files.readAllBytes(file), bytes)) Files.write(file, bytes);
    }

    static boolean healthy() {
        return healthyAt("http://127.0.0.1:8123/api/habab_home/status");
    }

    static boolean healthyAt(String endpoint) {
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL(endpoint).openConnection();
            connection.setConnectTimeout(1500); connection.setReadTimeout(10000);
            return connection.getResponseCode() == 401;
        } catch (IOException error) { return false; }
        finally { if (connection != null) connection.disconnect(); }
    }

    private void supervise() {
        RuntimeStore store = new RuntimeStore(this);
        File log = new File(getFilesDir(), "core.log");
        int failures = 0;
        try {
            if (!store.installed()) throw new IOException(getString(R.string.not_installed));
            // Do not adopt, stop or tunnel an unrelated Home Assistant on this port.
            try (java.net.Socket socket = new java.net.Socket()) {
                socket.connect(new InetSocketAddress("127.0.0.1", 8123), 500);
                throw new IllegalStateException("Port 8123 is already occupied");
            } catch (java.net.ConnectException | java.net.SocketTimeoutException available) { /* Free port. */ }
            store.seedConfig();
            while (active && failures < 5) {
                status = "starting";
                updateDns(store);
                if (log.length() > 8L * 1024 * 1024) Files.move(log.toPath(), new File(getFilesDir(), "core.previous.log").toPath(), StandardCopyOption.REPLACE_EXISTING);
                ProcessBuilder builder = new ProcessBuilder(command(store));
                Map<String, String> env = builder.environment(); env.clear();
                String nativeDir = getApplicationInfo().nativeLibraryDir;
                env.put("LD_LIBRARY_PATH", nativeDir);
                env.put("PROOT_LOADER", nativeDir + "/libproot-loader.so");
                env.put("PROOT_TMP_DIR", new File(getFilesDir(), "tmp").toString());
                env.put("PROOT_NO_SECCOMP", "1");
                env.put("HOME", "/root"); env.put("TMPDIR", "/tmp");
                env.put("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin");
                env.put("PYTHONPATH", "/usr/src/homeassistant"); env.put("LANG", "C.UTF-8");
                env.put("TZ", "Europe/Moscow"); env.put("SSL_CERT_FILE", "/etc/ssl/cert.pem");
                env.put("HABAB_OPENCODE_BINARY", "/opt/habab-opencode/opencode");
                env.put("HABAB_RUNTIME_KIND", "android_proot");
                JSONObject release = new JSONObject(new String(Files.readAllBytes(new File(store.root(), "etc/habab-android-release.json").toPath()), StandardCharsets.UTF_8));
                env.put("HABAB_OPENCODE_SHA256", release.getString("opencodeSha256"));
                env.put("PYTHONUNBUFFERED", "1");
                builder.redirectErrorStream(true).redirectOutput(ProcessBuilder.Redirect.appendTo(log));
                Process child = builder.start(); process = child;
                long started = System.currentTimeMillis(), networkUpdated = started; boolean ready = false;
                while (active && child.isAlive()) {
                    if (System.currentTimeMillis() - networkUpdated > 10000) { updateDns(store); networkUpdated = System.currentTimeMillis(); }
                    if (!ready && healthy()) {
                        ready = true; status = "running";
                        if (tunnelThread == null) {
                            tunnel = new Tunnel(this); tunnelThread = new Thread(tunnel::run, "habab-tunnel"); tunnelThread.start();
                        }
                    }
                    if (!ready && System.currentTimeMillis() - started > 300000) status = "startup-taking-longer-than-5-minutes";
                    Thread.sleep(2000);
                }
                if (!active) break;
                if (System.currentTimeMillis() - started > 300000 && ready) failures = 0;
                failures++;
                status = "restarting (" + failures + "/5)";
                Thread.sleep(Math.min(60000L, 5000L << (failures - 1)));
            }
            if (active) throw new IOException("Core exited repeatedly; inspect diagnostics");
        } catch (InterruptedException error) { Thread.currentThread().interrupt(); }
        catch (Exception error) {
            status = "error: " + error.getMessage();
            try (FileWriter out = new FileWriter(log, true)) { out.write("\nHabab launcher: " + error.getClass().getSimpleName() + ": " + error.getMessage() + "\n"); }
            catch (IOException ignored) { /* Status remains visible. */ }
            getSharedPreferences("server", 0).edit().putBoolean("desired", false).apply();
        } finally { stopSelf(); }
    }

    @Override public void onDestroy() {
        stopping = true;
        active = false;
        if (tunnel != null) tunnel.close();
        tunnelStatus = "disabled";
        if (tunnelThread != null) tunnelThread.interrupt();
        if (worker != null) worker.interrupt();
        Process child = process;
        if (child != null) {
            child.destroy();
            new Thread(() -> {
                try {
                    if (!child.waitFor(8, TimeUnit.SECONDS)) { child.destroyForcibly(); child.waitFor(); }
                } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
                finally { stopping = false; if (!status.startsWith("error:")) status = "stopped"; }
            }, "habab-stop").start();
        }
        else stopping = false;
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        if (multicastLock != null && multicastLock.isHeld()) multicastLock.release();
        if (!status.startsWith("error:")) status = stopping ? "stopping" : "stopped";
        stopForeground(true); super.onDestroy();
    }
}
