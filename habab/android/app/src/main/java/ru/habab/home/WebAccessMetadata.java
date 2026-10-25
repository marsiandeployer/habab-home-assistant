package ru.habab.home;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/** Selects a phone-local URL and conservative LAN URL candidates from a connectivity snapshot. */
final class WebAccessMetadata {
    static final String SOURCE = "android_connectivity_manager";

    static final class NetworkSnapshot {
        final String id;
        final boolean connected;
        final boolean wifi;
        final boolean ethernet;
        final boolean vpn;
        final List<String> ipv4Addresses;

        NetworkSnapshot(String id, boolean connected, boolean wifi, boolean ethernet,
                        boolean vpn, List<String> ipv4Addresses) {
            this.id = id;
            this.connected = connected;
            this.wifi = wifi;
            this.ethernet = ethernet;
            this.vpn = vpn;
            this.ipv4Addresses = ipv4Addresses == null
                    ? Collections.emptyList() : new ArrayList<>(ipv4Addresses);
        }
    }

    static final class Result {
        final String phoneUrl;
        final String lanUrl;
        final List<String> lanUrls;

        Result(String phoneUrl, String lanUrl, List<String> lanUrls) {
            this.phoneUrl = phoneUrl;
            this.lanUrl = lanUrl;
            this.lanUrls = Collections.unmodifiableList(new ArrayList<>(lanUrls));
        }
    }

    static Result resolve(String phoneBaseUrl, String activeNetworkId,
                          List<NetworkSnapshot> networks) {
        Set<String> allAddresses = new LinkedHashSet<>();
        Set<String> activeAddresses = new LinkedHashSet<>();
        if (networks != null) {
            for (NetworkSnapshot network : networks) {
                if (network == null || !network.connected || network.vpn
                        || !(network.wifi || network.ethernet)) continue;
                List<String> valid = validAddresses(network.ipv4Addresses);
                allAddresses.addAll(valid);
                if (activeNetworkId != null && activeNetworkId.equals(network.id)) {
                    activeAddresses.addAll(valid);
                }
            }
        }

        List<String> candidates = new ArrayList<>(allAddresses);
        Collections.sort(candidates);
        String selected = null;
        if (!activeAddresses.isEmpty()) {
            List<String> active = new ArrayList<>(activeAddresses);
            Collections.sort(active);
            selected = active.get(0);
        } else if (candidates.size() == 1) {
            selected = candidates.get(0);
        }
        String lanUrl = selected == null ? null : withHostAndPort(phoneBaseUrl, selected);
        List<String> lanUrls = new ArrayList<>();
        for (String address : candidates) lanUrls.add(withHostAndPort(phoneBaseUrl, address));
        return new Result(phoneBaseUrl, lanUrl, lanUrls);
    }

    private static List<String> validAddresses(List<String> addresses) {
        Set<String> valid = new LinkedHashSet<>();
        if (addresses != null) {
            for (String address : addresses) if (isUsableIpv4(address)) valid.add(address);
        }
        return new ArrayList<>(valid);
    }

    private static boolean isUsableIpv4(String address) {
        if (address == null) return false;
        String[] parts = address.split("\\.", -1);
        if (parts.length != 4) return false;
        int[] octets = new int[4];
        for (int i = 0; i < parts.length; i++) {
            if (parts[i].isEmpty() || parts[i].length() > 3
                    || (parts[i].length() > 1 && parts[i].charAt(0) == '0')) return false;
            int value = 0;
            for (int j = 0; j < parts[i].length(); j++) {
                char digit = parts[i].charAt(j);
                if (digit < '0' || digit > '9') return false;
                value = value * 10 + digit - '0';
            }
            if (value > 255) return false;
            octets[i] = value;
        }
        if (octets[0] == 0 || octets[0] == 127 || octets[0] >= 224) return false;
        if (octets[0] == 169 && octets[1] == 254) return false;
        return !(octets[0] == 255 && octets[1] == 255 && octets[2] == 255 && octets[3] == 255);
    }

    private static String withHostAndPort(String baseUrl, String host) {
        int schemeEnd = baseUrl.indexOf("://");
        if (schemeEnd < 0) throw new IllegalArgumentException("Base URL must be absolute");
        int authorityStart = schemeEnd + 3;
        int pathStart = baseUrl.indexOf('/', authorityStart);
        String authority = pathStart < 0 ? baseUrl.substring(authorityStart)
                : baseUrl.substring(authorityStart, pathStart);
        int colon = authority.lastIndexOf(':');
        String port = colon < 0 ? "" : authority.substring(colon);
        return baseUrl.substring(0, authorityStart) + host + port;
    }

    private WebAccessMetadata() { }
}
