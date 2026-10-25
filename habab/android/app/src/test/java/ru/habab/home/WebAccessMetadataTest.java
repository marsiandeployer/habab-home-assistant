package ru.habab.home;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;

import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import org.junit.Test;

public class WebAccessMetadataTest {
    private static final String BASE_URL = "http://127.0.0.1:8123";

    @Test public void activeWifiAddressBecomesLanUrl() {
        WebAccessMetadata.Result result = WebAccessMetadata.resolve(BASE_URL, "wifi",
                Collections.singletonList(network("wifi", true, false, "192.168.1.20")));

        assertEquals(BASE_URL, result.phoneUrl);
        assertEquals("http://192.168.1.20:8123", result.lanUrl);
        assertEquals(Collections.singletonList(result.lanUrl), result.lanUrls);
    }

    @Test public void activeEthernetWinsOverOtherLanCandidates() {
        WebAccessMetadata.Result result = WebAccessMetadata.resolve(BASE_URL, "ethernet",
                Arrays.asList(network("wifi", true, false, "192.168.1.20"),
                        network("ethernet", false, true, "10.0.0.5")));

        assertEquals("http://10.0.0.5:8123", result.lanUrl);
        assertEquals(2, result.lanUrls.size());
    }

    @Test public void multipleLanNetworksWithoutActiveLanRemainAmbiguous() {
        WebAccessMetadata.Result result = WebAccessMetadata.resolve(BASE_URL, "mobile",
                Arrays.asList(network("wifi", true, false, "192.168.1.20"),
                        network("ethernet", false, true, "10.0.0.5"),
                        new WebAccessMetadata.NetworkSnapshot("mobile", true, false, false,
                                false, Collections.singletonList("100.64.0.4"))));

        assertNull(result.lanUrl);
        assertEquals(Arrays.asList("http://10.0.0.5:8123", "http://192.168.1.20:8123"), result.lanUrls);
    }

    @Test public void uniqueUnderlyingWifiIsSelectedWhenVpnIsActive() {
        WebAccessMetadata.Result result = WebAccessMetadata.resolve(BASE_URL, "vpn",
                Arrays.asList(network("wifi", true, false, "192.168.1.20"),
                        new WebAccessMetadata.NetworkSnapshot("vpn", true, false, false,
                                true, Collections.singletonList("10.8.0.2"))));

        assertEquals("http://192.168.1.20:8123", result.lanUrl);
        assertEquals(Collections.singletonList(result.lanUrl), result.lanUrls);
    }

    @Test public void loopbackUnspecifiedAndLinkLocalAddressesAreExcluded() {
        WebAccessMetadata.Result result = WebAccessMetadata.resolve(BASE_URL, "wifi",
                Collections.singletonList(network("wifi", true, false, "127.0.0.1", "0.0.0.0",
                        "169.254.2.3", "192.168.1.20")));

        assertEquals("http://192.168.1.20:8123", result.lanUrl);
        assertEquals(Collections.singletonList(result.lanUrl), result.lanUrls);
    }

    @Test public void mobileAndVpnAddressesAreNeverLanCandidates() {
        WebAccessMetadata.Result result = WebAccessMetadata.resolve(BASE_URL, "vpn",
                Arrays.asList(new WebAccessMetadata.NetworkSnapshot("mobile", true, false, false,
                                false, Collections.singletonList("100.64.0.4")),
                        new WebAccessMetadata.NetworkSnapshot("vpn", true, true, false,
                                true, Collections.singletonList("10.8.0.2"))));

        assertNull(result.lanUrl);
        assertEquals(Collections.emptyList(), result.lanUrls);
    }

    @Test public void connectedLanWithoutIpv4PublishesNoLanUrl() {
        WebAccessMetadata.Result result = WebAccessMetadata.resolve(BASE_URL, "wifi",
                Collections.singletonList(new WebAccessMetadata.NetworkSnapshot("wifi", true, true, false,
                        false, Arrays.asList("fe80::1", "192.168.1.999", "192.168.1", "192.168.001.2",
                                "224.0.0.1", "239.255.255.250", "240.0.0.1", "255.255.255.255"))));

        assertNull(result.lanUrl);
        assertEquals(Collections.emptyList(), result.lanUrls);
    }

    @Test public void disconnectedLanSnapshotIsIgnored() {
        WebAccessMetadata.NetworkSnapshot disconnected = new WebAccessMetadata.NetworkSnapshot(
                "wifi", false, true, false, false, Collections.singletonList("192.168.1.20"));

        WebAccessMetadata.Result result = WebAccessMetadata.resolve(BASE_URL, "wifi",
                Collections.singletonList(disconnected));

        assertNull(result.lanUrl);
        assertEquals(Collections.emptyList(), result.lanUrls);
    }

    private static WebAccessMetadata.NetworkSnapshot network(
            String id, boolean wifi, boolean ethernet, String... addresses) {
        return new WebAccessMetadata.NetworkSnapshot(id, true, wifi, ethernet, false,
                Arrays.asList(addresses));
    }
}
