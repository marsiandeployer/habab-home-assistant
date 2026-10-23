package ru.habab.home;

import org.junit.Test;
import static org.junit.Assert.*;

public class LocalAddressTest {
    @Test public void aliasStaysOnThisPhoneAndPreservesNavigation() {
        assertEquals("http://127.0.0.1:8123/", LocalAddress.resolve("http://homeassistant.local:8123"));
        assertEquals("http://127.0.0.1:8123/config/devices?q=a%20b#test", LocalAddress.resolve("http://homeassistant.local:8123/config/devices?q=a%20b#test"));
    }
    @Test public void rejectsOtherHostsPortsSchemesAndCredentials() {
        for (String address : new String[]{null, "http://homeassistant.local.evil.test:8123/", "http://other.local:8123/",
                "http://homeassistant.local:8124/", "https://homeassistant.local:8123/", "javascript:alert(1)",
                "http://user@homeassistant.local:8123/", "http://homeassistant.local/", "malformed address"})
            assertNull(LocalAddress.resolve(address));
    }
}
