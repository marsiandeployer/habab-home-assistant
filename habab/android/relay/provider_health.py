"""Provider readiness and owner alerts. No credential or response-body logging."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time

from aiohttp import ClientTimeout


class ProviderHealth:
    def __init__(self, provider, model, state_path, notify_command=(), recipient=None):
        self.provider, self.model = provider, model
        self.path = Path(state_path)
        self.command, self.recipient = list(notify_command), recipient
        self.reason, self.checked_at = 'not_checked', 0
        self.inference_status, self.inference_at = None, None
        self.notified = None
        if self.path.is_file():
            saved = json.loads(self.path.read_text())
            self.notified = saved.get('notified')
            self.inference_status = saved.get('last_inference_status')
            self.inference_at = saved.get('last_inference_at')

    @staticmethod
    def classify(status):
        if status == 200: return 'ready'
        if status in (401, 403): return 'credential_rejected'
        if status == 429: return 'rate_limited'
        return 'provider_unavailable'

    def observe_http(self, status):
        self.inference_status, self.inference_at = status, int(time.time())
        # Do not blame provider credentials for malformed client input.
        if status == 200 or status in (401, 403, 429) or status >= 500:
            self.reason = self.classify(status)
            self.checked_at = int(time.time())
        self.save()

    async def probe(self, client, key):
        if not key:
            self.reason = 'missing_credential'
        else:
            url, headers = (
                ('https://api.deepseek.com/models', {'Authorization': 'Bearer ' + key})
                if self.provider == 'deepseek' else
                ('https://generativelanguage.googleapis.com/v1beta/models?pageSize=1', {'x-goog-api-key': key}))
            try:
                async with client.get(url, headers=headers, allow_redirects=False,
                                      timeout=ClientTimeout(total=10)) as response:
                    reason = self.classify(response.status)
                    if self.provider == 'gemini' and response.status == 400:
                        raw = await response.content.read(4097)
                        if len(raw) <= 4096:
                            try:
                                error = json.loads(raw).get('error', {})
                                if any(d.get('reason') == 'API_KEY_INVALID' for d in error.get('details', []) if isinstance(d, dict)):
                                    reason = 'credential_rejected'
                            except (ValueError, AttributeError, TypeError):
                                pass
                # A free models probe cannot prove an inference quota recovered.
                if (reason == 'ready' and self.inference_status is not None
                        and self.inference_status != 200):
                    reason = self.classify(self.inference_status)
                self.reason = reason
            except Exception:
                self.reason = 'provider_unavailable'
        self.checked_at = int(time.time())
        self.save()

    def snapshot(self):
        reason = self.reason if time.time() - self.checked_at < 180 else 'stale_check'
        return {'provider': self.provider, 'model': self.model, 'ready': reason == 'ready',
                'reason': reason, 'checked_at': self.checked_at,
                'probe': 'credential_access', 'last_inference_status': self.inference_status,
                'last_inference_at': self.inference_at}

    def save(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.health-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as out:
                json.dump({**self.snapshot(), 'notified': self.notified}, out)
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    async def notify(self):
        reason = self.snapshot()['reason']
        if not self.command or not self.recipient or reason == self.notified:
            return
        if reason == 'ready' and self.notified is None:
            self.notified = reason; self.save(); return
        labels = {'ready': 'проверка доступа снова проходит',
                  'missing_credential': 'серверный ключ отсутствует',
                  'credential_rejected': 'ключ или права доступа отклонены',
                  'rate_limited': 'запросы ограничены (HTTP 429), проверьте лимиты',
                  'provider_unavailable': 'провайдер не отвечает или вернул ошибку',
                  'stale_check': 'проверка провайдера давно не обновлялась',
                  'not_checked': 'провайдер ещё не проверен'}
        message = f'Habab Home: {self.provider} / {self.model} — {labels[reason]}.'
        process = None
        try:
            env = dict(os.environ, HABAB_TELEGRAM_NO_CONTENT_LOG='1', HABAB_TELEGRAM_STATUS='1')
            process = await asyncio.create_subprocess_exec(*self.command,
                f'напиши {self.recipient} {message}', stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=env)
            output, _ = await asyncio.wait_for(process.communicate(), timeout=25)
            delivered = False
            for line in output.decode('utf-8', errors='replace').splitlines():
                if line.startswith('HABAB_TELEGRAM_RESULT='):
                    receipt = json.loads(line.split('=', 1)[1])
                    delivered = receipt.get('state') == 'delivered' and type(receipt.get('message_id')) is int
            if process.returncode == 0 and delivered:
                self.notified = reason; self.save()
        except Exception:
            pass  # Retry the same unsent transition on the next check.
        finally:
            if process is not None and process.returncode is None:
                process.kill(); await process.wait()

    async def run(self, client, key_getter):
        while True:
            await self.probe(client, key_getter())
            await self.notify()
            await asyncio.sleep(60)
