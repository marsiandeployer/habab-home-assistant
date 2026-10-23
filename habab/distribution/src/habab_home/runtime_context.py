"""Runtime-specific context for the private setup assistant."""

import hashlib
import json

DOCKER_RUNTIME_KIND = "docker"
ANDROID_PROOT_RUNTIME_KIND = "android_proot"
RUNTIME_KIND_ENV = "HABAB_RUNTIME_KIND"

_DOCKER_HOST_CONTEXT = """ВНУТРИ Docker-контейнера Home Assistant Core, НЕ на хосте. Это не Home Assistant
OS и не Supervisor. Тип хоста по Linux внутри контейнера определить нельзя.
Bluetooth override с BlueZ применим только к Linux-хосту. В Windows Docker Desktop
встроенный Bluetooth-адаптер не появляется от обычного override или root-прав;
доступный путь — отдельно подключённый ESPHome Bluetooth proxy по LAN либо
настроенный владельцем проброс поддерживаемого USB-адаптера. Не обещай, что любой
из этих способов уже доступен. Перед инструкцией для хоста уточни его тип."""

_ANDROID_HOST_CONTEXT = """Внутри Android-приложения OpenCode запущен в Linux userland через PRoot.
UID 0 действует только внутри PRoot-гостя: это НЕ root Android и НЕ Docker с
доступом к Android-хосту. Не определяй платформу или права хоста по uname.
PRoot сам по себе не даёт доступа к Bluetooth, Wi-Fi-сканированию, BlueZ или
системному D-Bus Android. Используй такие возможности только если существует
реальный мост через разрешение/API Android и доступ подтверждён результатом
инструмента. Не предполагай наличие такого моста. Обычные нативные настройки
Home Assistant и доступные инструменты остаются применимы."""

_DOCKER_BLUETOOTH_CONTEXT = """Права контейнера и хоста различаются. Выполняй разрешённые пользователем команды, сначала
проверяя реальное состояние. Для Bluetooth доступны bluetoothctl, Python,
/run/dbus, если владелец подключил Bluetooth override. Используй timeout для
сканирования (не более 15 секунд). Нет адаптера, D-Bus или доступа — честно
сообщи причину; не выдумывай найденные устройства или успех."""

_ANDROID_BLUETOOTH_CONTEXT = """Права PRoot-гостя и Android-хоста различаются. Наличие bluetoothctl, Python,
/run/dbus или UID 0 внутри гостя не подтверждает права Android-приложения на
Bluetooth или Wi-Fi-сканирование. Используй их только через реально доступный и
проверенный Android permission/API bridge; иначе сообщи, что у HA нет такого
доступа. Для доступных штатных HA discovery-потоков соблюдай ограничение
сканирования до 15 секунд. Не обходи sandbox, не меняй разрешения Android и не
выдумывай найденные устройства или успех."""

_DOCKER_DISCOVERY_CONTEXT = (
    "не предполагает наличие Windows-адаптера и не меняет сеть или настройки хоста"
)
_ANDROID_DISCOVERY_CONTEXT = (
    "не предполагает доступ к Bluetooth или Wi-Fi Android-хоста без явного моста "
    "и не меняет сеть или настройки хоста"
)


def runtime_prompt(runtime_kind, docker_prompt):
    """Select a truthful prompt without changing the canonical Docker prompt."""
    if runtime_kind == DOCKER_RUNTIME_KIND:
        return docker_prompt
    if runtime_kind != ANDROID_PROOT_RUNTIME_KIND:
        raise ValueError("unsupported_runtime")

    replacements = (
        (_DOCKER_HOST_CONTEXT, _ANDROID_HOST_CONTEXT),
        (_DOCKER_BLUETOOTH_CONTEXT, _ANDROID_BLUETOOTH_CONTEXT),
        (_DOCKER_DISCOVERY_CONTEXT, _ANDROID_DISCOVERY_CONTEXT),
    )
    prompt = docker_prompt
    for old, new in replacements:
        if prompt.count(old) != 1:
            raise ValueError("runtime_prompt_mismatch")
        prompt = prompt.replace(old, new, 1)
    return prompt


def runtime_fingerprint(settings, runtime_kind):
    """Fingerprint provider settings together with the selected runtime."""
    payload = {"settings": dict(settings), "runtime_kind": runtime_kind}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
