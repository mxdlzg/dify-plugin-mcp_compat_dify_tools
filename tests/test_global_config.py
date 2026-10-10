"""Plugin-wide console-API defaults: auto-register, per-field inheritance,
and no poisoning by partially filled endpoints.

Storage is faked with the same contract as session.storage: get() raises for
missing keys, set() stores bytes.
"""

import pytest

from core.global_config import (
    CONSOLE_SETTING_KEYS,
    DEFAULTS_STORAGE_KEY,
    _endpoint_values,
    resolve_settings,
)

FULL = {
    "dify_console_api_base": "http://api:5001",
    "dify_console_api_key": "endpoint-key",
    "dify_workspace_id": "ws-a",
}


class FakeStorage:
    def __init__(self):
        self.store = {}
        self.sets = 0

    def get(self, key):
        return self.store[key]

    def set(self, key, val):
        self.sets += 1
        self.store[key] = val


@pytest.fixture
def storage():
    return FakeStorage()


def test_full_endpoint_registers_defaults_once(storage):
    merged = resolve_settings({"tools": []}, None)  # storage-less pass-through
    assert merged == {"tools": []}

    merged = resolve_settings({"tools": [], **FULL}, storage)
    assert merged["dify_console_api_base"] == "http://api:5001"
    assert storage.sets == 1

    # same values again -> no rewrite
    resolve_settings({"tools": [], **FULL}, storage)
    assert storage.sets == 1


def test_new_endpoint_inherits_all_fields(storage):
    resolve_settings(FULL, storage)
    merged = resolve_settings({"tools": []}, storage)
    for key in CONSOLE_SETTING_KEYS:
        assert merged[key] == FULL[key]


def test_per_field_override_keeps_endpoint_value(storage):
    resolve_settings(FULL, storage)
    override = {"dify_console_api_base": "http://other:5001"}
    merged = resolve_settings(override, storage)
    # endpoint wins for the field it filled...
    assert merged["dify_console_api_base"] == "http://other:5001"
    # ...and inherits the rest
    assert merged["dify_console_api_key"] == FULL["dify_console_api_key"]
    assert merged["dify_workspace_id"] == FULL["dify_workspace_id"]
    # a partial endpoint must not overwrite the registered defaults
    import json
    stored = json.loads(bytes(storage.store[DEFAULTS_STORAGE_KEY]))
    assert stored == FULL


def test_no_defaults_yet_leaves_missing_fields_absent(storage):
    merged = resolve_settings({"tools": []}, storage)
    for key in CONSOLE_SETTING_KEYS:
        assert key not in merged


def test_partial_endpoint_is_not_registered(storage):
    resolve_settings({"dify_console_api_base": "http://api:5001"}, storage)
    assert DEFAULTS_STORAGE_KEY not in storage.store
    assert storage.sets == 0


def test_broken_storage_never_breaks_resolution():
    class BrokenStorage:
        def get(self, key):
            raise RuntimeError("daemon down")

        def set(self, key, val):
            raise RuntimeError("daemon down")

    merged = resolve_settings({"tools": [], **FULL}, BrokenStorage())
    assert merged["dify_console_api_base"] == "http://api:5001"


def test_corrupt_stored_defaults_are_ignored(storage):
    storage.store[DEFAULTS_STORAGE_KEY] = b"not-json{"
    merged = resolve_settings({"tools": []}, storage)
    for key in CONSOLE_SETTING_KEYS:
        assert key not in merged


def test_base_url_is_trimmed_to_origin():
    # a full console URL pasted by the user must not be appended to
    settings = {
        "dify_console_api_base":
            "http://api:5001/console/api/workspaces/current/tool-provider/"
            "builtin/langgenius/tavily/tavily/tools",
    }
    assert _endpoint_values(settings)["dify_console_api_base"] == "http://api:5001"


def test_plain_origin_and_trailing_slash_pass_through():
    assert _endpoint_values(
        {"dify_console_api_base": "http://host.docker.internal:5001/"}
    )["dify_console_api_base"] == "http://host.docker.internal:5001"


def test_resolver_also_normalizes_pasted_url():
    from core.tool_metadata import ToolMetadataResolver

    resolver = ToolMetadataResolver({
        "dify_console_api_base": "http://api:5001/console/api/whatever",
        "dify_console_api_key": "k",
        "dify_workspace_id": "ws",
    })
    assert resolver._base == "http://api:5001"
