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
final class VoiceCredentials implements AutoCloseable {
    private static final String ALIAS = "habab.voice.authorization.v1";
    private static final Object LOCK = new Object();
    private static final Object STORAGE_LOCK = new Object();
    private static long storageGeneration;
    private final Context context;
    private final VoiceRequestGate requests = new VoiceRequestGate();
    private final VoiceRequestGate interactions = new VoiceRequestGate();
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

    private String seal(JSONObject source) throws Exception {
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
        return sealed.toString();
    }

    void save(JSONObject source) throws Exception {
        synchronized (LOCK) {
            String sealed = seal(source);
            synchronized (STORAGE_LOCK) { writeSealed(sealed, null); }
        }
    }

    boolean saveIfCurrent(JSONObject source, VoiceAuthLink link, VoiceAuthLink.Ticket ticket) throws Exception {
        synchronized (LOCK) {
            if (!link.current(ticket)) return false;
            String sealed = seal(source);
            return link.commit(ticket, () -> {
                synchronized (STORAGE_LOCK) { writeSealed(sealed, ticket.identity); }
            });
        }
    }

    private void writeSealed(String sealed, String identity) throws IOException {
        android.content.SharedPreferences.Editor editor = context.getSharedPreferences("voice_auth", 0)
                .edit().putString("sealed", sealed);
        if (identity != null) editor.putString("grant_identity", identity);
        if (!editor.commit()) throw new IOException("voice_authorization_not_saved");
        storageGeneration++;
    }

    // Revocation must not wait for a token refresh holding LOCK across a network request.
    void clear() {
        synchronized (STORAGE_LOCK) {
            storageGeneration++;
            context.getSharedPreferences("voice_auth", 0).edit().remove("sealed").remove("grant_identity").apply();
        }
    }

    private String token(VoiceRequestGate scope) throws Exception {
        synchronized (LOCK) {
            String value;
            long generation;
            synchronized (STORAGE_LOCK) {
                generation = storageGeneration;
                value = context.getSharedPreferences("voice_auth", 0).getString("sealed", null);
            }
            if (value == null) throw new IOException("voice_authorization_required");
            JSONObject sealed = new JSONObject(value);
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(128, Base64.decode(sealed.getString("iv"), Base64.NO_WRAP)));
            JSONObject data = new JSONObject(new String(cipher.doFinal(Base64.decode(sealed.getString("data"), Base64.NO_WRAP)), java.nio.charset.StandardCharsets.UTF_8));
            if (data.getLong("expires") < System.currentTimeMillis() + 60000) {
                Request request = new Request.Builder().url("http://127.0.0.1:8123/auth/token")
                        .post(new FormBody.Builder().add("grant_type", "refresh_token")
                                .add("refresh_token", data.getString("refresh_token")).add("client_id", data.getString("clientId")).build()).build();
                Call call = http.newCall(request);
                try (VoiceRequestGate.Ticket ticket = requests.register(call::cancel);
                     VoiceRequestGate.Ticket interaction = scope == null ? null : scope.register(call::cancel)) {
                    if (ticket == null || scope != null && interaction == null) throw new VoiceFailure("voice_session_ended");
                    try (Response response = call.execute()) {
                        if (!response.isSuccessful() || response.body() == null) throw new IOException("voice_authorization_expired");
                        JSONObject renewed = new JSONObject(response.body().string());
                        data.put("access_token", renewed.getString("access_token"));
                        data.put("expires", System.currentTimeMillis() + renewed.getLong("expires_in") * 1000);
                        String updated = seal(data);
                        synchronized (STORAGE_LOCK) {
                            if (generation != storageGeneration) throw new IOException("voice_authorization_expired");
                            writeSealed(updated, null);
                            generation = storageGeneration;
                        }
                    }
                }
            }
            synchronized (STORAGE_LOCK) {
                if (generation != storageGeneration) throw new IOException("voice_authorization_expired");
                return data.getString("access_token");
            }
        }
    }

    JSONObject call(String route, JSONObject body) throws Exception {
        if (!route.equals("session") && !route.equals("turn") && !route.equals("context")
                && !route.equals("transcript") && !route.equals("native_voice_route"))
            throw new IOException("invalid_voice_route");
        VoiceRequestGate scope = route.equals("transcript") || route.equals("native_voice_route") ? null : interactions;
        String path = route.equals("native_voice_route") ? route : "realtime/" + route;
        Request request = new Request.Builder().url("http://127.0.0.1:8123/api/habab_home/" + path)
                .header("Authorization", "Bearer " + token(scope)).post(RequestBody.create(MediaType.get("application/json"), body.toString())).build();
        Call call = http.newCall(request);
        try (VoiceRequestGate.Ticket ticket = requests.register(call::cancel);
             VoiceRequestGate.Ticket interaction = scope == null ? null : scope.register(call::cancel)) {
            if (ticket == null || scope != null && interaction == null) throw new VoiceFailure("voice_session_ended");
            try (Response response = call.execute()) {
                if (!response.isSuccessful()) {
                    String code = "";
                    try { code = new JSONObject(response.peekBody(8192).string()).optString("message", ""); }
                    catch (org.json.JSONException ignored) { /* Never expose an upstream body. */ }
                    throw VoiceFailure.fromHttp(response.code(), code);
                }
                if (response.body() == null) throw new VoiceFailure("invalid_realtime_response");
                return new JSONObject(response.body().string());
            }
        }
    }

    void cancelInteractions() { interactions.close(); }

    @Override public void close() {
        interactions.close();
        requests.close(); http.dispatcher().cancelAll(); http.connectionPool().evictAll();
    }
}
