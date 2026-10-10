"""Plugin-wide defaults for the console-API schema fetch settings.

Endpoint settings are per-endpoint, which means every new endpoint has to
retype ``dify_console_api_base`` / ``dify_console_api_key`` /
``dify_workspace_id``. This module keeps one shared copy in the plugin's
persistence storage (scoped to this plugin by the daemon, shared across all
its endpoints) with an "auto-register" flow and no extra UI:

- an endpoint that fills all three fields registers them as the default on
  first use (storage is written only when the values actually changed)
- an endpoint that leaves any of them empty inherits that field from the
  stored default (per-field merge: endpoint values win)
- a partially filled endpoint is never written back, so it cannot poison the
  default with an incomplete configuration

Caveat: endpoint settings are encrypted at rest by the daemon
(``secret-input``), but plugin storage is plain KV. The API key copy in
storage is only reachable through this plugin's own storage invocation.

Only field names and a boolean ever reach the logs — never values.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

CONSOLE_SETTING_KEYS = (
    "dify_console_api_base",
    "dify_console_api_key",
    "dify_workspace_id",
)

# plugin storage key holding the JSON default (bytes in, bytes out)
DEFAULTS_STORAGE_KEY = "mcp_console_api_defaults"


class StorageAdapter:
    """Narrow interface required from ``session.storage``."""

    def get(self, key: str) -> bytes: ...

    def set(self, key: str, val: bytes) -> None: ...


def normalize_base(value: str) -> str:
    """Keep only scheme://host[:port] of a configured base URL.

    A user pasting a full console URL (``http://api:5001/console/api/...``)
    would otherwise have the path appended to it and 404.
    """
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return value.rstrip("/")


def _endpoint_values(settings: Mapping) -> dict[str, str]:
    values: dict[str, str] = {}
    for key in CONSOLE_SETTING_KEYS:
        try:
            raw = settings.get(key)
        except AttributeError:
            raw = None
        value = str(raw).strip() if raw is not None else ""
        if key == "dify_console_api_base" and value:
            value = normalize_base(value)
        if value:
            values[key] = value
    return values


def _load_defaults(storage: StorageAdapter) -> dict[str, str]:
    try:
        raw = storage.get(DEFAULTS_STORAGE_KEY)
    except Exception:
        # not registered yet / storage unavailable
        return {}
    try:
        data = json.loads(bytes(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        logger.warning("Stored console defaults are malformed; ignoring them")
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k: str(data[k])
        for k in CONSOLE_SETTING_KEYS
        if isinstance(data.get(k), str) and data[k]
    }


def _store_defaults(storage: StorageAdapter, values: dict[str, str]) -> None:
    try:
        storage.set(DEFAULTS_STORAGE_KEY, json.dumps(values).encode("utf-8"))
        logger.info("Console API defaults registered in plugin storage")
    except Exception as exc:
        # non-fatal: this endpoint still works with its own values
        logger.warning(
            "Failed to register console API defaults in plugin storage (%s)",
            type(exc).__name__,
        )


def resolve_settings(settings: Mapping, storage: StorageAdapter | None) -> dict:
    """Merge endpoint settings with the plugin-wide console-API defaults.

    Returns a plain dict usable as ``settings``: every endpoint key is kept;
    the three console keys fall back to the stored default when the endpoint
    left them empty. A fully filled endpoint also registers its values as the
    default. Never raises on storage failures.
    """

    try:
        merged: dict[str, Any] = dict(settings)
    except (TypeError, ValueError):
        merged = {}
    if storage is None:
        return merged

    endpoint_values = _endpoint_values(settings)
    defaults = _load_defaults(storage)

    complete = all(k in endpoint_values for k in CONSOLE_SETTING_KEYS)
    if complete and endpoint_values != defaults:
        _store_defaults(storage, endpoint_values)
        defaults = endpoint_values

    for key in CONSOLE_SETTING_KEYS:
        if not merged.get(key) and key in defaults:
            merged[key] = defaults[key]

    logger.info(
        "Console API settings resolved: endpoint_provided=%s inherited=%s",
        sorted(endpoint_values.keys()),
        sorted(k for k in CONSOLE_SETTING_KEYS if k not in endpoint_values),
    )
    return merged
