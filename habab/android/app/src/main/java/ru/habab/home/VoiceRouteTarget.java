package ru.habab.home;

/** Fixed route targets accepted by the owner-authenticated Home Assistant action. */
final class VoiceRouteTarget {
    static final String PHONE = "phone";
    static final String BLUETOOTH = "bluetooth";
    static final String TOGGLE = "toggle";
    static final int BLUETOOTH_SCO_INPUT = 7;
    static final int BUILTIN_MIC_INPUT = 15;

    private VoiceRouteTarget() {}

    static String resolve(String requested, String selected) {
        if (PHONE.equals(requested) || BLUETOOTH.equals(requested)) return requested;
        if (!TOGGLE.equals(requested)) throw new IllegalArgumentException("invalid_voice_route_target");
        return BLUETOOTH.equals(selected) || "duplicate".equals(selected) ? PHONE : BLUETOOTH;
    }

    static String confirmedPreferenceTarget(String target) {
        if (!PHONE.equals(target) && !BLUETOOTH.equals(target))
            throw new IllegalArgumentException("invalid_voice_route_target");
        return target;
    }

    static boolean requiresServiceRestartForConfirmedRoute(String previous, String target) {
        return !confirmedPreferenceTarget(target).equals(previous);
    }

    static boolean confirmed(String target, boolean wakeEnabled, String phase,
            String actualRoute, int inputType) {
        if (!wakeEnabled) return false;
        if (!"wake".equals(phase)) return false;
        if (BLUETOOTH.equals(target))
            return !"bluetooth_unavailable".equals(actualRoute)
                    && inputType == BLUETOOTH_SCO_INPUT;
        return PHONE.equals(target) && "phone".equals(actualRoute)
                && inputType == BUILTIN_MIC_INPUT;
    }

    static boolean safeBoundary(boolean wakeEnabled, boolean serviceActive, boolean recorderReady,
            String phase, boolean conversationActive, boolean endCueComplete,
            boolean releasingFocus) {
        if (!wakeEnabled || !serviceActive) return true;
        return recorderReady && "wake".equals(phase) && !conversationActive
                && endCueComplete && !releasingFocus;
    }

    static boolean shouldRollback(String result) { return "error".equals(result); }

    static boolean isNewCommand(String commandId, String pendingId) {
        return commandId != null && !commandId.isEmpty() && !commandId.equals(pendingId);
    }

    static boolean matchesPendingCommand(String storedId, String ackId) {
        return storedId != null && !storedId.isEmpty() && storedId.equals(ackId);
    }

    static boolean wakeEnabledAfterAck(String result, boolean previous, boolean current) {
        return shouldRollback(result) ? previous : current;
    }
}
