"""Recorded voice endpoints tested with loopback-only upstreams and synthetic credentials."""
import hashlib
import json
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from openai_gateway import Gateway
import telegram_speech as speech


class SpeechTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.transcript = 'Покажи список устройств'
        self.status = 200
        self.audio = b'OggS' + b'fake-unit-audio'
        self.key = 'unit-service-key'
        self.bot = '12345:' + 'b' * 24
        async def upstream(request):
            self.calls.append((request.path, dict(request.headers), await request.read()))
            if request.path.startswith('/file/'):
                return web.Response(body=self.audio, status=self.status)
            return web.json_response({'candidates': [{'content': {'parts': [{'text': self.transcript}]}}]}, status=self.status)
        app = web.Application(client_max_size=8 * 1024 * 1024)
        app.router.add_route('*', '/{tail:.*}', upstream)
        self.upstream = TestServer(app)
        await self.upstream.start_server()
        self.gateway = Gateway(hashlib.sha256(self.key.encode()).hexdigest(), upstream_key='unit-gemini-key')
        self.client = TestClient(TestServer(self.gateway.app()))
        await self.client.start_server()
        self.patches = [patch.object(speech, 'GEMINI_AUDIO_URL', str(self.upstream.make_url('/transcribe'))),
                        patch.object(speech, 'TELEGRAM_FILE_BASE', str(self.upstream.make_url('/file/')))]
        for item in self.patches: item.start()
        self.auth = {'Authorization': 'Bearer ' + self.key}

    async def asyncTearDown(self):
        for item in self.patches: item.stop()
        await self.client.close()
        await self.upstream.close()

    def form(self, audio=None, model='habab-transcribe'):
        form = aiohttp.FormData()
        form.add_field('file', self.audio if audio is None else audio, filename='test.ogg', content_type='audio/ogg')
        form.add_field('model', model)
        form.add_field('response_format', 'json')
        return form

    async def test_recorded_audio_uses_gemini_and_returns_only_transcript(self):
        response = await self.client.post('/v1/audio/transcriptions', headers=self.auth, data=self.form())
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {'text': self.transcript})
        _, headers, raw = self.calls[0]
        self.assertEqual(headers['x-goog-api-key'], 'unit-gemini-key')
        self.assertNotIn('Authorization', headers)
        body = json.loads(raw)
        self.assertEqual(body['contents'][0]['parts'][1]['inlineData']['mimeType'], 'audio/ogg')
        self.assertEqual(self.gateway.inflight, 0)

    async def test_owner_auth_required_before_audio_or_download_upstream(self):
        for path, kwargs in (('/v1/audio/transcriptions', {'data': self.form()}),
                             ('/telegram/downloadFile', {'json': {'file_path': 'voice/file.oga'}})):
            response = await self.client.post(path, **kwargs)
            self.assertEqual(response.status, 401)
        self.assertEqual(self.calls, [])

    async def test_no_key_reports_not_ready_without_openai_fallback(self):
        self.gateway.upstream_key = None
        response = await self.client.post('/v1/audio/transcriptions', headers=self.auth, data=self.form())
        self.assertEqual(response.status, 503)
        self.assertEqual(self.calls, [])
        self.assertFalse(speech.capability(self.gateway)['ready'])

    async def test_download_hides_bot_token_in_headers_and_checks_path(self):
        headers = {**self.auth, 'X-Habab-Telegram-Bot-Token': self.bot}
        response = await self.client.post('/telegram/downloadFile', headers=headers, json={'file_path': 'voice/file.oga'})
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), self.audio)
        self.assertEqual(self.calls[0][0], '/file/' + self.bot + '/voice/file.oga')
        self.assertNotIn('Authorization', self.calls[0][1])
        self.assertNotIn('X-Habab-Telegram-Bot-Token', self.calls[0][1])
        for path in ('voice/../private', 'https://attacker/x', 'voice/file?x=1'):
            response = await self.client.post('/telegram/downloadFile', headers=headers, json={'file_path': path})
            self.assertEqual(response.status, 400)
        self.assertEqual(len(self.calls), 1)

    async def test_upload_and_download_byte_limits(self):
        with patch.object(speech, 'MAX_AUDIO_BYTES', 16):
            response = await self.client.post('/v1/audio/transcriptions', headers=self.auth, data=self.form(b'OggS' + b'x' * 20))
            self.assertEqual(response.status, 413)
            response = await self.client.post('/telegram/downloadFile', headers={**self.auth, 'X-Habab-Telegram-Bot-Token': self.bot}, json={'file_path': 'voice/file.oga'})
            self.assertEqual(response.status, 413)
        self.assertEqual(len(self.calls), 1)

    async def test_upstream_error_and_empty_transcript_do_not_fallback(self):
        self.status = 429
        response = await self.client.post('/v1/audio/transcriptions', headers=self.auth, data=self.form())
        self.assertEqual(response.status, 502)
        self.status = 200
        self.transcript = ''
        response = await self.client.post('/v1/audio/transcriptions', headers=self.auth, data=self.form())
        self.assertEqual(response.status, 422)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.gateway.inflight, 0)
