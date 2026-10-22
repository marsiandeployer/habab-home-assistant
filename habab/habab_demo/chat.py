"""Public DeepSeek help chat. No shell, hardware or private Home Assistant API."""
import json
import os
from pathlib import Path

from flask import current_app, jsonify, request
import requests

from .routes import ha_demo_bp, _same_origin, _session_hash, _store

PROMPT = """Ты помощник по настройке Home Assistant на сайте Habab. Отвечай
по-русски, кратко и по делу. Пользователь смотрит публичное демо умного дома.
В этом чате у тебя нет shell, root, Bluetooth, доступа к реальному дому или
инструментов управления. Давай полезные инструкции по подключению устройств,
провайдерам, автоматизациям, Docker и диагностике. Не утверждай, что выполнил
команду, сканирование, включил устройство или изменил настройки. Для выполнения
команд и подключения реального оборудования нужен чат OpenCode в домашней
Docker-установке Home Assistant под администратором. Если не хватает сведений
об устройстве, уточни модель и способ подключения. Не проси присылать API-ключи,
пароли или токены. Ты не видишь список реальных устройств пользователя.
"""


class ChatError(Exception):
    def __init__(self, code):
        self.code = code


def deepseek_reply(messages):
    filename = current_app.config.get('HA_DEMO_DEEPSEEK_KEY_FILE') or os.getenv('HABAB_DEMO_DEEPSEEK_KEY_FILE')
    if not filename:
        raise ChatError('unavailable')
    try:
        key = Path(filename).read_text().strip()
    except OSError as error:
        raise ChatError('unavailable') from error
    if not key:
        raise ChatError('unavailable')
    try:
        with requests.post('https://api.deepseek.com/chat/completions',
            headers={'Authorization': 'Bearer ' + key},
            json={'model': 'deepseek-flash', 'messages': [{'role': 'system', 'content': PROMPT}] + messages[-12:],
                  'max_tokens': 1024, 'thinking': {'type': 'disabled'}},
            timeout=(5, 25), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                current_app.logger.warning('Demo chat provider HTTP %d', response.status_code)
                raise ChatError('rate_limited' if response.status_code == 429 else 'provider_error')
            raw = bytearray()
            for chunk in response.iter_content(65536):
                raw.extend(chunk)
                if len(raw) > 1_000_000:
                    raise ChatError('provider_error')
            result = json.loads(raw)
            choice = result['choices'][0]
            text = choice['message']['content']
            if choice.get('finish_reason') != 'stop' or not isinstance(text, str) or not text.strip():
                raise ChatError('provider_error')
            return text.replace(key, '[redacted]')[:12000]
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as error:
        current_app.logger.warning('Demo chat request failed: %s', type(error).__name__)
        raise ChatError('provider_error') from error


@ha_demo_bp.get('/chat')
def get_chat():
    try:
        return jsonify(messages=_store().chat_history(_session_hash()))
    except Exception:
        current_app.logger.warning('Demo chat history unavailable')
        return jsonify(error='unavailable'), 503


@ha_demo_bp.post('/chat')
def send_chat():
    if not _same_origin():
        return jsonify(error='origin'), 403
    if request.content_length and request.content_length > 18000:
        return jsonify(error='too_large'), 413
    body = request.get_json(silent=True)
    text = body.get('text') if isinstance(body, dict) else None
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 4000:
        return jsonify(error='invalid'), 400
    try:
        messages = _store().chat_turn(_session_hash(), text.strip(), deepseek_reply)
        return jsonify(messages=messages)
    except ChatError as error:
        return jsonify(error=error.code), 429 if error.code == 'rate_limited' else 503
    except Exception:
        current_app.logger.warning('Demo chat turn unavailable')
        return jsonify(error='unavailable'), 503


@ha_demo_bp.post('/chat/reset')
def reset_chat():
    if not _same_origin():
        return jsonify(error='origin'), 403
    try:
        _store().chat_reset(_session_hash())
        return jsonify(messages=[])
    except Exception:
        current_app.logger.warning('Demo chat reset unavailable')
        return jsonify(error='unavailable'), 503
