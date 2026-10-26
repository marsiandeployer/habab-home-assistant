"""Model-owned response meaning, checked against native action receipts.

OpenCode's json_schema output format stores the final value in info.structured.
Legacy text history is handled by SetupAgent; no natural-language classification
belongs in this adapter.
"""

from collections import Counter
import json
import shlex

OUTCOME_PROTOCOL = "habab-action-outcome.v1"
UNVERIFIED_TODO_TEXT = (
    "Добавление в список дел не подтверждено: в этом ходе "
    "нет проверенной записи для указанного списка и пункта."
)
OUTCOME_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["protocol", "kind", "text", "todo_additions"],
    "properties": {
        "protocol": {"type": "string", "const": OUTCOME_PROTOCOL},
        "kind": {"type": "string", "enum": ["answer", "clarification", "action_result"]},
        "text": {"type": "string", "minLength": 1, "maxLength": 12000},
        "todo_additions": {
            "type": "array", "maxItems": 100,
            "description": "Every todo addition claimed as completed in text; otherwise empty.",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["entity_id", "item"],
                "properties": {
                    "entity_id": {"type": "string", "minLength": 1},
                    "item": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}
OUTCOME_FORMAT = {"type": "json_schema", "schema": OUTCOME_SCHEMA, "retryCount": 0}
OUTCOME_PROMPT = """
Финальный ответ передавай через штатный инструмент StructuredOutput по заданной
схеме habab-action-outcome.v1. Смысл ответа определяешь ты: kind=clarification,
если нужен ответ владельца; kind=answer для объяснения, вопроса о состоянии или
сообщения об ошибке; kind=action_result для результата выполненного изменения.
В text пиши полный ответ владельцу. Все заявления об успешно добавленных в todo
пунктах перечисляй в todo_additions с точными entity_id и item из выполненных
действий этого хода; для таких заявлений всегда выбирай kind=action_result.
Не прячь заявление об успешном добавлении в answer или clarification и не
пропускай его в todo_additions. Для остальных ответов todo_additions=[]. Если
пункт уже был в списке, это ответ о состоянии, а не новое добавление. Уточнение
цели и ошибка не являются успешным изменением. Прежде чем вернуть результат
добавления, выполни habab-ha и проверь verification=persisted в его результате.
Обычный текст вне StructuredOutput не является финальным ответом владельцу.
"""


class OutcomeError(ValueError):
    """The requested runtime protocol was not satisfied."""


def parse_outcome(value):
    """Validate our small schema again at the application boundary."""
    if (not isinstance(value, dict)
            or set(value) != {"protocol", "kind", "text", "todo_additions"}
            or value["protocol"] != OUTCOME_PROTOCOL
            or value["kind"] not in ("answer", "clarification", "action_result")
            or not isinstance(value["text"], str)
            or not value["text"].strip() or len(value["text"]) > 12000
            or not isinstance(value["todo_additions"], list)
            or len(value["todo_additions"]) > 100):
        raise OutcomeError("invalid_action_outcome")
    for addition in value["todo_additions"]:
        if (not isinstance(addition, dict) or set(addition) != {"entity_id", "item"}
                or any(not isinstance(addition[key], str) or not addition[key].strip()
                       for key in ("entity_id", "item"))):
            raise OutcomeError("invalid_action_outcome")
    if value["todo_additions"] and value["kind"] != "action_result":
        raise OutcomeError("invalid_action_outcome")
    return value


def requests_outcome(info):
    """Recognize stored user turns that requested this version of the protocol."""
    output_format = info.get("format")
    if not isinstance(output_format, dict) or output_format.get("type") != "json_schema":
        return False
    schema = output_format.get("schema")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    protocol = properties.get("protocol") if isinstance(properties, dict) else None
    return isinstance(protocol, dict) and protocol.get("const") == OUTCOME_PROTOCOL


def _todo_add_request(tool_input, tool_name):
    """Decode the literal native helper invocation, never the owner's text."""
    if not isinstance(tool_input, dict):
        return None
    request = tool_input
    if tool_name == "bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            return None
        try:
            argv = shlex.split(command)
            if len(argv) != 2 or argv[0] != "habab-ha":
                return None
            request = json.loads(argv[1])
        except (TypeError, ValueError):
            return None
    elif tool_name != "habab-ha":
        return None
    if (not isinstance(request, dict) or request.get("action") != "call_service"
            or request.get("domain") != "todo" or request.get("service") != "add_item"):
        return None
    target, data = request.get("target"), request.get("data")
    entity_ids = target.get("entity_id") if isinstance(target, dict) else None
    if isinstance(entity_ids, str):
        entity_ids = [entity_ids]
    item = data.get("item") if isinstance(data, dict) else None
    if (not isinstance(entity_ids, list) or not entity_ids
            or any(not isinstance(entity_id, str) or not entity_id for entity_id in entity_ids)
            or not isinstance(item, str) or not item.strip()):
        return None
    return set(entity_ids), item.strip()


def _persisted_additions(part):
    state = part.get("state")
    if part.get("type") != "tool" or not isinstance(state, dict) or state.get("status") != "completed":
        return []
    request = _todo_add_request(state.get("input"), part.get("tool"))
    if request is None:
        return []
    output = state.get("output")
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except (TypeError, ValueError):
            return []
    if (not isinstance(output, dict) or output.get("called") != "todo.add_item"
            or output.get("verification") != "persisted" or not isinstance(output.get("items"), list)):
        return []
    targets, item = request
    verified = {
        (entry.get("entity_id"), entry.get("summary"))
        for entry in output["items"] if isinstance(entry, dict)
        and isinstance(entry.get("entity_id"), str) and isinstance(entry.get("summary"), str)
    }
    # One helper call must have verified every target before it can certify any.
    return [(entity_id, item) for entity_id in targets] if all(
        (entity_id, item) in verified for entity_id in targets) else []


def todo_claims_verified(outcome, message, messages, prior_message_ids=frozenset()):
    """Match every declared addition with a completed receipt in this user turn."""
    claims = Counter((entry["entity_id"], entry["item"]) for entry in outcome["todo_additions"])
    if not claims:
        return True
    info = message.get("info", {})
    parent_id = info.get("parentID")
    if (not isinstance(parent_id, str) or parent_id in prior_message_ids
            or not isinstance(info.get("id"), str) or info["id"] in prior_message_ids
            or not any(entry.get("info", {}).get("id") == parent_id
                       and entry.get("info", {}).get("role") == "user" for entry in messages)):
        return False
    receipts = Counter()
    seen_parts = set()
    for entry in messages:
        entry_info = entry.get("info", {})
        if (entry_info.get("role") != "assistant" or entry_info.get("parentID") != parent_id
                or not isinstance(entry_info.get("id"), str) or entry_info["id"] in prior_message_ids):
            continue
        for part in entry.get("parts", []):
            # A repeated message/part in the response cannot multiply receipts.
            part_id = part.get("callID") or part.get("id")
            if not isinstance(part_id, str) or not part_id or part_id in seen_parts:
                continue
            seen_parts.add(part_id)
            receipts.update(_persisted_additions(part))
    return all(receipts[claim] >= count for claim, count in claims.items())
