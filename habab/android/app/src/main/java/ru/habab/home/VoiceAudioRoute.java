package ru.habab.home;

import android.media.AudioDeviceInfo;
import android.media.AudioManager;
import android.os.Build;

import java.util.ArrayList;
import java.util.List;

/** Selects a Bluetooth communication endpoint without scanning or opening its microphone. */
final class VoiceAudioRoute {
    static final String PHONE = "phone";
    static final String BLUETOOTH = "bluetooth";
    static final String DUPLICATE = "duplicate";

    private VoiceAudioRoute() {}

    static String requestedRoute(String configuredRoute, boolean legacyBluetooth) {
        if (configuredRoute == null) return legacyBluetooth ? BLUETOOTH : PHONE;
        if (PHONE.equals(configuredRoute) || BLUETOOTH.equals(configuredRoute) || DUPLICATE.equals(configuredRoute))
            return configuredRoute;
        return PHONE;
    }

    static List<AudioDeviceInfo> bluetoothCommunicationOutputs(AudioManager manager) {
        AudioDeviceInfo[] devices = Build.VERSION.SDK_INT >= Build.VERSION_CODES.S
                ? manager.getAvailableCommunicationDevices().toArray(new AudioDeviceInfo[0])
                : manager.getDevices(AudioManager.GET_DEVICES_OUTPUTS);
        List<AudioDeviceInfo> result = new ArrayList<>();
        for (AudioDeviceInfo device : devices) {
            if (!device.isSink()) continue;
            int type = device.getType();
            if (type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO
                    || (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S
                    && type == AudioDeviceInfo.TYPE_BLE_HEADSET)) result.add(device);
        }
        return result;
    }

    static AudioDeviceInfo bluetoothCommunicationOutput(AudioManager manager) {
        List<AudioDeviceInfo> devices = bluetoothCommunicationOutputs(manager);
        return devices.isEmpty() ? null : devices.get(0);
    }

    static AudioDeviceInfo phoneSpeaker(AudioManager manager) {
        for (AudioDeviceInfo device : manager.getDevices(AudioManager.GET_DEVICES_OUTPUTS))
            if (device.isSink() && device.getType() == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER) return device;
        return null;
    }

    static boolean selectBluetooth(AudioManager manager, AudioDeviceInfo device) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) return manager.setCommunicationDevice(device);
        manager.setMode(AudioManager.MODE_IN_COMMUNICATION);
        manager.startBluetoothSco();
        return true;
    }

    static void selectPhone(AudioManager manager) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            for (AudioDeviceInfo device : manager.getAvailableCommunicationDevices()) {
                if (device.getType() == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER
                        && manager.setCommunicationDevice(device)) return;
            }
            manager.clearCommunicationDevice();
        } else {
            manager.setMode(AudioManager.MODE_IN_COMMUNICATION);
            manager.setSpeakerphoneOn(true);
        }
    }

    static void release(AudioManager manager) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) manager.clearCommunicationDevice();
        else {
            manager.stopBluetoothSco();
            manager.setSpeakerphoneOn(false);
            manager.setMode(AudioManager.MODE_NORMAL);
        }
    }

    static String deviceName(AudioDeviceInfo device) {
        CharSequence name = device.getProductName();
        return name == null || name.length() == 0 ? "Bluetooth" : name.toString();
    }
}
