"""Offline protocol checks for the copyable I-Digital REST command recipe."""

import json
from pathlib import Path

import yaml
from jinja2 import Environment


RECIPE = Path(__file__).parents[2] / "docs" / "sms_rest_commands.yaml"


def _secret_constructor(loader, node):
    """Keep !secret references as inert strings while parsing the example."""
    return loader.construct_scalar(node)


class _RecipeLoader(yaml.SafeLoader):
    pass


_RecipeLoader.add_constructor("!secret", _secret_constructor)


def test_send_recipe_renders_one_safe_direct_sms_object():
    config = yaml.load(RECIPE.read_text(), Loader=_RecipeLoader)
    command = config["rest_command"]["idigital_sms_send"]
    assert command["url"] == "https://direct.i-dgtl.ru/api/v1/message"
    assert command["method"] == "POST"
    assert command["headers"]["Authorization"] == "idigital_sms_authorization"
    assert command["timeout"] == 70

    template = Environment().from_string(command["payload"])
    payload = json.loads(template.render(
        target="+79123456789",
        message='Door "open"\\line\nВход',
    ))

    assert payload == [{
        "channelType": "SMS",
        "senderName": "sms_promo",
        "destination": "79123456789",
        "content": 'Door "open"\\line\nВход',
    }]


def test_status_recipe_uses_direct_uuid_endpoint_without_send_side_effects():
    config = yaml.load(RECIPE.read_text(), Loader=_RecipeLoader)
    command = config["rest_command"]["idigital_sms_status"]

    assert command["method"] == "GET"
    assert command["url"] == (
        "https://direct.i-dgtl.ru/api/v1/message/"
        "{{ message_uuid | urlencode }}"
    )
    assert command["headers"]["Authorization"] == "idigital_sms_authorization"
    assert "payload" not in command


def test_script_requires_one_scalar_target_and_returns_bounded_status():
    config = yaml.load(RECIPE.read_text(), Loader=_RecipeLoader)
    script = config["script"]["idigital_sms_send"]
    assert script["mode"] == "single"
    assert set(script["fields"]) == {"target", "message"}
    assert all(field["required"] for field in script["fields"].values())

    choose = script["sequence"][1]["choose"][0]
    args_valid = Environment().from_string(choose["conditions"][0]["value_template"])
    assert args_valid.render(sms_target="+79123456789", sms_text="hello") == "True"
    assert args_valid.render(sms_target=["+79123456789"], sms_text="hello") == "False"
    assert args_valid.render(sms_target="+7912,+7999", sms_text="hello") == "False"
    assert args_valid.render(sms_target="+79123456789", sms_text="  ") == "False"
    post_actions = [
        action for action in choose["sequence"]
        if action.get("action") == "rest_command.idigital_sms_send"
    ]
    assert len(post_actions) == 1
    assert post_actions[0]["continue_on_error"] is True
    assert not any("repeat" in action for action in choose["sequence"])

    result_template = Environment().from_string(
        script["sequence"][1]["choose"][0]["sequence"][1]["variables"]["sms_result_status"]
    )
    assert result_template.render(sms_http_response={
        "status": 200,
        "content": {"errors": False, "items": [{"messageUuid": "uuid-1"}]},
    }).strip() == "accepted"
    assert result_template.render(sms_http_response={
        "status": 200,
        "content": {"errors": True, "items": []},
    }).strip() == "rejected"
    assert result_template.render(sms_http_response={
        "status": 400,
        "content": {"error": {"code": 4220}},
    }).strip() == "rejected"
    assert result_template.render(sms_http_response={
        "status": 502,
        "content": "upstream failure",
    }).strip() == "unknown"
    assert result_template.render(sms_http_response={
        "status": 200,
        "content": {"errors": False, "items": []},
    }).strip() == "unknown"
