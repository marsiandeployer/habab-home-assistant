package ru.habab.home;

import android.content.*;

public final class BootReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context context, Intent intent) {
        if (Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction())
                && context.getSharedPreferences("server", 0).getBoolean("desired", false))
            context.startForegroundService(new Intent(context, ServerService.class));
        if (Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction())
                && context.getSharedPreferences("voice", 0).getBoolean("enabled", false))
            context.startForegroundService(new Intent(context, VoiceService.class));
    }
}
