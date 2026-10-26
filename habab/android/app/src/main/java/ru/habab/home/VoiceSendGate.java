package ru.habab.home;

/** Pure admission rule shared by all realtime provider sends. */
final class VoiceSendGate {
    private VoiceSendGate() {}

    static boolean canSend(boolean closed, boolean ending, boolean hasSocket, boolean toolAllowed,
            Long expectedEpoch, long currentEpoch) {
        return !closed && !ending && hasSocket && toolAllowed
                && (expectedEpoch == null || expectedEpoch == currentEpoch);
    }
}
