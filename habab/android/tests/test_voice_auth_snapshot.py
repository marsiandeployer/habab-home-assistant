"""Execute the exact native WebView snapshot against HA's actual token-storage sequence."""
import json
from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[3]


class VoiceAuthSnapshotTest(unittest.TestCase):
    def test_same_webview_logout_reload_and_login(self):
        source = (ROOT / "habab/android/app/src/main/java/ru/habab/home/VoiceAuthLink.java").read_text()
        snapshot = re.search(r'SNAPSHOT_SCRIPT = """\n(.*?)\n\s*""";', source, re.S).group(1)
        storage_path = ROOT / "src/common/auth/token_storage.ts"
        storage = storage_path.read_text() if storage_path.exists() else subprocess.check_output(
            ["git", "show", "HEAD:src/common/auth/token_storage.ts"], cwd=ROOT, text=True
        )
        save_tokens = re.search(
            r"export function saveTokens\(tokens: AuthData \| null\) \{(.*?)\n\}", storage, re.S
        ).group(1).replace("catch (err: any)", "catch (err)")
        script = r"""
const vm = require('node:vm');
const config = JSON.parse(process.argv[1]);
const store = new Map();
const localStorage = {getItem: key => store.has(key) ? store.get(key) : null,
  setItem: (key, value) => store.set(key, value)};
const location = {origin: 'http://127.0.0.1:8123', protocol: 'http:', host: '127.0.0.1:8123', href: '/'};
let element = null;
const context = vm.createContext({window: {localStorage}, localStorage, location, URL,
  document: {querySelector: () => element}, tokenCache: {writeEnabled: true},
  extractSearchParam: () => '', hassUrl: location.origin});
vm.runInContext('function saveTokens(tokens) {' + config.save + '\n}', context);
const snapshot = () => vm.runInContext(config.snapshot, context);
const auth = name => ({access_token: 'synthetic-access-' + name, refresh_token: 'synthetic-grant-' + name,
  clientId: 'local-client', hassUrl: location.origin});
const login = (id, admin=true) => {
  element = {hass: {user: {id, is_admin: admin, is_active: true}, auth: {data: auth(id)}}};
  context.next = element.hass.auth.data;
  vm.runInContext('saveTokens(next)', context);
};
login('owner-a');
const first = snapshot();
// Same sequence used by HA: revoke calls saveTokens(null); logout then reloads '/'.
vm.runInContext('saveTokens(null); location.href = "/"', context);
const signedOut = snapshot();
element = null;
const reload = snapshot();
login('owner-b');
const second = snapshot();
// Transient DOM loading is not an explicit logout even if storage is absent.
element = null; store.delete('hassTokens');
const transient = snapshot();
login('non-admin', false);
const forbidden = snapshot();
login('owner-b'); element.hass.auth.data.hassUrl = 'https://other-installation.invalid';
const foreignAuth = snapshot();
location.origin = 'https://foreign.invalid';
const foreignPage = snapshot();
console.log(JSON.stringify({first: first.state, firstUser: first.userId,
  signedOut: signedOut.state, reload: reload.state, second: second.state, secondUser: second.userId,
  transient: transient.state, forbidden: forbidden.state,
  foreignAuth: foreignAuth.state, foreignPage: foreignPage.state}));
"""
        result = subprocess.run(
            ["node", "-e", script, json.dumps({"snapshot": snapshot, "save": save_tokens})],
            text=True, capture_output=True, timeout=10, check=True,
        )
        self.assertEqual(json.loads(result.stdout), {
            "first": "authenticated", "firstUser": "owner-a", "signedOut": "signed_out",
            "reload": "signed_out", "second": "authenticated", "secondUser": "owner-b",
            "transient": "loading", "forbidden": "forbidden", "foreignAuth": "loading",
            "foreignPage": "loading",
        })


if __name__ == "__main__":
    unittest.main()
