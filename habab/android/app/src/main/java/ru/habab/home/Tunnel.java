package ru.habab.home;

import android.content.Context;
import android.content.SharedPreferences;
import com.jcraft.jsch.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.net.URI;
import org.json.*;

final class Tunnel {
    private final Context context;
    private volatile Session session;
    Tunnel(Context context) { this.context = context; }

    synchronized String publicKey() throws Exception {
        File directory = new File(context.getFilesDir(), "identity"); directory.mkdirs();
        File key = new File(directory, "tunnel-rsa");
        if (!key.exists()) {
            KeyPair pair = KeyPair.genKeyPair(new JSch(), KeyPair.RSA, 3072);
            try {
                pair.writePrivateKey(key.toString());
                key.setReadable(false, false); key.setReadable(true, true);
                key.setWritable(false, false); key.setWritable(true, true);
                pair.writePublicKey(key + ".pub", "habab-android");
            } finally { pair.dispose(); }
        }
        return new String(Files.readAllBytes(new File(key + ".pub").toPath()), StandardCharsets.US_ASCII).trim();
    }

    static void validate(String host, String port, String user, String remote, String pin, String url) throws Exception {
        if (!host.matches("[A-Za-z0-9][A-Za-z0-9.-]*") || !user.matches("[a-z_][a-z0-9_-]*"))
            throw new IOException("Invalid SSH host or user");
        int sshPort = Integer.parseInt(port), remotePort = Integer.parseInt(remote);
        if (sshPort < 1 || sshPort > 65535 || remotePort < 1024 || remotePort > 65535)
            throw new IOException("Invalid port");
        if (!pin.matches("(ssh-ed25519|ssh-rsa) [A-Za-z0-9+/=]+")) throw new IOException("Invalid pinned SSH host key");
        URI uri = new URI(url);
        if (!"https".equals(uri.getScheme()) || uri.getHost() == null || uri.getUserInfo() != null
                || uri.getQuery() != null || uri.getFragment() != null)
            throw new IOException("A public HTTPS URL is required");
    }

    private boolean hasOwner() {
        File onboarding = new File(new RuntimeStore(context).config(), ".storage/onboarding");
        try {
            JSONObject data = new JSONObject(new String(Files.readAllBytes(onboarding.toPath()), StandardCharsets.UTF_8));
            JSONArray done = data.getJSONObject("data").getJSONArray("done");
            for (int i = 0; i < done.length(); i++) if ("user".equals(done.getString(i))) return true;
        } catch (Exception ignored) { /* No owner: never expose onboarding. */ }
        return false;
    }

    void run() {
        SharedPreferences prefs = context.getSharedPreferences("server", 0);
        if (!prefs.getBoolean("tunnel_enabled", false)) return;
        int delay = 5;
        while (ServerService.active && !Thread.currentThread().isInterrupted()) {
            try {
                if (!hasOwner()) { ServerService.tunnelStatus = "waiting-for-owner"; Thread.sleep(5000); continue; }
                String host = prefs.getString("ssh_host", ""), user = prefs.getString("ssh_user", "");
                int port = prefs.getInt("ssh_port", 22), remote = prefs.getInt("remote_port", 18123);
                String pin = prefs.getString("ssh_pin", "");
                publicKey();
                JSch jsch = new JSch();
                String known = (port == 22 ? host : "[" + host + "]:" + port) + " " + pin + "\n";
                jsch.setKnownHosts(new ByteArrayInputStream(known.getBytes(StandardCharsets.US_ASCII)));
                jsch.addIdentity(new File(context.getFilesDir(), "identity/tunnel-rsa").toString());
                Session candidate = jsch.getSession(user, host, port);
                session = candidate;
                candidate.setConfig("StrictHostKeyChecking", "yes");
                candidate.setConfig("PreferredAuthentications", "publickey");
                candidate.setServerAliveInterval(15000);
                candidate.setServerAliveCountMax(3);
                candidate.connect(20000);
                candidate.setPortForwardingR("127.0.0.1", remote, "127.0.0.1", 8123);
                ServerService.tunnelStatus = "connected"; delay = 5;
                while (candidate.isConnected() && ServerService.active) Thread.sleep(1000);
                candidate.disconnect();
            } catch (InterruptedException error) { Thread.currentThread().interrupt(); break; }
            catch (Exception error) { ServerService.tunnelStatus = "disconnected: " + error.getClass().getSimpleName(); }
            finally { close(); }
            if (!ServerService.active) break;
            try { Thread.sleep(delay * 1000L); }
            catch (InterruptedException error) { Thread.currentThread().interrupt(); break; }
            delay = Math.min(delay * 2, 60);
        }
    }

    void close() { Session current = session; if (current != null) current.disconnect(); }
}
