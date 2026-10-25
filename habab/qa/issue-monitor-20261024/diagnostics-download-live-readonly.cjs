'use strict';

// Owner-only diagnostics export acceptance. It uses the real settings panel
// and authenticated export route, but keeps the ZIP in memory and records no
// conversation text, identifiers, credentials, or archive bytes.
const fs = require('node:fs');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const puppeteer = require('../../node_modules/puppeteer-core');
const {withOwnedChromium} = require('/root/tools/chrome-daemon/owned-browser.cjs');

const ORIGIN = process.env.HABAB_DIAGNOSTICS_ORIGIN || 'https://habab-demo.wpmix.net';
const AUTH = '/root/.local/share/habab-owner-test/qa-current-auth.json';
const IDENTITY = '/root/.local/share/habab-owner-test/frontend8-hotfix-runtime-identity.json';
const CLEANUP_REFERENCE = path.join(__dirname, 'provider-panel-after-fixture-cancel.json');
const DIAGNOSTICS_OVERLAY_PATH = '/usr/src/homeassistant/homeassistant/components/habab_home/diagnostics.py';
const OUTPUT = path.join(__dirname, 'diagnostics-download-live-readonly-attempt2.json');
const MAX_ARCHIVE_BYTES = 1_048_576;

const VALIDATE_ZIP = String.raw`
import io, json, re, sys, zipfile
payload = sys.stdin.buffer.read()
expected = {'manifest.json', 'history.json', 'history.txt', 'opencode/availability.json'}
try:
    if not payload or len(payload) > 1048576:
        raise ValueError('archive_size')
    archive = zipfile.ZipFile(io.BytesIO(payload))
    names = set(archive.namelist())
    if names != expected or len(archive.namelist()) != len(expected):
        raise ValueError('archive_entries')
    if archive.testzip() is not None:
        raise ValueError('archive_crc')
    contents = {name: archive.read(name) for name in expected}
    if sum(map(len, contents.values())) > 1048576:
        raise ValueError('expanded_size')
    manifest = json.loads(contents['manifest.json'])
    history = json.loads(contents['history.json'])
    sessions = history.get('sessions')
    limits = manifest.get('limits')
    if not isinstance(sessions, list) or not isinstance(limits, dict):
        raise ValueError('archive_schema')
    event_count = sum(len(item.get('events', [])) for item in sessions if isinstance(item, dict))
    checks = {
        'archive_under_1_mib': len(payload) <= 1048576,
        'expanded_files_under_1_mib': sum(map(len, contents.values())) <= 1048576,
        'session_count_within_five': len(sessions) <= 5 and limits.get('sessions') == 5,
        'event_count_within_two_hundred': event_count <= 200 and limits.get('projection_events') == 200,
        'messages_per_session_within_sixty': limits.get('messages_per_session') == 60,
        'runtime_response_limit_two_megabytes': limits.get('runtime_response_bytes_per_session') == 2000000,
        'archive_limit_one_mib': limits.get('archive_bytes') == 1048576,
        'audio_excluded': manifest.get('audio_included') is False and 'audio' in manifest.get('excluded', []),
        'required_redaction_disclosures': set(manifest.get('redactions_applied', [])) == {
            'configured provider/runtime secrets', 'secret-named fields', 'credential-like string patterns'},
    }
    secret_patterns = [
        re.compile(rb'\bBearer\s+[A-Za-z0-9._~+/-]{12,}', re.I),
        re.compile(rb'\bsk-[A-Za-z0-9_-]{16,}\b'),
        re.compile(rb'\bAIza[A-Za-z0-9_-]{20,}\b'),
        re.compile(rb'[?&](?:oauth[_-]?token|id[_-]?token|access[_-]?token|refresh[_-]?token|client[_-]?secret|api[_-]?key)=([^&#\s]{6,})', re.I),
        re.compile(rb'(?:^|[\s"\'({,;])(?:[A-Z0-9]+[_-]+)*(?:API[_ -]?KEY|PRIVATE[_ -]?KEY|CLIENT[_ -]?SECRET|ACCESS[_ -]?TOKEN|REFRESH[_ -]?TOKEN|SESSION[_ -]?TOKEN|SIGNING[_ -]?KEY|ENCRYPTION[_ -]?KEY|SECRET|TOKEN|PASSWORD|COOKIE|AUTHORIZATION|CREDENTIAL)\b\s*[:=]\s*(?:"[^"]{6,}"|\'[^\']{6,}\'|[^&#\s,;]{6,})', re.I),
    ]
    checks['credential_patterns_absent'] = not any(pattern.search(data) for data in contents.values() for pattern in secret_patterns)
    if not all(checks.values()):
        raise ValueError('archive_policy_check')
    print(json.dumps({'status': 'passed', 'archive_bytes': len(payload),
                      'entries': sorted(names), 'session_count': len(sessions),
                      'event_count': event_count, 'audio_included': False,
                      'checks': checks}, separators=(',', ':')))
except Exception as exc:
    reason = str(exc) if str(exc) in {'archive_size','archive_entries','archive_crc','expanded_size','archive_schema','archive_policy_check'} else 'archive_validation_failed'
    print(json.dumps({'status': 'failed', 'reason': reason}, separators=(',', ':')))
    sys.exit(1)
`;

function safeError(error) {
  return error instanceof Error && /^[A-Za-z0-9_]{1,60}$/.test(error.message)
    ? error.message : 'diagnostics_download_qa_failed';
}

function validateArchive(base64) {
  if (typeof base64 !== 'string' || base64.length > Math.ceil(MAX_ARCHIVE_BYTES * 4 / 3) + 8) {
    throw new Error('archive_size');
  }
  const result = spawnSync('python3', ['-c', VALIDATE_ZIP], {
    input: Buffer.from(base64, 'base64'), encoding: 'utf8', maxBuffer: 16_384, timeout: 15_000,
  });
  if (result.error || result.status !== 0) throw new Error('archive_validation_failed');
  const parsed = JSON.parse(result.stdout);
  if (parsed.status !== 'passed') throw new Error(parsed.reason || 'archive_validation_failed');
  return parsed;
}

async function findPanel(page) {
  return page.evaluateHandle(() => {
    const find = (root) => {
      for (const element of root.querySelectorAll('*')) {
        if (element.localName === 'habab-diagnostics-panel') return element;
        if (element.shadowRoot) {
          const nested = find(element.shadowRoot);
          if (nested) return nested;
        }
      }
      return null;
    };
    return find(document);
  });
}

function isOwnedHaSocket(socketUrl, pageOrigin) {
  try {
    const socket = new URL(socketUrl);
    const page = new URL(pageOrigin);
    const expectedProtocol = page.protocol === 'https:' ? 'wss:' : 'ws:';
    return socket.protocol === expectedProtocol && socket.host === page.host && socket.pathname === '/api/websocket';
  } catch {
    return false;
  }
}

function classifyWebsocketFrame(payloadData) {
  let frame;
  try { frame = JSON.parse(payloadData); } catch { return undefined; }
  if (!frame || typeof frame !== 'object' || typeof frame.type !== 'string') return undefined;
  if (frame.type === 'call_service') {
    const cleanPart = (value) => typeof value === 'string' && /^[A-Za-z0-9_.-]{1,80}$/.test(value) ? value : 'unknown';
    const service = `${cleanPart(frame.domain)}.${cleanPart(frame.service)}`;
    return {kind: service === 'system_log.write' ? 'system_log_write' : 'home_assistant_service', service};
  }
  if (/^habab_home\/(?:agent\/(?:run|abort)|providers\/(?:save|check|check_connection)|setup\/(?:start|submit|cancel)|call_service|config\/.*(?:update|create|delete)|entity_registry\/.*(?:update|create|delete)|xiaomi_setup\/(?!status$|get$).+)$/.test(frame.type)) {
    return {kind: 'habab_write', operation: frame.type.replace(/\d+/g, '{n}')};
  }
  return undefined;
}

async function main() {
  if (fs.existsSync(OUTPUT)) throw new Error('receipt_already_exists');
  const origin = new URL(ORIGIN);
  if (!['https:', 'http:'].includes(origin.protocol) || origin.username || origin.password || origin.search || origin.hash) {
    throw new Error('invalid_origin');
  }
  const authStat = fs.statSync(AUTH);
  if ((authStat.mode & 0o077) !== 0 || Date.now() - authStat.mtimeMs > 20 * 60 * 1000) {
    throw new Error('auth_not_fresh_or_private');
  }
  const auth = JSON.parse(fs.readFileSync(AUTH, 'utf8'));
  if (!auth.access_token || new URL(auth.client_id).origin !== origin.origin) throw new Error('auth_target_mismatch');
  const identity = JSON.parse(fs.readFileSync(IDENTITY, 'utf8'));
  const cleanupReference = JSON.parse(fs.readFileSync(CLEANUP_REFERENCE, 'utf8'));
  const runtimeImage = identity.image;
  const runtimeSource = identity.source;
  const diagnosticsOverlaySha = identity.forward_hotfix_files?.[DIAGNOSTICS_OVERLAY_PATH];
  if (identity.runtime !== 'habab-owner-test' || identity.mode !== 'base_image_plus_forward_hotfix'
      || !/^sha256:[a-f0-9]{64}$/.test(runtimeImage || '') || !/^[a-f0-9]{40}$/.test(runtimeSource || '')
      || !/^[a-f0-9]{64}$/.test(diagnosticsOverlaySha || '')
      || cleanupReference.runtime?.image !== runtimeImage || cleanupReference.runtime?.source !== runtimeSource) {
    throw new Error('runtime_identity_mismatch');
  }

  const report = {
    checked_at: new Date().toISOString(),
    target_route: '/habab-diagnostics',
    runtime_identity: {
      runtime: identity.runtime,
      image: runtimeImage,
      source: runtimeSource,
      home_assistant_version: identity.home_assistant_version,
      frontend_version: identity.frontend_version,
      product_version: identity.product_version,
      mode: identity.mode,
      diagnostics_backend_overlay_sha256: diagnosticsOverlaySha,
      diagnostics_panel_asset_sha256: null,
    },
    verdict: 'IN_PROGRESS',
    admin_verified: false,
    panel_accessible: false,
    selected_export_downloaded_in_memory: false,
    download_verdict: 'NOT_RUN',
    export_requests: 0,
    archive: null,
    no_application_http_writes_except_export: false,
    forbidden_websocket_write_types: [],
    browser_errors: {console: 0, page: 0, failed_requests: 0, http_errors: 0},
    no_archive_saved: true,
    no_model_calls: true,
    no_device_actions: true,
    no_settings_writes: true,
    audio_included: false,
  };
  const httpWrites = [];
  let exportRequests = 0;
  let systemLogWriteCount = 0;
  const websocketWrites = new Set();
  const socketIds = new Set();
  let page;
  let cdp;
  let diagnosticsAssetPromise;
  let failure;

  try {
    await withOwnedChromium(puppeteer, {
      taskTimeoutMs: 150_000,
      defaultViewport: {width: 1280, height: 800},
      args: ['--disable-dev-shm-usage'],
    }, async ({page: ownedPage}) => {
      page = ownedPage;
      try {
      page.setDefaultTimeout(12_000);
      page.on('console', (message) => { if (message.type() === 'error') report.browser_errors.console++; });
      page.on('pageerror', () => { report.browser_errors.page++; });
      page.on('requestfailed', () => { report.browser_errors.failed_requests++; });
      page.on('response', (response) => { if (response.status() >= 400) report.browser_errors.http_errors++; });
      page.on('response', (response) => {
        try {
          if (new URL(response.url()).pathname === '/habab-home/diagnostics-panel.js') {
            diagnosticsAssetPromise = response.buffer().then((bytes) => {
              const digest = require('node:crypto').createHash('sha256').update(bytes).digest('hex');
              return {status: response.status(), sha256: digest};
            });
          }
        } catch {}
      });
      page.on('request', (request) => {
        const url = new URL(request.url());
        const method = request.method().toUpperCase();
        const isExport = url.pathname === '/api/habab_home/diagnostics/export' && method === 'POST';
        const isAuthRefresh = url.pathname === '/auth/token' && method === 'POST';
        if (isExport) exportRequests++;
        if (method !== 'GET' && !isExport && !isAuthRefresh) {
          httpWrites.push(`${method} ${url.pathname}`);
        }
      });

      cdp = await page.target().createCDPSession();
      await cdp.send('Network.enable');
      cdp.on('Network.webSocketCreated', (event) => {
        if (isOwnedHaSocket(event.url, origin.origin)) socketIds.add(event.requestId);
      });
      cdp.on('Network.webSocketFrameSent', (event) => {
        if (!socketIds.has(event.requestId)) return;
        const classified = classifyWebsocketFrame(event.response?.payloadData || '');
        if (classified?.kind === 'system_log_write') systemLogWriteCount++;
        else if (classified?.kind === 'home_assistant_service') websocketWrites.add(`ha_service:${classified.service}`);
        else if (classified?.kind === 'habab_write') websocketWrites.add(classified.operation);
      });
      cdp.on('Network.webSocketClosed', (event) => socketIds.delete(event.requestId));

      await page.evaluateOnNewDocument((tokens, targetOrigin) => {
        localStorage.setItem('hassTokens', JSON.stringify({
          access_token: tokens.access_token,
          refresh_token: tokens.refresh_token,
          token_type: tokens.token_type,
          expires_in: tokens.expires_in,
          clientId: tokens.client_id,
          client_id: tokens.client_id,
          hassUrl: targetOrigin,
          expires: Date.now() + (tokens.expires_in || 3600) * 1000,
        }));
        localStorage.setItem('selectedLanguage', JSON.stringify('ru'));
      }, {
        access_token: auth.access_token,
        refresh_token: auth.refresh_token,
        token_type: auth.token_type,
        expires_in: auth.expires_in,
        client_id: auth.client_id,
      }, origin.origin);

      await page.goto(`${origin.origin}/owner-test`, {waitUntil: 'networkidle2', timeout: 60_000});
      await page.goto(`${origin.origin}/habab-diagnostics`, {waitUntil: 'networkidle2', timeout: 60_000});
      await page.waitForFunction(() => {
        const find = (root) => {
          for (const element of root.querySelectorAll('*')) {
            if (element.localName === 'habab-diagnostics-panel') return element;
            if (element.shadowRoot) { const nested = find(element.shadowRoot); if (nested) return nested; }
          }
          return null;
        };
        const panel = find(document);
        return panel?._hass?.user?.is_admin === true && panel.shadowRoot?.querySelectorAll('.session').length > 0;
      }, {timeout: 60_000});
      const panelHandle = await findPanel(page);
      const panel = panelHandle.asElement();
      if (!panel) throw new Error('diagnostics_panel_not_found');
      if (!diagnosticsAssetPromise) throw new Error('diagnostics_panel_asset_not_observed');
      const servedDiagnosticsAsset = await diagnosticsAssetPromise;
      if (servedDiagnosticsAsset.status !== 200) throw new Error('diagnostics_panel_asset_failed');
      report.runtime_identity.diagnostics_panel_asset_sha256 = servedDiagnosticsAsset.sha256;
      report.admin_verified = await panel.evaluate((element) => element._hass?.user?.is_admin === true);
      report.panel_accessible = await panel.evaluate((element) => Boolean(element.shadowRoot?.querySelector('#download-session')));
      if (!report.admin_verified || !report.panel_accessible) throw new Error('diagnostics_admin_panel_unavailable');

      // Hold the Blob in page memory and suppress the anchor's disk download.
      await page.evaluate(() => {
        window.__hababDiagnosticsZip = undefined;
        window.__hababDiagnosticsFilename = undefined;
        const originalCreate = URL.createObjectURL.bind(URL);
        URL.createObjectURL = (blob) => {
          if (blob instanceof Blob && blob.type.includes('application/zip')) window.__hababDiagnosticsZip = blob;
          return originalCreate(blob);
        };
        HTMLAnchorElement.prototype.click = function() {
          window.__hababDiagnosticsFilename = this.download;
        };
      });
      await panel.evaluate((element) => element.shadowRoot.querySelector('.session')?.click());
      await page.waitForFunction((element) => element.shadowRoot.querySelector('#download-session')?.disabled === false,
        {timeout: 10_000}, panel);
      const buttonLabel = await panel.evaluate((element) => element.shadowRoot.querySelector('#download-session')?.textContent.trim());
      if (buttonLabel !== 'Экспортировать выбранный разговор') throw new Error('download_action_label_mismatch');
      await panel.evaluate((element) => element.shadowRoot.querySelector('#download-session')?.click());
      await page.waitForFunction(() => window.__hababDiagnosticsZip instanceof Blob
        && window.__hababDiagnosticsFilename === 'habab-diagnostics.zip', {timeout: 30_000});
      const archiveBase64 = await page.evaluate(async () => {
        const bytes = new Uint8Array(await window.__hababDiagnosticsZip.arrayBuffer());
        let binary = '';
        for (let offset = 0; offset < bytes.length; offset += 0x8000) {
          binary += String.fromCharCode(...bytes.subarray(offset, Math.min(offset + 0x8000, bytes.length)));
        }
        return btoa(binary);
      });
      report.archive = validateArchive(archiveBase64);
      if (report.archive.session_count !== 1) throw new Error('selected_export_session_count_mismatch');
      report.selected_export_downloaded_in_memory = true;
      report.export_requests = exportRequests;
      report.no_application_http_writes_except_export = httpWrites.length === 0;
      report.forbidden_websocket_write_types = [...websocketWrites].sort();
      report.system_log_write_count = systemLogWriteCount;
      report.no_model_calls = ![...websocketWrites].some((type) => /agent\/(?:run|abort)/.test(type));
      report.no_device_actions = ![...websocketWrites].some((type) => /call_service|xiaomi_setup/.test(type));
      report.no_settings_writes = httpWrites.length === 0 && ![...websocketWrites].some((type) => /config\/|entity_registry|providers\/save|setup\//.test(type));
      if (exportRequests !== 1 || httpWrites.length || websocketWrites.size) throw new Error('unexpected_write_request');
      report.download_verdict = 'PASS_SELECTED_EXPORT_BYTES_VALIDATED_IN_MEMORY';
      if (Object.values(report.browser_errors).some((count) => count !== 0)) throw new Error('browser_errors_observed');
      if (report.archive.status !== 'passed' || !report.selected_export_downloaded_in_memory) throw new Error('archive_acceptance_failed');
      report.verdict = 'PASS_READ_ONLY_LIVE_DIAGNOSTICS_EXPORT';
      await panelHandle.dispose();
      await cdp.detach();
      cdp = undefined;
      } finally {
        // The owned profile is disposable; clear the injected HA session even
        // on an assertion failure before the owned-browser helper closes it.
        try { await page.evaluate(() => { localStorage.clear(); sessionStorage.clear(); }); } catch {}
        if (cdp) { try { await cdp.detach(); } catch {} cdp = undefined; }
      }
    });
  } catch (error) {
    failure = error;
    report.verdict = 'FAIL';
    report.failure = safeError(error);
  } finally {
    if (cdp) { try { await cdp.detach(); } catch {} }
    report.no_application_http_writes_except_export = httpWrites.length === 0;
    report.export_requests = exportRequests;
    report.forbidden_websocket_write_types = [...websocketWrites].sort();
    report.system_log_write_count = systemLogWriteCount;
    report.no_model_calls = ![...websocketWrites].some((type) => /agent\/(?:run|abort)/.test(type));
    report.no_device_actions = ![...websocketWrites].some((type) => /call_service|xiaomi_setup/.test(type));
    report.no_settings_writes = httpWrites.length === 0 && ![...websocketWrites].some((type) => /config\/|entity_registry|providers\/save|setup\//.test(type));
    if (report.download_verdict === 'NOT_RUN' && report.archive?.status === 'passed'
        && report.archive.session_count === 1 && exportRequests === 1 && httpWrites.length === 0 && websocketWrites.size === 0) {
      report.download_verdict = 'PASS_SELECTED_EXPORT_BYTES_VALIDATED_IN_MEMORY';
    }
    report.no_archive_saved = true;
    report.finished_at = new Date().toISOString();
    fs.writeFileSync(OUTPUT, `${JSON.stringify(report, null, 2)}\n`, {flag: 'wx', mode: 0o600});
    process.stdout.write(`${JSON.stringify({report: path.basename(OUTPUT), verdict: report.verdict,
      download_verdict: report.download_verdict,
      admin_verified: report.admin_verified, archive_checks: report.archive?.checks || null,
      archive_bytes: report.archive?.archive_bytes || null, no_archive_saved: report.no_archive_saved,
      no_model_calls: report.no_model_calls, no_device_actions: report.no_device_actions,
      no_settings_writes: report.no_settings_writes})}\n`);
  }
  if (failure) process.exitCode = 1;
}

if (require.main === module) main().catch((error) => {
  process.stderr.write(`${safeError(error)}\n`);
  process.exitCode = 1;
});

module.exports = {validateArchive, isOwnedHaSocket, classifyWebsocketFrame};
