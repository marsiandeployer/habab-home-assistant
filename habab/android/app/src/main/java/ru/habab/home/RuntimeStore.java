package ru.habab.home;

import android.content.Context;
import android.net.Uri;
import android.system.Os;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.*;
import java.util.*;
import java.util.concurrent.TimeUnit;
import javax.crypto.*;
import org.json.JSONObject;

final class RuntimeStore {
    interface Progress { void update(String text); }
    private final Context context;
    private final File base;
    RuntimeStore(Context context) { this.context = context.getApplicationContext(); base = context.getFilesDir(); }
    File root() { return new File(base, "runtime"); }
    File config() { return new File(base, "config"); }
    boolean installed() { return new File(root(), "etc/habab-android-release.json").isFile(); }

    static HttpURLConnection connection(String address) throws Exception {
        URL url = new URL(address);
        if (!url.getProtocol().equals("https") || url.getUserInfo() != null || url.getRef() != null || url.getQuery() != null)
            throw new IOException("A plain HTTPS URL is required");
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        connection.setConnectTimeout(20000);
        connection.setReadTimeout(60000);
        connection.setInstanceFollowRedirects(false);
        if (connection.getResponseCode() != 200) { connection.disconnect(); throw new IOException("Download HTTP error"); }
        return connection;
    }

    private static byte[] downloadSmall(String address) throws Exception {
        HttpURLConnection connection = connection(address);
        try (InputStream in = connection.getInputStream(); ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[8192]; int n;
            while ((n = in.read(buffer)) != -1) {
                if (out.size() + n > 1024 * 1024) throw new IOException("Manifest too large");
                out.write(buffer, 0, n);
            }
            return out.toByteArray();
        } finally { connection.disconnect(); }
    }

    void install(String address, Progress progress) throws Exception {
        if (ServerService.active || ServerService.stopping) throw new IOException(context.getString(R.string.stop_first));
        progress.update(context.getString(R.string.check_compatibility));
        verifyNative();
        byte[] manifest = downloadSmall(address);
        byte[] signature = Base64.getDecoder().decode(new String(downloadSmall(address + ".sig"), StandardCharsets.US_ASCII).trim());
        byte[] publicKey;
        try (InputStream input = context.getAssets().open("release/trusted-key.der")) { publicKey = readLimited(input, 8192); }
        ReleaseVerifier.verify(manifest, signature, publicKey);
        JSONObject release = new JSONObject(new String(manifest, StandardCharsets.UTF_8));
        if (release.getInt("schema") != 1 || !release.getString("architecture").equals("aarch64"))
            throw new IOException("Unsupported release");
        long size = release.getLong("bytes"), expanded = release.getLong("unpackedBytes");
        if (size <= 0 || expanded <= 0 || expanded > 16L * 1024 * 1024 * 1024 || size > 8L * 1024 * 1024 * 1024)
            throw new IOException("Invalid release size");
        if (base.getUsableSpace() < size + expanded + 256L * 1024 * 1024)
            throw new IOException("Not enough free space for download and installation");
        File archive = new File(context.getCacheDir(), "runtime-download.tgz");
        File staging = new File(base, "runtime-installing");
        File previous = new File(base, "runtime-previous");
        try {
            MessageDigest hash = MessageDigest.getInstance("SHA-256");
            HttpURLConnection connection = connection(release.getString("url"));
            try (InputStream input = connection.getInputStream(); OutputStream output = new FileOutputStream(archive)) {
                byte[] buffer = new byte[128 * 1024]; int n; long count = 0, last = 0;
                while ((n = input.read(buffer)) != -1) {
                    if (Thread.currentThread().isInterrupted()) throw new InterruptedIOException();
                    count += n;
                    if (count > size) throw new IOException("Download size mismatch");
                    hash.update(buffer, 0, n); output.write(buffer, 0, n);
                    if (count - last > 8 * 1024 * 1024) { progress.update("Download: " + count / 1048576 + " / " + size / 1048576 + " MB"); last = count; }
                }
                if (count != size) throw new IOException("Incomplete download");
            } finally { connection.disconnect(); }
            if (!hex(hash.digest()).equals(release.getString("sha256"))) throw new GeneralSecurityException("Archive checksum mismatch");
            progress.update("Unpacking verified server environment…");
            ArchiveFiles.delete(staging);
            try (InputStream input = new FileInputStream(archive)) { ArchiveFiles.extract(input, staging, expanded, true); }
            if (!new File(staging, "usr/local/bin/python3").exists() || !new File(staging, "etc/habab-android-release.json").isFile())
                throw new IOException("Incomplete server environment");
            ArchiveFiles.delete(previous);
            if (root().exists()) Files.move(root().toPath(), previous.toPath());
            try { Files.move(staging.toPath(), root().toPath()); }
            catch (Exception error) { if (previous.exists()) Files.move(previous.toPath(), root().toPath()); throw error; }
            seedConfig();
        } finally { archive.delete(); ArchiveFiles.delete(staging); }
    }

    private void verifyNative() throws Exception {
        String nativeDir = context.getApplicationInfo().nativeLibraryDir;
        File temporary = new File(base, "tmp"); temporary.mkdirs();
        ProcessBuilder builder = new ProcessBuilder(nativeDir + "/libproot.so", "--version");
        builder.environment().put("LD_LIBRARY_PATH", nativeDir);
        builder.environment().put("PROOT_TMP_DIR", temporary.toString());
        builder.redirectErrorStream(true);
        Process child = builder.start();
        try {
            if (!child.waitFor(10, TimeUnit.SECONDS)) throw new IOException("Native compatibility check timed out");
            byte[] output;
            try (InputStream input = child.getInputStream()) { output = readLimited(input, 65536); }
            if (child.exitValue() != 0) throw new IOException("Native runtime cannot start on this phone: " + new String(output, StandardCharsets.UTF_8));
            JSONObject result = new JSONObject().put("result", "PASS").put("tool", "proot")
                    .put("versionOutput", new String(output, StandardCharsets.UTF_8).trim());
            Files.write(new File(base, "native-check.json").toPath(), result.toString().getBytes(StandardCharsets.UTF_8));
        } finally { if (child.isAlive()) child.destroyForcibly(); }
    }

    void seedConfig() throws Exception {
        config().mkdirs(); Os.chmod(config().toString(), 0700);
        File target = new File(config(), "configuration.yaml");
        if (!target.exists()) Files.copy(new File(root(), "usr/local/share/habab-home/configuration.yaml").toPath(), target.toPath());
        for (String name : new String[]{"automations.yaml", "scenes.yaml", "scripts.yaml"}) {
            File file = new File(config(), name);
            if (!file.exists()) Files.write(file.toPath(), (name.equals("scripts.yaml") ? "{}\n" : "[]\n").getBytes(StandardCharsets.UTF_8));
        }
    }

    private Cipher backupCipher(int mode, char[] password, byte[] salt, byte[] iv) throws Exception {
        return BackupCrypto.cipher(mode, password, salt, iv);
    }

    void backup(Uri uri, char[] password) throws Exception {
        if (ServerService.active || ServerService.stopping) throw new IOException(context.getString(R.string.stop_first));
        if (!new File(config(), "configuration.yaml").isFile()) throw new IOException("No configuration to back up");
        byte[] salt = new byte[16], iv = new byte[12];
        new SecureRandom().nextBytes(salt); new SecureRandom().nextBytes(iv);
        try (OutputStream out = context.getContentResolver().openOutputStream(uri, "wt")) {
            out.write("HABBAK01".getBytes(StandardCharsets.US_ASCII)); out.write(salt); out.write(iv);
            ArchiveFiles.pack(config(), new CipherOutputStream(out, backupCipher(Cipher.ENCRYPT_MODE, password, salt, iv)));
        }
    }

    void restore(Uri uri, char[] password) throws Exception {
        if (ServerService.active || ServerService.stopping) throw new IOException(context.getString(R.string.stop_first));
        File staging = new File(base, "config-restoring");
        ArchiveFiles.delete(staging);
        File decrypted = new File(context.getCacheDir(), "restore-authenticated.tgz");
        try {
            // Authenticate GCM to EOF before parsing or replacing any configuration.
            try (DataInputStream input = new DataInputStream(context.getContentResolver().openInputStream(uri))) {
                byte[] header = new byte[8], salt = new byte[16], iv = new byte[12];
                input.readFully(header); input.readFully(salt); input.readFully(iv);
                if (!Arrays.equals(header, "HABBAK01".getBytes(StandardCharsets.US_ASCII))) throw new IOException("Unknown backup format");
                Cipher cipher = backupCipher(Cipher.DECRYPT_MODE, password, salt, iv);
                try (InputStream plain = new CipherInputStream(input, cipher); OutputStream output = new FileOutputStream(decrypted)) {
                    byte[] buffer = new byte[65536]; int n; long count = 0;
                    while ((n = plain.read(buffer)) != -1) {
                        if ((count += n) > 4L * 1024 * 1024 * 1024) throw new IOException("Backup too large");
                        output.write(buffer, 0, n);
                    }
                }
            }
            try (InputStream input = new FileInputStream(decrypted)) { ArchiveFiles.extract(input, staging, 8L * 1024 * 1024 * 1024); }
            if (!new File(staging, "configuration.yaml").isFile()) throw new IOException("Backup has no configuration");
            File previous = new File(base, "config-before-restore-" + System.currentTimeMillis());
            if (config().exists()) Files.move(config().toPath(), previous.toPath());
            try { Files.move(staging.toPath(), config().toPath()); }
            catch (Exception error) { if (previous.exists()) Files.move(previous.toPath(), config().toPath()); throw error; }
        } finally { decrypted.delete(); ArchiveFiles.delete(staging); Arrays.fill(password, '\0'); }
    }

    static byte[] readLimited(InputStream input, int limit) throws IOException {
        ByteArrayOutputStream output = new ByteArrayOutputStream(); byte[] buffer = new byte[8192]; int n;
        while ((n = input.read(buffer)) != -1) {
            if (output.size() + n > limit) throw new IOException("File exceeds limit");
            output.write(buffer, 0, n);
        }
        return output.toByteArray();
    }
    static String hex(byte[] value) {
        StringBuilder result = new StringBuilder();
        for (byte b : value) result.append(String.format(Locale.ROOT, "%02x", b & 255));
        return result.toString();
    }
}
