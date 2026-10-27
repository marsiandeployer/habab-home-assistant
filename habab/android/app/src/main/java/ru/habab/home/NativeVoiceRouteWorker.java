package ru.habab.home;

import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.Manifest;
import android.media.AudioManager;
import android.os.SystemClock;
import org.json.JSONObject;

/** Owner-authenticated HA poll/ack bridge; the ServerService owns its lifetime. */
final class NativeVoiceRouteWorker implements Runnable {
    private static final String ID = "voice_route_command_id";
    private static final String TARGET = "voice_route_command_target";
    private static final String PREVIOUS = "voice_route_command_previous";
    private static final String PREVIOUS_WAKE = "voice_route_command_previous_wake";
    private static final String STARTED = "voice_route_command_started";
    private static final String ACK_RESULT = "voice_route_ack_result";
    private static final String ACK_ERROR = "voice_route_ack_error";
    private static final long POLL_INTERVAL_MS = 1800;
    private final Context context;

    NativeVoiceRouteWorker(Context context) { this.context = context.getApplicationContext(); }

    private SharedPreferences voice() { return context.getSharedPreferences("voice", 0); }

    @Override public void run() {
        try (VoiceCredentials credentials = new VoiceCredentials(context)) {
            while (ServerService.active && !Thread.currentThread().isInterrupted()) {
                if (!credentials.configured()) {
                    SystemClock.sleep(2500);
                    continue;
                }
                try {
                    JSONObject client = clientState();
                    JSONObject request = new JSONObject().put("action", "poll").put("client", client);
                    JSONObject ack = pendingAck(client);
                    if (ack != null) request.put("ack", ack);
                    JSONObject result = credentials.call("native_voice_route", request);
                    if (ack != null && result.optBoolean("ack_recorded")) onAckRecorded();
                    else if (ack != null && result.optBoolean("ack_unknown"))
                        onAckUnknown(ack.optString("id", ""));
                    JSONObject command = result.optJSONObject("command");
                    if (command != null) applyCommand(command);
                } catch (Exception ignored) {
                    // A local control bridge outage must not affect the HA or voice service.
                }
                SystemClock.sleep(POLL_INTERVAL_MS);
            }
        } catch (Exception ignored) { /* Missing voice credentials disables only this bridge. */ }
    }

    private JSONObject clientState() throws Exception {
        SharedPreferences preferences = voice();
        boolean wakeEnabled = preferences.getBoolean("enabled", false);
        JSONObject route = VoiceService.routeControlSnapshot(context);
        boolean serviceActive = VoiceService.active;
        AudioManager audio = context.getSystemService(AudioManager.class);
        boolean headsetAvailable = !VoiceAudioRoute.bluetoothCommunicationOutputs(audio).isEmpty();
        return new JSONObject().put("safe_to_switch", !wakeEnabled || !serviceActive
                        || VoiceService.canApplyRemoteRoute())
                .put("active_voice", VoiceService.hasActiveConversation())
                .put("wake_enabled", wakeEnabled)
                .put("phase", route.optString("phase", VoiceService.status))
                .put("selected_target", preferences.getString("audio_route_mode", VoiceAudioRoute.PHONE))
                .put("actual_route", route.optString("actual_route", "unknown"))
                .put("input_type", route.optInt("input_type", -1))
                .put("headset_available", headsetAvailable);
    }

    private JSONObject pendingAck(JSONObject client) throws Exception {
        SharedPreferences preferences = voice();
        String id = preferences.getString(ID, null);
        if (id == null) return null;
        String result = preferences.getString(ACK_RESULT, null);
        String error = preferences.getString(ACK_ERROR, "");
        if (result == null) {
            String target = preferences.getString(TARGET, VoiceRouteTarget.PHONE);
            if ("saved".equals(target)) result = "saved";
            else if ("wake_off".equals(target) && !client.optBoolean("wake_enabled")) result = "applied";
            else if ("wake_on".equals(target)) {
                String selected = client.optString("selected_target", VoiceRouteTarget.PHONE);
                if (VoiceRouteTarget.confirmed(selected, client.optBoolean("wake_enabled"),
                        client.optString("phase"), client.optString("actual_route"),
                        client.optInt("input_type", -1))) result = "applied";
                else if (System.currentTimeMillis() - preferences.getLong(STARTED, 0) > 30000) {
                    result = "error"; error = "voice_start_not_confirmed";
                    preferences.edit().putString(ACK_RESULT, result).putString(ACK_ERROR, error).commit();
                } else return null;
            }
            else {
                int actualInput = client.optInt("input_type", -1);
                String actualRoute = client.optString("actual_route", "unknown");
                if (VoiceRouteTarget.confirmed(target, client.optBoolean("wake_enabled"),
                        client.optString("phase"), actualRoute, actualInput)) {
                    result = "applied";
                } else if ("wake".equals(client.optString("phase"))
                        && System.currentTimeMillis() - preferences.getLong(STARTED, 0) > 18000) {
                    result = "error";
                    error = "bluetooth".equals(target) ? "headset_unavailable" : "audio_route_not_confirmed";
                    preferences.edit().putString(ACK_RESULT, result).putString(ACK_ERROR, error).commit();
                } else if (System.currentTimeMillis() - preferences.getLong(STARTED, 0) > 30000) {
                    result = "error"; error = "audio_route_timeout";
                    preferences.edit().putString(ACK_RESULT, result).putString(ACK_ERROR, error).commit();
                } else return null;
            }
        }
        return new JSONObject().put("id", id).put("result", result)
                .put("actual_route", client.optString("actual_route", "unknown"))
                .put("input_type", client.optInt("input_type", -1))
                .put("wake_enabled", client.optBoolean("wake_enabled"))
                .put("error", error);
    }

    private void applyCommand(JSONObject command) throws Exception {
        SharedPreferences preferences = voice();
        String id = command.getString("id");
        String requested = command.getString("target");
        if (!VoiceRouteTarget.isNewCommand(id, preferences.getString(ID, null))) return;
        if ("wake_on".equals(requested) || "wake_off".equals(requested)) {
            applyWakeCommand(id, requested, preferences);
            return;
        }
        if (!VoiceRouteTarget.PHONE.equals(requested) && !VoiceRouteTarget.BLUETOOTH.equals(requested)
                && !VoiceRouteTarget.TOGGLE.equals(requested)) return;
        boolean wakeEnabled = preferences.getBoolean("enabled", false);
        String previous = preferences.getString("audio_route_mode", VoiceAudioRoute.PHONE);
        String target = VoiceRouteTarget.resolve(requested, previous);
        AudioManager audio = context.getSystemService(AudioManager.class);
        if (VoiceRouteTarget.BLUETOOTH.equals(target)
                && VoiceAudioRoute.bluetoothCommunicationOutputs(audio).isEmpty()) {
            preferences.edit().putString(ID, id).putString(TARGET, "bluetooth")
                    .putString(ACK_RESULT, "error").putString(ACK_ERROR, "headset_unavailable")
                    .putLong(STARTED, System.currentTimeMillis()).putBoolean(PREVIOUS_WAKE, wakeEnabled).commit();
            return;
        }
        if (wakeEnabled && VoiceRouteTarget.confirmed(target, true,
                VoiceService.routeControlSnapshot(context).optString("phase"),
                VoiceService.routeControlSnapshot(context).optString("actual_route"),
                VoiceService.routeControlSnapshot(context).optInt("input_type", -1))) {
            String confirmedSelection = VoiceRouteTarget.confirmedPreferenceTarget(target);
            SharedPreferences.Editor confirmedEdit = preferences.edit()
                    .putString("audio_route_mode", confirmedSelection)
                    .putString(ID, id).putString(TARGET, target).putString(PREVIOUS, previous)
                    .putBoolean(PREVIOUS_WAKE, wakeEnabled)
                    .putLong(STARTED, System.currentTimeMillis()).remove(ACK_RESULT).remove(ACK_ERROR);
            if (VoiceRouteTarget.requiresServiceRestartForConfirmedRoute(previous, target)) {
                if (confirmedEdit.commit()) VoiceService.restartAfterConfiguration(context);
            } else {
                confirmedEdit.putString(ACK_RESULT, "applied").putString(ACK_ERROR, "").commit();
            }
            return;
        }
        SharedPreferences.Editor edit = preferences.edit().putString("audio_route_mode", target)
                .putString(ID, id).putString(TARGET, target).putString(PREVIOUS, previous)
                .putLong(STARTED, System.currentTimeMillis()).putBoolean(PREVIOUS_WAKE, wakeEnabled)
                .remove(ACK_RESULT).remove(ACK_ERROR);
        if (!edit.commit()) return;
        if (!wakeEnabled) {
            preferences.edit().putString(TARGET, "saved").putString(ACK_RESULT, "saved")
                    .putString(ACK_ERROR, "").commit();
        } else {
            VoiceService.restartAfterConfiguration(context);
        }
    }

    private void applyWakeCommand(String id, String target, SharedPreferences preferences) throws Exception {
        boolean enable = "wake_on".equals(target);
        boolean previousWake = preferences.getBoolean("enabled", false);
        String previousRoute = preferences.getString("audio_route_mode", VoiceAudioRoute.PHONE);
        SharedPreferences.Editor edit = preferences.edit().putString(ID, id).putString(TARGET, target)
                .putString(PREVIOUS, previousRoute).putBoolean(PREVIOUS_WAKE, previousWake)
                .putLong(STARTED, System.currentTimeMillis()).remove(ACK_RESULT).remove(ACK_ERROR);
        if (!enable) {
            if (!edit.putBoolean("enabled", false).commit()) return;
            if (VoiceService.active) context.stopService(new Intent(context, VoiceService.class));
            return;
        }
        if ("duplicate".equals(previousRoute)) {
            edit.putString(ACK_RESULT, "error").putString(ACK_ERROR, "select_single_voice_route_first").commit();
            return;
        }
        if (context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            edit.putString(ACK_RESULT, "error").putString(ACK_ERROR, "microphone_permission_required").commit();
            return;
        }
        if (!context.getSharedPreferences("voice_auth", 0).contains("sealed")) {
            edit.putString(ACK_RESULT, "error").putString(ACK_ERROR, "voice_authorization_required").commit();
            return;
        }
        if (!ServerService.active || !"running".equals(ServerService.status)) {
            edit.putString(ACK_RESULT, "error").putString(ACK_ERROR, "server_unavailable").commit();
            return;
        }
        AudioManager audio = context.getSystemService(AudioManager.class);
        if (VoiceRouteTarget.BLUETOOTH.equals(previousRoute)
                && VoiceAudioRoute.bluetoothCommunicationOutputs(audio).isEmpty()) {
            edit.putString(ACK_RESULT, "error").putString(ACK_ERROR, "headset_unavailable").commit();
            return;
        }
        if (!edit.putBoolean("enabled", true)
                .putBoolean(ServerService.PREF_AUTO_START_SUPPRESSED, false).commit()) return;
        ServerService.startVoiceIfReady(context);
    }

    private void onAckRecorded() {
        SharedPreferences preferences = voice();
        String result = preferences.getString(ACK_RESULT, null);
        boolean restore = VoiceRouteTarget.shouldRollback(result);
        String previous = preferences.getString(PREVIOUS, null);
        boolean previousWake = preferences.getBoolean(PREVIOUS_WAKE, preferences.getBoolean("enabled", false));
        SharedPreferences.Editor edit = preferences.edit();
        if (restore && previous != null) edit.putString("audio_route_mode", previous);
        edit.putBoolean("enabled", VoiceRouteTarget.wakeEnabledAfterAck(
                result, previousWake, preferences.getBoolean("enabled", false)));
        edit.remove(ID).remove(TARGET).remove(PREVIOUS).remove(PREVIOUS_WAKE)
                .remove(STARTED).remove(ACK_RESULT).remove(ACK_ERROR).commit();
        if (restore && preferences.getBoolean("enabled", false))
            VoiceService.restartAfterConfiguration(context);
    }

    private void onAckUnknown(String ackId) {
        SharedPreferences preferences = voice();
        String storedId = preferences.getString(ID, null);
        if (!VoiceRouteTarget.matchesPendingCommand(storedId, ackId)) return;
        // HA restarted and lost this in-memory command. Drop only this stale local ACK;
        // preserve the user's selected route and wake setting, then keep polling.
        preferences.edit().remove(ID).remove(TARGET).remove(PREVIOUS).remove(PREVIOUS_WAKE)
                .remove(STARTED).remove(ACK_RESULT).remove(ACK_ERROR).commit();
    }
}
