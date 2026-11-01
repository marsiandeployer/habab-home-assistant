package ru.habab.home;

import org.junit.Test;

import java.io.IOException;
import java.util.Arrays;
import java.util.List;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertThrows;

public final class CdcUnionParserTest {
    @Test
    public void parsesUnionOwnedByControlInterface() throws Exception {
        List<CdcUnionParser.Union> unions = CdcUnionParser.parse(configuration(
                iface(0, 0x02, 0x02),
                new byte[] {5, 0x24, 0x06, 0, 1},
                iface(1, 0x0a, 0x00)));

        assertEquals(1, unions.size());
        assertEquals(0, unions.get(0).controlInterface);
        assertEquals(0, unions.get(0).masterInterface);
        assertEquals(1, unions.get(0).slaveInterface);
    }

    @Test
    public void parsesMultipleSlaveInterfaces() throws Exception {
        List<CdcUnionParser.Union> unions = CdcUnionParser.parse(configuration(
                iface(3, 0x02, 0x02),
                new byte[] {6, 0x24, 0x06, 3, 4, 5}));

        assertEquals(2, unions.size());
        assertEquals(4, unions.get(0).slaveInterface);
        assertEquals(5, unions.get(1).slaveInterface);
    }

    @Test
    public void rejectsUnionBeforeItsOwningInterface() {
        assertThrows(IOException.class, () -> CdcUnionParser.parse(configuration(
                new byte[] {5, 0x24, 0x06, 0, 1})));
    }

    @Test
    public void rejectsZeroLengthDescriptorInsteadOfLooping() {
        assertThrows(IOException.class, () -> CdcUnionParser.parse(configuration(
                new byte[] {0, 0x04, 0, 0, 0, 0, 0, 0, 0})));
    }

    @Test
    public void rejectsTruncatedInterfaceAndUnionDescriptors() {
        assertThrows(IOException.class, () -> CdcUnionParser.parse(configuration(
                new byte[] {8, 0x04, 0, 0, 0, 0, 0, 0})));
        assertThrows(IOException.class, () -> CdcUnionParser.parse(configuration(
                iface(0, 0x02, 0x02), new byte[] {4, 0x24, 0x06, 0})));
    }

    @Test
    public void rejectsInvalidConfigurationHeaderLength() {
        byte[] invalid = configuration();
        invalid[0] = 8;

        assertThrows(IOException.class, () -> CdcUnionParser.parse(invalid));
    }

    @Test
    public void selectsUnionLinkedAcmControlAndBulkDataInterface() throws Exception {
        List<CdcUnionParser.Union> unions = CdcUnionParser.parse(configuration(
                iface(0, 0x02, 0x02), new byte[] {5, 0x24, 0x06, 0, 1},
                iface(1, 0x0a, 0x00)));
        CdcUnionParser.InterfacePair pair = CdcUnionParser.selectInterfaces(unions, Arrays.asList(
                new CdcUnionParser.InterfaceInfo(0, 0x02, 0x02, 0, false, false),
                new CdcUnionParser.InterfaceInfo(1, 0x0a, 0, 0, true, true)));

        assertEquals(0, pair.controlId);
        assertEquals(1, pair.dataId);
    }

    @Test
    public void rejectsWrongMasterClassAlternateSettingOrMissingEndpoint() throws Exception {
        List<CdcUnionParser.Union> unions = CdcUnionParser.parse(configuration(
                iface(0, 0x02, 0x02), new byte[] {5, 0x24, 0x06, 0, 1},
                iface(1, 0x0a, 0x00)));
        List<CdcUnionParser.InterfaceInfo> valid = Arrays.asList(
                new CdcUnionParser.InterfaceInfo(0, 0x02, 0x02, 0, false, false),
                new CdcUnionParser.InterfaceInfo(1, 0x0a, 0, 0, true, true));
        List<CdcUnionParser.Union> wrongMaster = Arrays.asList(new CdcUnionParser.Union(0, 2, 1));

        assertThrows(IOException.class, () -> CdcUnionParser.selectInterfaces(wrongMaster, valid));
        assertThrows(IOException.class, () -> CdcUnionParser.selectInterfaces(unions, Arrays.asList(
                new CdcUnionParser.InterfaceInfo(0, 0x02, 0x03, 0, false, false), valid.get(1))));
        assertThrows(IOException.class, () -> CdcUnionParser.selectInterfaces(unions, Arrays.asList(
                new CdcUnionParser.InterfaceInfo(0, 0x02, 0x02, 1, false, false), valid.get(1))));
        assertThrows(IOException.class, () -> CdcUnionParser.selectInterfaces(unions, Arrays.asList(
                valid.get(0), new CdcUnionParser.InterfaceInfo(1, 0x0a, 0, 1, true, true))));
        assertThrows(IOException.class, () -> CdcUnionParser.selectInterfaces(unions, Arrays.asList(
                valid.get(0), new CdcUnionParser.InterfaceInfo(1, 0x0a, 0, 0, true, false))));
    }

    @Test
    public void rejectsAmbiguousCompatibleInterfacePairs() throws Exception {
        List<CdcUnionParser.Union> unions = CdcUnionParser.parse(configuration(
                iface(0, 0x02, 0x02), new byte[] {6, 0x24, 0x06, 0, 1, 2},
                iface(1, 0x0a, 0x00), iface(2, 0x0a, 0x00)));

        assertThrows(IOException.class, () -> CdcUnionParser.selectInterfaces(unions, Arrays.asList(
                new CdcUnionParser.InterfaceInfo(0, 0x02, 0x02, 0, false, false),
                new CdcUnionParser.InterfaceInfo(1, 0x0a, 0, 0, true, true),
                new CdcUnionParser.InterfaceInfo(2, 0x0a, 0, 0, true, true))));
    }

    @Test
    public void rejectsDescriptorPastDeclaredConfigurationLength() {
        byte[] invalid = configuration(new byte[] {4, 0x24, 0x06, 0});
        invalid[9] = 6;

        assertThrows(IOException.class, () -> CdcUnionParser.parse(invalid));
    }

    private static byte[] iface(int number, int deviceClass, int subclass) {
        return new byte[] {9, 0x04, (byte) number, 0, 0, (byte) deviceClass,
                (byte) subclass, 0, 0};
    }

    private static byte[] configuration(byte[]... descriptors) {
        int length = 9;
        for (byte[] descriptor : descriptors) length += descriptor.length;
        byte[] result = new byte[length];
        result[0] = 9;
        result[1] = 0x02;
        result[2] = (byte) (length & 0xff);
        result[3] = (byte) ((length >>> 8) & 0xff);
        int offset = 9;
        for (byte[] descriptor : descriptors) {
            System.arraycopy(descriptor, 0, result, offset, descriptor.length);
            offset += descriptor.length;
        }
        return result;
    }
}
