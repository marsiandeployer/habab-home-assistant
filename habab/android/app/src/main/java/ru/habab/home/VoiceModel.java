package ru.habab.home;

import android.content.Context;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.MessageDigest;
import java.util.zip.*;
import org.json.JSONObject;

final class VoiceModel {
    static File prepare(Context context) throws Exception {
        JSONObject release;
        try (InputStream in = context.getAssets().open("voice/model.json")) {
            release = new JSONObject(new String(RuntimeStore.readLimited(in, 8192), StandardCharsets.UTF_8));
        }
        String digest = release.getString("sha256");
        File ready = new File(context.getFilesDir(), "voice-model-" + digest.substring(0, 12));
        if (new File(ready, "ready").isFile()) return new File(ready, release.getString("name"));
        File staging = new File(context.getFilesDir(), "voice-model-installing");
        ArchiveFiles.delete(staging); staging.mkdirs();
        MessageDigest hash = MessageDigest.getInstance("SHA-256");
        try (InputStream input = context.getAssets().open("voice/model.zip")) {
            byte[] block = new byte[65536]; int count;
            while ((count = input.read(block)) != -1) hash.update(block, 0, count);
        }
        if (!RuntimeStore.hex(hash.digest()).equals(digest)) throw new IOException("voice_model_checksum_mismatch");
        long total = 0; int files = 0;
        try (ZipInputStream zip = new ZipInputStream(context.getAssets().open("voice/model.zip"))) {
            ZipEntry entry; byte[] block = new byte[65536];
            while ((entry = zip.getNextEntry()) != null) {
                if (Thread.currentThread().isInterrupted()) throw new InterruptedIOException();
                if (++files > 2000) throw new IOException("invalid_voice_model");
                Path path = ArchiveFiles.destination(staging.toPath(), entry.getName());
                if (entry.isDirectory()) Files.createDirectories(path);
                else {
                    Files.createDirectories(path.getParent());
                    try (OutputStream out = Files.newOutputStream(path, StandardOpenOption.CREATE_NEW)) {
                        int count;
                        while ((count = zip.read(block)) != -1) {
                            if ((total += count) > 256L * 1024 * 1024) throw new IOException("voice_model_too_large");
                            out.write(block, 0, count);
                        }
                    }
                }
            }
        }
        if (!new File(staging, release.getString("name") + "/am/final.mdl").isFile()) throw new IOException("incomplete_voice_model");
        Files.write(new File(staging, "ready").toPath(), digest.getBytes(StandardCharsets.US_ASCII));
        Files.move(staging.toPath(), ready.toPath());
        return new File(ready, release.getString("name"));
    }
}
