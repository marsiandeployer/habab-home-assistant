"""Owner-editable instructions shared by the local assistant executors.

Call filesystem operations through hass.async_add_executor_job.
"""
from pathlib import Path
import os
import tempfile

MAX_BYTES = 64 * 1024
DEFAULT_INSTRUCTIONS = """# Помощник Habab Home

Отвечай по-русски. Выполняй поручения авторизованного владельца дома.
Префикс «ко» означает обращение к тебе; текст после него — задача.

У тебя есть доступ к локальной системе в пределах процесса приложения.
Сначала проверь фактические права, окружение и доступные команды. Не путай
контейнер с хостом. При необходимости исследуй и меняй код, конфигурацию и
базу данных этой установки. Предпочитай штатные API Home Assistant, но не
отказывайся от задачи только потому, что её нет в списке готовых инструментов.
Перед прямым изменением файлов или БД сохрани возможность отката; учитывай,
что Home Assistant может держать настройки в памяти. Не меняй чужие системы.
Секреты используй локально, не публикуй их в ответах и журналах.

Проверяй результат чтением изменённого состояния. Различай изменение кода,
запуск и результат на устройстве. При ошибке объясни причину и следующий шаг;
не оставляй пользователя без ответа и не объявляй успех без проверки.
Голосовое сообщение после распознавания выполняй как обычную текстовую задачу.
"""


def read_instructions(workspace: Path) -> str:
    """Initialize once; preserve subsequent owner edits, including empty text."""
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = workspace / "AGENTS.md"
    if target.is_symlink():
        raise ValueError("agent_instructions_symlink")
    try:
        with target.open("x", encoding="utf-8") as stream:
            os.chmod(target, 0o600)
            stream.write(DEFAULT_INSTRUCTIONS)
    except FileExistsError:
        pass
    with target.open("rb") as stream:
        content = stream.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise ValueError("agent_instructions_too_large")
    return content.decode("utf-8")


def write_instructions(workspace: Path, text: str) -> None:
    """Atomically replace only the assistant workspace's instructions."""
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("agent_instructions_invalid")
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = workspace / "AGENTS.md"
    if target.is_symlink():
        raise ValueError("agent_instructions_symlink")
    descriptor, name = tempfile.mkstemp(prefix=".AGENTS-", dir=workspace)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)
