package ru.habab.home;

import android.content.Context;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import java.io.IOException;
import java.security.KeyStore;
import java.util.concurrent.TimeUnit;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;
import okhttp3.*;
import org.json.JSONObject;

/** HA authorization stays in Android Keystore-encrypted private preferences. */
final class VoiceCredentials {
    private static final String ALIAS = "habab.voice.authorization.v1";
    private static final Object LOCK = new Object();
    private final Context context;
    private final OkHttpClient http = new OkHttpClient.Builder()
            .connectTimeout(8, TimeUnit.SECONDS).readTimeout(120, TimeUnit.SECONDS)
            .followRedirects(false).followSslRedirects(false).build();
    VoiceCredentials(Context context) { this.context = context.getApplicationContext(); }

    private SecretKey key() throws Exception {
        KeyStore store = KeyStore.getInstance("AndroidKeyStore"); store.load(null);
        if (store.containsAlias(ALIAS)) return ((KeyStore.SecretKeyEntry) store.getEntry(ALIAS, null)).getSecretKey();
        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        generator.init(new KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setUserAuthenticationRequired(false).build());
        return generator.generateKey();
    }

    boolean configured() { return context.getSharedPreferences("voice_auth", 0).contains("sealed"); }

    void save(JSONObject source) throws Exception {
        synchronized (LOCK) {
            JSONObject data = new JSONObject();
            for (String name : new String[]{"access_token", "refresh_token", "clientId"}) {
                String value = source.getString(name);
                if (value.isEmpty() || value.length() > 8192) throw new IOException("invalid_voice_authorization");
                data.put(name, value);
            }
            data.put("expires", source.optLong("expires", System.currentTimeMillis() + source.optLong("expires_in", 1800) * 1000));
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding"); cipher.init(Cipher.ENCRYPT_MODE, key());
            byte[] encrypted = cipher.doFinal(data.toString().getBytes(java.nio.charset.StandardCharsets.UTF_8));
            JSONObject sealed = new JSONObject().put("iv", Base64.encodeToString(cipher.getIV(), Base64.NO_WRAP))
                    .put("data", Base64.encodeToString(encrypted, Base64.NO_WRAP));
            if (!context.getSharedPreferences("voice_auth", 0).edit().putString("sealed", sealed.toString()).commit())
                throw new IOException("voice_authorization_not_saved");
        }
    }

    private String token() throws Exception {
        synchronized (LOCK) {
            String value = context.getSharedPreferences("voice_auth", 0).getString("sealed", null);
            if (value == null) throw new IOException("voice_authorization_required");
            JSONObject sealed = new JSONObject(value);
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(128, Base64.decode(sealed.getString("iv"), Base64.NO_WRAP)));
            JSONObject data = new JSONObject(new String(cipher.doFinal(Base64.decode(sealed.getString("data"), Base64.NO_WRAP)), java.nio.charset.StandardCharsets.UTF_8));
            if (data.getLong("expires") < System.currentTimeMillis() + 60000) {
                Request request = new Request.Builder().url("http://127.0.0.1:8123/auth/token")
                        .post(new FormBody.Builder().add("grant_type", "refresh_token")
                                .add("refresh_token", data.getString("refresh_token")).add("client_id", data.getString("clientId")).build()).build();
                try (Response response = http.newCall(request).execute()) {
                    if (!response.isSuccessful() || response.body() == null) throw new IOException("voice_authorization_expired");
                    JSONObject renewed = new JSONObject(response.body().string());
                    data.put("access_token", renewed.getString("access_token"));
                    data.put("expires", System.currentTimeMillis() + renewed.getLong("expires_in") * 1000);
                    save(data);
                }
            }
            return data.getString("access_token");
        }
    }

    JSONObject call(String route, JSONObject body) throws Exception {
        if (!route.equals("session") && !route.equals("turn")) throw new IOException("invalid_voice_route");
        Request request = new Request.Builder().url("http://127.0.0.1:8123/api/habab_home/realtime/" + route)
                .header("Authorization", "Bearer " + token()).post(RequestBody.create(MediaType.get("application/json"), body.toString())).build();
        try (Response response = http.newCall(request).execute()) {
            if (!response.isSuccessful() || response.body() == null) throw new IOException("voice_http_" + response.code());
            return new JSONObject(response.body().string());
        }
    }
}
