package ru.habab.home;

import java.io.IOException;
import java.util.ArrayList;
import java.util.List;

/** Parses CDC Union functional descriptors from one USB configuration descriptor. */
final class CdcUnionParser {
    private CdcUnionParser() { }

    static List<Union> parse(byte[] descriptors) throws IOException {
        if (descriptors == null || descriptors.length < 9
                || (descriptors[1] & 0xff) != 0x02) {
            throw new IOException("USB configuration descriptor is unavailable");
        }
        int configurationLength = descriptors[0] & 0xff;
        int totalLength = (descriptors[2] & 0xff) | ((descriptors[3] & 0xff) << 8);
        if (configurationLength < 9 || configurationLength > totalLength
                || totalLength < 9 || totalLength != descriptors.length) {
            throw new IOException("Invalid USB configuration descriptor length");
        }

        List<Union> unions = new ArrayList<>();
        int currentInterface = -1;
        for (int offset = 0; offset < totalLength; ) {
            if (offset + 2 > totalLength) throw new IOException("Truncated USB descriptor header");
            int length = descriptors[offset] & 0xff;
            int type = descriptors[offset + 1] & 0xff;
            if (length < 2 || offset + length > totalLength) {
                throw new IOException("Malformed USB descriptor chain");
            }
            if (type == 0x04) {
                if (length < 9) throw new IOException("Malformed USB interface descriptor");
                currentInterface = descriptors[offset + 2] & 0xff;
            } else if (type == 0x24 && length >= 3
                    && (descriptors[offset + 2] & 0xff) == 0x06) {
                if (length < 5) throw new IOException("Malformed CDC Union descriptor");
                if (currentInterface < 0) {
                    throw new IOException("CDC Union descriptor has no interface owner");
                }
                int master = descriptors[offset + 3] & 0xff;
                for (int index = offset + 4; index < offset + length; index++) {
                    unions.add(new Union(currentInterface, master, descriptors[index] & 0xff));
                }
            }
            offset += length;
        }
        return unions;
    }

    static InterfacePair selectInterfaces(List<Union> unions, List<InterfaceInfo> interfaces)
            throws IOException {
        InterfacePair match = null;
        for (Union union : unions) {
            if (union.controlInterface != union.masterInterface) continue;
            for (InterfaceInfo control : interfaces) {
                if (control.id != union.controlInterface || control.interfaceClass != 0x02
                        || control.subclass != 0x02 || control.alternateSetting != 0) continue;
                for (InterfaceInfo data : interfaces) {
                    if (data.id != union.slaveInterface || data.interfaceClass != 0x0a
                            || data.alternateSetting != 0 || !data.hasBulkIn || !data.hasBulkOut) continue;
                    if (match != null) {
                        throw new IOException("Ambiguous CDC ACM control/data interface pairing");
                    }
                    match = new InterfacePair(control.id, data.id);
                }
            }
        }
        if (match == null) {
            throw new IOException("No union-linked CDC ACM interface with bulk IN/OUT endpoints");
        }
        return match;
    }

    static final class InterfaceInfo {
        final int id;
        final int interfaceClass;
        final int subclass;
        final int alternateSetting;
        final boolean hasBulkIn;
        final boolean hasBulkOut;

        InterfaceInfo(int id, int interfaceClass, int subclass, int alternateSetting,
                boolean hasBulkIn, boolean hasBulkOut) {
            this.id = id;
            this.interfaceClass = interfaceClass;
            this.subclass = subclass;
            this.alternateSetting = alternateSetting;
            this.hasBulkIn = hasBulkIn;
            this.hasBulkOut = hasBulkOut;
        }
    }

    static final class InterfacePair {
        final int controlId;
        final int dataId;

        InterfacePair(int controlId, int dataId) {
            this.controlId = controlId;
            this.dataId = dataId;
        }
    }

    static final class Union {
        final int controlInterface;
        final int masterInterface;
        final int slaveInterface;

        Union(int controlInterface, int masterInterface, int slaveInterface) {
            this.controlInterface = controlInterface;
            this.masterInterface = masterInterface;
            this.slaveInterface = slaveInterface;
        }
    }
}
