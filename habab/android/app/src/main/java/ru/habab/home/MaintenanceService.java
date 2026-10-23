package ru.habab.home;

import android.app.*;
import android.content.*;
import android.os.IBinder;
import android.os.PowerManager;
import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import org.json.JSONObject;

/** Long downloads/extraction/backups remain foreground work with the screen off. */
public final class MaintenanceService extends Service {
    interface Work { void run() throws Exception; }
    static volatile boolean active;
    static volatile String status = "";
    private static Work pending;
    private Thread worker;
    private PowerManager.WakeLock wakeLock;

    static synchronized void start(Context context, Work work) {
        if (active || ServerService.active || ServerService.stopping) throw new IllegalStateException("Server or maintenance is active");
        active = true; status = context.getString(R.string.working); pending = work;
        try { context.startForegroundService(new Intent(context, MaintenanceService.class)); }
        catch (RuntimeException error) { active = false; pending = null; throw error; }
    }

    @Override public IBinder onBind(Intent intent) { return null; }
    @Override public void onCreate() {
        super.onCreate();
        NotificationManager manager = getSystemService(NotificationManager.class);
        manager.createNotificationChannel(new NotificationChannel("maintenance", getString(R.string.install_title), NotificationManager.IMPORTANCE_LOW));
        PendingIntent open = PendingIntent.getActivity(this, 2, new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        startForeground(2, new Notification.Builder(this, "maintenance").setSmallIcon(android.R.drawable.stat_sys_download)
                .setContentTitle(getString(R.string.app_name)).setContentText(getString(R.string.working))
                .setContentIntent(open).setOngoing(true).build());
        wakeLock = getSystemService(PowerManager.class).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "habab:maintenance");
        wakeLock.acquire();
    }
    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (worker != null) return START_NOT_STICKY;
        Work work;
        synchronized (MaintenanceService.class) { work = pending; pending = null; }
        if (work == null) { status = "Operation interrupted; please retry"; stopSelf(); return START_NOT_STICKY; }
        worker = new Thread(() -> {
            try { work.run(); status = getString(R.string.done); }
            catch (Exception error) { status = getString(R.string.error, error.getMessage()); }
            finally {
                try {
                    JSONObject result = new JSONObject().put("status", status).put("recordedAt", System.currentTimeMillis());
                    Files.write(new File(getFilesDir(), "maintenance-status.json").toPath(), result.toString().getBytes(StandardCharsets.UTF_8));
                } catch (Exception ignored) { /* The original operation result remains visible in the UI. */ }
                stopSelf();
            }
        }, "habab-maintenance");
        worker.start(); return START_NOT_STICKY;
    }
    @Override public void onDestroy() {
        if (worker != null) worker.interrupt();
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        active = false; stopForeground(true); super.onDestroy();
    }
}
