"""Pure cross-repository request contract, not a gateway or Hermes certification.

Run with Enterprise/src on PYTHONPATH as well as Runtime/src. All bodies are
fictional; no provider or transport is called. The Runtime factory regression
separately proves local request_overrides are absent for managed ModelConfig.
"""

from copy import deepcopy

import pytest

contract = pytest.importorskip("safent_control.domain.inference_request")
InferenceError = pytest.importorskip("safent_control.domain.inference").InferenceError
pytestmark = pytest.mark.integration


def native_body():
    return {
        "model": "company-model",
        "messages": [
            {"role": "user", "content": "Read the approved fixture."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "fixture-call",
                        "type": "function",
                        "function": {
                            "name": "read_fixture",
                            "arguments": '{"name":"fixture"}',
                        },
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "fixture-call", "content": "Fixture result."},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "read_fixture",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": 4096,
    }


def test_native_text_tools_stream_envelope_remains_valid_and_unmodified():
    body = native_body()
    before = deepcopy(body)
    contract.validate_request(body)
    assert body == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("extra_body", {"model": "personal", "api_key": "fictional"}),
        ("chat_template_kwargs", {"enable_thinking": False}),
        ("request_overrides", {"base_url": "https://other.invalid"}),
        ("base_url", "https://other.invalid"),
        ("api_key", "fictional"),
        ("provider", "personal"),
        ("fallback_providers", ["personal"]),
    ],
)
def test_enterprise_rejects_both_wrapped_and_sdk_flattened_local_extensions(field, value):
    body = native_body() | {field: value}
    with pytest.raises(InferenceError) as caught:
        contract.validate_request(body)
    assert caught.value.code == "inference_request_unsupported"


def test_extensions_inside_tool_argument_json_remain_inert_data():
    body = native_body()
    body["messages"][1]["tool_calls"][0]["function"]["arguments"] = (
        '{"base_url":"https://not-a-route.invalid","extra_body":{"model":"not-a-model"}}'
    )
    contract.validate_request(body)
