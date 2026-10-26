package ru.habab.home;

import android.app.*;
import android.content.*;
import android.net.Uri;
import android.os.*;
import android.provider.Settings;
import android.text.InputType;
import android.view.*;
import android.webkit.*;
import android.widget.*;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.*;
import java.util.concurrent.*;

public final class MainActivity extends Activity {
    static final String EXTRA_OPEN_HOME_IF_READY = "ru.habab.home.extra.OPEN_HOME_IF_READY";
    private final ExecutorService operations = Executors.newSingleThreadExecutor();
    private final Handler handler = new Handler(Looper.getMainLooper());
    private boolean busy;
    private boolean launcherStartRequested;
    private boolean autoStartServerRequested;
    private boolean autoStartVoiceRequested;
    private TextView state;
    private LinearLayout content;
    private WebView browser;
    private RuntimeStore store;
    private final Runnable refresh = new Runnable() {
        @Override public void run() {
            if (launcherStartRequested) ensureServerStarted();
            maybeStartVoiceIfReady();
            if (state != null && !busy) state.setText(getString(R.string.status, phaseText())
                    + "\nHTTPS: " + tunnelText() + "\n" + MaintenanceService.status);
            handler.postDelayed(this, 2000);
        }
    };

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved); store = new RuntimeStore(this); home(); handler.post(refresh);
        if (saved == null && beginLauncherStartup(getIntent())) ensureServerStarted();
        maybeStartVoiceIfReady();
        // ADB provisioning is available only in a debug build, never the release APK.
        if (openAlias(getIntent())) return;
        if (openHomeFromNotification(getIntent())) return;
        if (BuildConfig.DEBUG && getIntent().hasExtra("install_url")) install(getIntent().getStringExtra("install_url"));
        else if (BuildConfig.DEBUG && getIntent().getBooleanExtra("start_server", false)) startServer();
        else if (BuildConfig.DEBUG && getIntent().getBooleanExtra("provision_voice", false)) provisionVoice();
    }

    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent); setIntent(intent);
        if (beginLauncherStartup(intent)) ensureServerStarted();
        maybeStartVoiceIfReady();
        if (openAlias(intent)) return;
        openHomeFromNotification(intent);
    }

    @Override protected void onResume() {
        super.onResume();
        if (launcherStartRequested) ensureServerStarted();
        maybeStartVoiceIfReady();
    }

    private boolean openHomeFromNotification(Intent intent) {
        if (!intent.getBooleanExtra(EXTRA_OPEN_HOME_IF_READY, false)
                || !"running".equals(ServerService.status)) return false;
        openHome();
        return true;
    }

    private boolean openAlias(Intent intent) {
        if (!Intent.ACTION_VIEW.equals(intent.getAction())) return false;
        String address = LocalAddress.resolve(intent.getDataString());
        if (address == null) return false;
        openHome(address); return true;
    }

    private boolean beginLauncherStartup(Intent intent) {
        if (intent == null || !Intent.ACTION_MAIN.equals(intent.getAction())
                || !intent.hasCategory(Intent.CATEGORY_LAUNCHER)) return false;
        getSharedPreferences("server", 0).edit()
                .putBoolean(ServerService.PREF_AUTO_START_SUPPRESSED, false)
                .putBoolean(ServerService.PREF_AUTO_START_ATTEMPTED, false).apply();
        launcherStartRequested = true;
        autoStartServerRequested = false;
        autoStartVoiceRequested = false;
        return true;
    }

    private TextView text(String value) {
        TextView view = new TextView(this); view.setText(value); view.setTextSize(16);
        view.setPadding(0, 8, 0, 8); view.setTextIsSelectable(true); content.addView(view); return view;
    }
    private void button(int label, Runnable action) {
        Button button = new Button(this); button.setText(label); button.setAllCaps(false);
        button.setOnClickListener(view -> action.run()); content.addView(button);
    }

    private void home() {
        if (browser != null) { browser.destroy(); browser = null; }
        ScrollView scroll = new ScrollView(this); content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL); content.setPadding(24, 24, 24, 24);
        scroll.addView(content); setContentView(scroll);
        TextView title = text(getString(R.string.app_name)); title.setTextSize(28);
        state = text(getString(R.string.status, phaseText()));
        text(getString(R.string.local_url));
        String lan = lanAddresses();
        if (!lan.isEmpty()) text(getString(R.string.lan_url, lan));
        String external = getSharedPreferences("server", 0).getString("public_url", "");
        if (!external.isEmpty()) text(external);
        button(R.string.setup, this::installDialog);
        button(R.string.start, this::startServerManually);
        CheckBox autoStartOnBoot = new CheckBox(this);
        autoStartOnBoot.setText(R.string.server_auto_start_on_boot);
        autoStartOnBoot.setChecked(ServerService.autoStartOnBoot(this));
        autoStartOnBoot.setOnCheckedChangeListener((button, checked) -> getSharedPreferences("server", 0).edit()
                .putBoolean(ServerService.PREF_AUTO_START_ON_BOOT, checked).apply());
        content.addView(autoStartOnBoot);
        button(R.string.open_home, this::openHome);
        button(R.string.voice_title, this::voiceSettings);
        button(R.string.stop, () -> {
            getSharedPreferences("server", 0).edit().putBoolean("desired", false)
                    .putBoolean(ServerService.PREF_AUTO_START_SUPPRESSED, true)
                    .putBoolean(ServerService.PREF_AUTO_START_ATTEMPTED, true).apply();
            VoiceService.cancelPendingStart();
            autoStartServerRequested = true;
            stopService(new Intent(this, VoiceService.class));
            stopService(new Intent(this, ServerService.class));
        });
        button(R.string.battery, () -> startActivity(new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:" + getPackageName()))));
        button(R.string.tunnel, this::tunnelDialog);
        button(R.string.backup, () -> {
            if (!stopped()) return;
            Intent intent = new Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
                    .setType("application/octet-stream").putExtra(Intent.EXTRA_TITLE, "habab-home-" + System.currentTimeMillis() + ".habak");
            startActivityForResult(intent, 10);
        });
        button(R.string.restore, () -> {
            if (!stopped()) return;
            startActivityForResult(new Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("*/*"), 11);
        });
        button(R.string.logs, this::showLogs); text(getString(R.string.preview));
    }

    private boolean stopped() {
        if (busy || ServerService.active || ServerService.stopping || MaintenanceService.active) { message(getString(R.string.stop_first)); return false; }
        return true;
    }

    private void startServer() {
        if (busy || MaintenanceService.active || ServerService.stopping) return;
        if (!store.installed()) { message(getString(R.string.not_installed)); return; }
        getSharedPreferences("server", 0).edit().putBoolean("desired", true).apply();
        startForegroundService(new Intent(this, ServerService.class));
    }

    private void startServerManually() {
        getSharedPreferences("server", 0).edit()
                .putBoolean(ServerService.PREF_AUTO_START_SUPPRESSED, false)
                .putBoolean(ServerService.PREF_AUTO_START_ATTEMPTED, false).apply();
        launcherStartRequested = true;
        autoStartServerRequested = false;
        autoStartVoiceRequested = false;
        ensureServerStarted();
    }

    private void installDialog() {
        if (!stopped()) return;
        EditText address = new EditText(this); address.setHint(R.string.manifest_url);
        address.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        String defaultUrl = getSharedPreferences("server", 0).getString("manifest_url", "");
        if (defaultUrl.isEmpty()) {
            try (InputStream input = getAssets().open("release/default-url.txt")) {
                defaultUrl = new String(RuntimeStore.readLimited(input, 8192), StandardCharsets.UTF_8).trim();
            } catch (IOException ignored) { /* User can enter their own signed release endpoint. */ }
        }
        address.setText(defaultUrl);
        new AlertDialog.Builder(this).setTitle(R.string.install_title).setView(address)
                .setPositiveButton(R.string.setup, (dialog, which) -> install(address.getText().toString().trim()))
                .setNegativeButton(R.string.cancel, null).show();
    }

    private interface Work { void run() throws Exception; }
    private void work(Work operation) {
        if (busy) return;
        busy = true;
        if (state != null) state.setText(R.string.working);
        operations.execute(() -> {
            try { operation.run(); handler.post(() -> message(getString(R.string.done))); }
            catch (Exception error) { handler.post(() -> message(getString(R.string.error, error.getMessage()))); }
            finally { handler.post(() -> { busy = false; home(); }); }
        });
    }

    private void install(String address) {
        if (!stopped()) return;
        MaintenanceService.start(this, () -> {
            store.install(address, progress -> MaintenanceService.status = progress);
            getSharedPreferences("server", 0).edit().putString("manifest_url", address).apply();
        });
    }

    private void openHome() {
        openHome("http://127.0.0.1:8123");
    }

    private void openHome(String address) {
        if (busy) return;
        if (browser != null) { browser.destroy(); browser = null; }
        LinearLayout layout = new LinearLayout(this); layout.setOrientation(LinearLayout.VERTICAL);
        Button back = new Button(this); back.setText(R.string.back); back.setOnClickListener(view -> home()); layout.addView(back);
        Button voice = new Button(this); voice.setText(R.string.voice_authorize);
        voice.setOnClickListener(view -> authorizeVoice()); layout.addView(voice);
        browser = new WebView(this);
        browser.getSettings().setJavaScriptEnabled(true); browser.getSettings().setDomStorageEnabled(true);
        browser.getSettings().setAllowFileAccess(false); browser.getSettings().setAllowContentAccess(false);
        browser.getSettings().setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        browser.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if ("http".equals(uri.getScheme()) && "127.0.0.1".equals(uri.getHost()) && uri.getPort() == 8123) return false;
                try { startActivity(new Intent(Intent.ACTION_VIEW, uri)); }
                catch (ActivityNotFoundException error) { message(getString(R.string.no_link_handler)); }
                return true;
            }
        });
        layout.addView(browser, new LinearLayout.LayoutParams(-1, 0, 1)); setContentView(layout);
        state = null; browser.loadUrl(address);
    }

    private void voiceSettings() {
        if (!new VoiceCredentials(this).configured()) {
            new AlertDialog.Builder(this).setTitle(R.string.voice_title).setMessage(R.string.voice_login_help)
                    .setPositiveButton(R.string.open_home, (dialog, which) -> openHome())
                    .setNegativeButton(R.string.cancel, null).show();
            return;
        }
        LinearLayout form = new LinearLayout(this); form.setOrientation(LinearLayout.VERTICAL); form.setPadding(28, 8, 28, 8);
        TextView explanation = new TextView(this); explanation.setText(R.string.voice_privacy); form.addView(explanation);
        TextView phase = new TextView(this); phase.setText(VoiceService.label(this, VoiceService.status)); form.addView(phase);
        TextView routeHelp = new TextView(this); routeHelp.setText(R.string.voice_audio_route_help); form.addView(routeHelp);
        String routeMode = getSharedPreferences("voice", 0).getString("audio_route_mode",
                getSharedPreferences("voice", 0).getBoolean("bluetooth_audio", false) ? "bluetooth" : "phone");
        if (!"bluetooth".equals(routeMode) && !"duplicate".equals(routeMode)) routeMode = "phone";
        RadioGroup audioRouteGroup = new RadioGroup(this); audioRouteGroup.setOrientation(LinearLayout.VERTICAL);
        addVoiceRouteOption(audioRouteGroup, "phone", R.string.voice_route_phone);
        addVoiceRouteOption(audioRouteGroup, "bluetooth", R.string.voice_route_bluetooth);
        addVoiceRouteOption(audioRouteGroup, "duplicate", R.string.voice_route_duplicate);
        for (int i = 0; i < audioRouteGroup.getChildCount(); i++) {
            View child = audioRouteGroup.getChildAt(i);
            if (routeMode.equals(child.getTag())) audioRouteGroup.check(child.getId());
        }
        form.addView(audioRouteGroup);
        TextView routeStatus = new TextView(this); routeStatus.setText(voiceAudioStatus()); form.addView(routeStatus);
        if (VoiceService.lastError != null) {
            TextView error = new TextView(this); error.setText(VoiceService.failureLabel(this, VoiceService.lastError)); form.addView(error);
        }
        EditText phrase = new EditText(this); phrase.setHint(R.string.voice_phrase);
        phrase.setText(getSharedPreferences("voice", 0).getString("phrase", "эй дом"));
        phrase.setFilters(new android.text.InputFilter[]{new android.text.InputFilter.LengthFilter(64)}); form.addView(phrase);
        CheckBox enabled = new CheckBox(this); enabled.setText(R.string.voice_enable);
        enabled.setChecked(getSharedPreferences("voice", 0).getBoolean("enabled", false)); form.addView(enabled);
        AlertDialog voiceDialog = new AlertDialog.Builder(this).setTitle(R.string.voice_title).setView(form)
                .setPositiveButton(R.string.configure, (dialog, which) -> {
                    String value = WakeDetector.normalize(phrase.getText().toString());
                    if (value.isEmpty()) { message(getString(R.string.voice_invalid_phrase)); return; }
                    getSharedPreferences("voice", 0).edit().putString("phrase", value)
                            .putBoolean("enabled", enabled.isChecked())
                            .putString("audio_route_mode", audioRouteGroup.findViewById(audioRouteGroup.getCheckedRadioButtonId()).getTag().toString())
                            .remove("bluetooth_audio").apply();
                    if (enabled.isChecked()) {
                        startServerManually();
                        VoiceService.restartAfterConfiguration(this);
                        if (checkSelfPermission(android.Manifest.permission.RECORD_AUDIO)
                                != android.content.pm.PackageManager.PERMISSION_GRANTED) {
                            requestPermissions(new String[]{android.Manifest.permission.RECORD_AUDIO}, 20);
                        }
                    } else {
                        VoiceService.cancelPendingStart();
                        stopService(new Intent(this, VoiceService.class));
                    }
                }).setNegativeButton(R.string.cancel, null).create();
        Runnable updateRouteStatus = new Runnable() {
            @Override public void run() {
                if (!voiceDialog.isShowing()) return;
                routeStatus.setText(voiceAudioStatus());
                handler.postDelayed(this, 1000);
            }
        };
        voiceDialog.setOnShowListener(ignored -> handler.post(updateRouteStatus));
        voiceDialog.setOnDismissListener(ignored -> handler.removeCallbacks(updateRouteStatus));
        voiceDialog.show();
    }

    private void authorizeVoice() {
        if (browser == null || browser.getUrl() == null) return;
        Uri uri = Uri.parse(browser.getUrl());
        if (!"http".equals(uri.getScheme()) || !"127.0.0.1".equals(uri.getHost()) || uri.getPort() != 8123) return;
        browser.evaluateJavascript("localStorage.getItem('hassTokens')", value -> {
            try {
                Object decoded = new org.json.JSONTokener(value).nextValue();
                if (!(decoded instanceof String)) { message(getString(R.string.voice_login_help)); return; }
                org.json.JSONObject auth = new org.json.JSONObject((String) decoded);
                operations.execute(() -> {
                    try { new VoiceCredentials(this).save(auth); handler.post(this::voiceSettings); }
                    catch (Exception error) { handler.post(() -> message(getString(R.string.voice_auth_failed))); }
                });
            } catch (Exception error) { message(getString(R.string.voice_login_help)); }
        });
    }

    private void startVoice() {
        if (checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{android.Manifest.permission.RECORD_AUDIO}, 20); return;
        }
        boolean enabled = getSharedPreferences("voice", 0).getBoolean("enabled", false);
        boolean authConfigured = getSharedPreferences("voice_auth", 0).contains("sealed");
        boolean microphoneGranted = checkSelfPermission(android.Manifest.permission.RECORD_AUDIO)
                == android.content.pm.PackageManager.PERMISSION_GRANTED;
        boolean manuallyStopped = getSharedPreferences("server", 0)
                .getBoolean(ServerService.PREF_AUTO_START_SUPPRESSED, false);
        if (!ServerService.voiceAutoStartAllowed(enabled, authConfigured, microphoneGranted, manuallyStopped)) return;
        launcherStartRequested = true;
        ensureServerStarted();
        maybeStartVoiceIfReady();
    }

    private void ensureServerStarted() {
        SharedPreferences preferences = getSharedPreferences("server", 0);
        boolean manuallyStopped = preferences.getBoolean(ServerService.PREF_AUTO_START_SUPPRESSED, false);
        boolean runtimeInstalled = store.installed();
        if (!ServerService.serverStartAllowed(launcherStartRequested, runtimeInstalled, manuallyStopped)) {
            if (launcherStartRequested && !manuallyStopped && !runtimeInstalled
                    && !preferences.getBoolean(ServerService.PREF_AUTO_START_ATTEMPTED, false)) {
                autoStartServerRequested = true;
                preferences.edit().putBoolean(ServerService.PREF_AUTO_START_ATTEMPTED, true).apply();
                message(getString(R.string.not_installed));
            }
            return;
        }
        if (ServerService.active || ServerService.stopping || MaintenanceService.active
                || busy || autoStartServerRequested
                || preferences.getBoolean(ServerService.PREF_AUTO_START_ATTEMPTED, false)) return;
        autoStartServerRequested = true;
        preferences.edit().putBoolean(ServerService.PREF_AUTO_START_ATTEMPTED, true).apply();
        try { startServer(); }
        catch (RuntimeException error) {
            message(getString(R.string.error, error.getMessage() == null
                    ? error.getClass().getSimpleName() : error.getMessage()));
        }
    }

    private void maybeStartVoiceIfReady() {
        if (autoStartVoiceRequested) return;
        boolean enabled = getSharedPreferences("voice", 0).getBoolean("enabled", false);
        boolean authConfigured = getSharedPreferences("voice_auth", 0).contains("sealed");
        boolean microphoneGranted = checkSelfPermission(android.Manifest.permission.RECORD_AUDIO)
                == android.content.pm.PackageManager.PERMISSION_GRANTED;
        boolean serverReady = ServerService.active && "running".equals(ServerService.status);
        boolean manuallyStopped = getSharedPreferences("server", 0)
                .getBoolean(ServerService.PREF_AUTO_START_SUPPRESSED, false);
        if (VoiceService.active || !ServerService.voiceStartAllowed(enabled, authConfigured,
                microphoneGranted, serverReady, manuallyStopped)) return;
        autoStartVoiceRequested = true;
        ServerService.startVoiceIfReady(this);
    }

    private String voiceAudioStatus() {
        String selectedMode = getSharedPreferences("voice", 0).getString("audio_route_mode",
                getSharedPreferences("voice", 0).getBoolean("bluetooth_audio", false) ? "bluetooth" : "phone");
        int modeString = "duplicate".equals(selectedMode) ? R.string.voice_route_duplicate
                : "bluetooth".equals(selectedMode) ? R.string.voice_route_bluetooth : R.string.voice_route_phone;
        String mode = getString(modeString);
        try {
            File file = new File(getFilesDir(), "voice-status.json");
            if (file.isFile()) {
                org.json.JSONObject audio = new org.json.JSONObject(new String(
                        Files.readAllBytes(file.toPath()), StandardCharsets.UTF_8)).optJSONObject("audio");
                if (audio != null) {
                    String active = audio.optString("activeRoute", "");
                    String input = audio.optString("inputDeviceName", "");
                    String output = audio.optString("outputDeviceName", "");
                    if ("bluetooth_unavailable".equals(active)) active = getString(R.string.voice_bluetooth_unavailable);
                    return getString(R.string.voice_audio_active_status, mode,
                            input.isEmpty() ? getString(R.string.voice_audio_unknown) : input,
                            output.isEmpty() ? getString(R.string.voice_audio_unknown) : output,
                            active.isEmpty() ? getString(R.string.voice_audio_unknown) : active);
                }
            }
        } catch (Exception ignored) { /* Keep the saved mode visible if runtime status is unavailable. */ }
        return getString(R.string.voice_audio_route_status, mode);
    }

    private void addVoiceRouteOption(RadioGroup group, String route, int label) {
        RadioButton option = new RadioButton(this); option.setText(label); option.setTag(route);
        option.setId(View.generateViewId()); group.addView(option);
    }

    @Override public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(requestCode, permissions, results);
        if (requestCode == 20 && results.length > 0 && results[0] == android.content.pm.PackageManager.PERMISSION_GRANTED)
            startVoice();
    }

    private void provisionVoice() {
        work(() -> {
            File file = new File(getFilesDir(), "voice-provision.json");
            try {
                org.json.JSONObject data = new org.json.JSONObject(new String(Files.readAllBytes(file.toPath()), StandardCharsets.UTF_8));
                new VoiceCredentials(this).save(data);
                boolean enabled = data.optBoolean("enable_voice", false);
                getSharedPreferences("voice", 0).edit().putString("phrase", "эй дом").putBoolean("enabled", enabled).commit();
                if (enabled) handler.post(this::startVoice);
            } finally { Files.deleteIfExists(file.toPath()); }
        });
    }

    private void showLogs() {
        File file = new File(getFilesDir(), "core.log");
        String data = "";
        if (file.isFile()) try (RandomAccessFile input = new RandomAccessFile(file, "r")) {
            input.seek(Math.max(0, input.length() - 64000)); byte[] buffer = new byte[(int)(input.length() - input.getFilePointer())];
            input.readFully(buffer); data = new String(buffer, StandardCharsets.UTF_8);
        } catch (IOException error) { data = error.getMessage(); }
        TextView view = new TextView(this);
        view.setText(voiceLifecycleLog() + "\n\n" + data);
        view.setTextIsSelectable(true); view.setTextSize(12);
        ScrollView scroll = new ScrollView(this); scroll.addView(view);
        new AlertDialog.Builder(this).setTitle(R.string.logs).setView(scroll).setPositiveButton(android.R.string.ok, null).show();
    }

    private String voiceLifecycleLog() {
        String title = getString(R.string.voice_lifecycle_title);
        File trace = new File(getFilesDir(), "voice-lifecycle.json");
        if (!trace.isFile() || trace.length() > 16384) return title + "\n" + getString(R.string.voice_lifecycle_unavailable);
        try {
            String json = new String(Files.readAllBytes(trace.toPath()), StandardCharsets.UTF_8);
            org.json.JSONArray events = new org.json.JSONArray(json);
            StringBuilder lines = new StringBuilder(title);
            int start = Math.max(0, events.length() - 32);
            for (int i = start; i < events.length(); i++) {
                org.json.JSONObject event = events.optJSONObject(i);
                if (event == null) continue;
                String name = event.optString("event", "");
                String reason = event.optString("reason", "");
                try {
                    VoiceLifecycleTrace.Event.valueOf(name);
                    VoiceLifecycleTrace.Reason.valueOf(reason);
                } catch (IllegalArgumentException invalidCode) { continue; }
                int route = event.optInt("route", -1);
                String routeName = route == 0 ? getString(R.string.voice_route_phone)
                        : route == 1 ? getString(R.string.voice_route_bluetooth)
                        : route == 2 ? getString(R.string.voice_route_duplicate)
                        : getString(R.string.voice_audio_unknown);
                long generation = event.optLong("generation", -1);
                long at = event.optLong("at", 0);
                lines.append('\n').append(name).append(" / ").append(reason)
                        .append(" #").append(Math.max(0, generation)).append(" / ").append(routeName);
                if (at > 0) lines.append(" / ").append(at);
            }
            return lines.toString();
        } catch (Exception ignored) {
            return title + "\n" + getString(R.string.voice_lifecycle_unavailable);
        }
    }

    private void tunnelDialog() {
        if (!stopped()) return;
        work(() -> {
            String publicKey = new Tunnel(this).publicKey();
            handler.post(() -> showTunnelSettings(publicKey));
        });
    }

    private void showTunnelSettings(String publicKey) {
        SharedPreferences prefs = getSharedPreferences("server", 0);
        LinearLayout form = new LinearLayout(this); form.setOrientation(LinearLayout.VERTICAL); form.setPadding(20, 8, 20, 8);
        TextView explanation = new TextView(this); explanation.setText(R.string.tunnel_onboarding); form.addView(explanation);
        TextView key = new TextView(this); key.setText(getString(R.string.public_key) + "\n" + publicKey); key.setTextIsSelectable(true); form.addView(key);
        String[] names = {"ssh_host", "ssh_port", "ssh_user", "remote_port", "ssh_pin", "public_url"};
        int[] hints = {R.string.tunnel_host, R.string.tunnel_port, R.string.tunnel_user, R.string.tunnel_remote, R.string.tunnel_known_host, R.string.tunnel_url};
        EditText[] fields = new EditText[names.length];
        for (int i = 0; i < names.length; i++) {
            fields[i] = new EditText(this); fields[i].setHint(hints[i]); fields[i].setSingleLine(true);
            String value = (i == 1 || i == 3) ? String.valueOf(prefs.getInt(names[i], i == 1 ? 22 : 18123)) : prefs.getString(names[i], "");
            fields[i].setText(value); form.addView(fields[i]);
        }
        CheckBox enabled = new CheckBox(this); enabled.setText(R.string.tunnel); enabled.setChecked(prefs.getBoolean("tunnel_enabled", false)); form.addView(enabled);
        ScrollView scroll = new ScrollView(this); scroll.addView(form);
        AlertDialog dialog = new AlertDialog.Builder(this).setTitle(R.string.tunnel).setView(scroll)
                .setPositiveButton(R.string.configure, null).setNegativeButton(R.string.cancel, null).create();
        dialog.setOnShowListener(ignored -> dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(view -> {
            try {
                if (enabled.isChecked()) Tunnel.validate(fields[0].getText().toString().trim(), fields[1].getText().toString().trim(), fields[2].getText().toString().trim(), fields[3].getText().toString().trim(), fields[4].getText().toString().trim(), fields[5].getText().toString().trim());
                SharedPreferences.Editor edit = prefs.edit().putBoolean("tunnel_enabled", enabled.isChecked());
                for (int i = 0; i < names.length; i++) {
                    String value = fields[i].getText().toString().trim();
                    if (i == 1 || i == 3) edit.putInt(names[i], Integer.parseInt(value)); else edit.putString(names[i], value);
                }
                edit.apply(); dialog.dismiss(); home(); message(getString(R.string.tunnel_saved));
            } catch (Exception error) { message(getString(R.string.error, error.getMessage())); }
        })); dialog.show();
    }

    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if ((request != 10 && request != 11) || result != RESULT_OK || data == null || data.getData() == null) return;
        Uri uri = data.getData();
        EditText password = new EditText(this);
        password.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        new AlertDialog.Builder(this).setTitle(request == 10 ? R.string.backup : R.string.restore)
                .setMessage(R.string.backup_password)
                .setView(password).setPositiveButton(android.R.string.ok, (dialog, which) -> {
                    char[] secret = password.getText().toString().toCharArray(); password.setText("");
                    if (secret.length < 12) { Arrays.fill(secret, '\0'); message(getString(R.string.short_password)); return; }
                    MaintenanceService.start(this, () -> { try { if (request == 10) store.backup(uri, secret); else store.restore(uri, secret); } finally { Arrays.fill(secret, '\0'); } });
                }).setNegativeButton(R.string.cancel, null).show();
    }

    private String lanAddresses() {
        List<String> addresses = new ArrayList<>();
        android.net.ConnectivityManager manager = getSystemService(android.net.ConnectivityManager.class);
        for (android.net.Network network : manager.getAllNetworks()) {
            android.net.NetworkCapabilities caps = manager.getNetworkCapabilities(network);
            android.net.LinkProperties links = manager.getLinkProperties(network);
            if (caps == null || links == null || !caps.hasTransport(android.net.NetworkCapabilities.TRANSPORT_WIFI)) continue;
            for (android.net.LinkAddress link : links.getLinkAddresses()) {
                InetAddress value = link.getAddress();
                if (value instanceof Inet4Address && value.isSiteLocalAddress()) addresses.add("http://" + value.getHostAddress() + ":8123");
            }
        }
        return String.join("\n", addresses);
    }
    private void message(String value) { new AlertDialog.Builder(this).setMessage(value).setPositiveButton(android.R.string.ok, null).show(); }
    private String phaseText() {
        String phase = ServerService.status;
        if (phase.equals("running")) return getString(R.string.phase_running);
        if (phase.equals("starting")) return getString(R.string.phase_starting);
        if (phase.equals("stopping")) return getString(R.string.phase_stopping);
        if (phase.equals("stopped")) return getString(R.string.stopped);
        if (phase.startsWith("restarting")) return getString(R.string.phase_restarting);
        if (phase.startsWith("startup-taking")) return getString(R.string.phase_long_start);
        return phase;
    }
    private String tunnelText() {
        String phase = ServerService.tunnelStatus;
        if (phase.equals("connected")) return getString(R.string.tunnel_connected);
        if (phase.equals("waiting-for-owner")) return getString(R.string.tunnel_waiting);
        if (phase.startsWith("disconnected")) return getString(R.string.tunnel_disconnected);
        return getString(R.string.tunnel_disabled);
    }
    @Override public void onBackPressed() { if (browser != null) home(); else super.onBackPressed(); }
    @Override protected void onDestroy() {
        handler.removeCallbacks(refresh);
        operations.shutdown(); if (browser != null) browser.destroy(); super.onDestroy();
    }
}
