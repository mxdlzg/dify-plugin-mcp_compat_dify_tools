"""Convert MCP file arguments into the Dify plugin file parameter format.

Verified against Dify 1.17.1 / dify-plugin-daemon / dify_plugin 0.10.2:

* ``session.tool.invoke()`` sends ``tool_parameters`` as plain JSON through the
  daemon to Dify ``inner/api/invoke/tool``; nothing on that path converts a
  dict into a file object (Dify's ``convert_parameters_to_plugin_format`` only
  matches a real ``graphon.file.File`` *instance*).
* The target plugin's SDK (``Tool._convert_parameters``) rebuilds a
  ``dify_plugin.file.File`` from a dict whose ``dify_model_identity`` is
  ``__dify__file__``, reading ``url`` (required — ``KeyError`` if missing) and
  ``type`` (must be a valid ``FileType`` or ``ValueError``).
* Therefore the shape that actually reaches a tool is the one produced by
  graphon's ``File.to_plugin_parameter()``: identity + url + mime_type +
  filename + extension + size + type. ``UploadFileResponse.to_app_parameter()``
  (``tool_file_id``/``transfer_method``) is NOT recognized on this path — do
  not emit it.
* ``url`` must be the upload's signed ``preview_url``. It is a bearer token
  bound to the uploading tenant/user and expires after ``FILES_ACCESS_TIMEOUT``
  (default 300 s), which is why uploads are reused only briefly in-process.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import logging
import mimetypes
import re
import socket
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

DIFY_FILE_IDENTITY = "__dify__file__"

#: upload(filename, content, mimetype) -> object with id/name/size/extension/
#: mime_type/type/preview_url (i.e. dify_plugin UploadFileResponse).
UploadFn = Callable[[str, bytes, str], Any]


class FileAdapterError(ValueError):
    """Client-facing file problem: wrong shape, too big, blocked, unreachable.

    Surfaces as an MCP tool result with ``isError: true``; the message must
    never contain file content or credentials.
    """


@dataclass(frozen=True)
class FileLimits:
    max_file_bytes: int = 5 * 1024 * 1024
    max_files_per_call: int = 5
    max_total_bytes: int = 10 * 1024 * 1024
    download_timeout_seconds: float = 30.0
    #: URL sources are refused unless the admin lists the host here. Guards
    #: against the gateway being used to read the Docker network (DB, Redis,
    #: Dify API) or cloud metadata endpoints.
    url_allow_hosts: tuple[str, ...] = ()
    url_enabled: bool = False
    #: Allow an allow-listed host to resolve into RFC1918/ULA space (a LAN file
    #: server). Loopback and link-local (cloud metadata) stay refused.
    url_allow_private: bool = False

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any] | None) -> "FileLimits":
        """Read limits from endpoint settings; invalid/absent values fall back.

        Only field names are logged — settings may hold secrets.
        """

        if not settings:
            return cls()

        def _int(key: str, default: int, lo: int, hi: int) -> int:
            raw = settings.get(key)
            if raw in (None, ""):
                return default
            try:
                value = int(str(raw).strip())
            except (TypeError, ValueError):
                logger.warning("Ignoring non-integer file limit setting '%s'", key)
                return default
            if not lo <= value <= hi:
                logger.warning(
                    "Ignoring out-of-range file limit setting '%s'=%s (allowed %d..%d)",
                    key, value, lo, hi,
                )
                return default
            return value

        def _bool(key: str, default: bool = False) -> bool:
            # Dify select settings arrive as the strings "true"/"false"; a
            # plain bool() would read "false" as True.
            raw = settings.get(key)
            if raw in (None, ""):
                return default
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("true", "1", "yes", "on")

        allow_raw = settings.get("file_url_allow_hosts") or ""
        hosts = tuple(
            h.strip().lower().rstrip(".")
            for h in str(allow_raw).replace("\n", ",").split(",")
            if h.strip()
        )
        return cls(
            max_file_bytes=_int("file_max_bytes", 5 * 1024 * 1024, 1, 15 * 1024 * 1024),
            max_files_per_call=_int("file_max_count", 5, 1, 50),
            max_total_bytes=_int("file_max_total_bytes", 10 * 1024 * 1024, 1, 60 * 1024 * 1024),
            download_timeout_seconds=float(
                _int("file_url_timeout_seconds", 30, 1, 300)
            ),
            url_allow_hosts=hosts,
            # Explicit switch: listing hosts alone is not consent to fetch.
            url_enabled=_bool("file_url_enabled"),
            url_allow_private=_bool("file_url_allow_private"),
        )


# Signature probes: enough to reject a mismatched declared MIME type without
# pulling in a magic-database dependency.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "video/webp"),  # refined below
    (b"ID3", "audio/mpeg"),
    (b"\x1aE\xdf\xa3", "video/x-matroska"),
    (b"OggS", "audio/ogg"),
    (b"PK\x03\x04", "application/zip"),
    (b"\x7fELF", "application/x-executable"),
)

_TEXT_SAFE = re.compile(rb"^[ -~\t\r\n]*$")


def _sniff_mime(content: bytes) -> str | None:
    """Best-effort content type, or None when the bytes are inconclusive."""

    for magic, mime in _MAGIC:
        if content.startswith(magic):
            if mime == "video/webp" and content[8:12] != b"WEBP":
                return "application/octet-stream"
            if mime == "application/zip":
                # Office XML formats are zip containers with a known layout.
                if b"word/" in content[:2048]:
                    return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                if b"xl/" in content[:2048]:
                    return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                if b"ppt/" in content[:2048]:
                    return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            return mime
    if content and _TEXT_SAFE.match(content):
        return "text/plain"
    return None


def _dify_file_type(mime_type: str) -> str:
    """Map MIME to Dify FileType; mirrors UploadFileResponse.Type.from_mime_type."""

    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("video/"):
        return "video"
    if mime_type.startswith("audio/"):
        return "audio"
    return "document"


def _safe_filename(name: Any) -> str:
    """Reject anything that could escape a directory or smuggle a control char."""

    if not isinstance(name, str) or not name.strip():
        raise FileAdapterError("filename must be a non-empty string")
    name = name.strip()
    if len(name) > 255:
        raise FileAdapterError("filename is too long (max 255 characters)")
    if "/" in name or "\\" in name or "\x00" in name:
        raise FileAdapterError("filename must not contain path separators")
    if name in (".", "..") or name.startswith("."):
        raise FileAdapterError(f"filename '{name}' is not allowed")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise FileAdapterError("filename must not contain control characters")
    return name


def _extension_of(filename: str, mime_type: str) -> str:
    if "." in filename and not filename.startswith("."):
        return "." + filename.rsplit(".", 1)[1].lower()
    guessed = mimetypes.guess_extension(mime_type) or ""
    return guessed


def _assert_allowlisted(host: str, allow: tuple[str, ...]) -> None:
    """Hard gate: only admin-allow-listed hosts are fetched at all.

    This (not the address check) is what stops an MCP client from using the
    gateway to reach the Dify API, a database, or any other internal service.
    """

    host = _normalize_host(host)
    if not any(host == a or host.endswith("." + a) for a in allow):
        raise FileAdapterError(
            f"host '{host}' is not in the trusted-host list "
            "(configure file_url_allow_hosts)"
        )


def _assert_host_address_safe(host: str, *, allow_private: bool) -> None:
    """Defense in depth on the resolved address, checked before the request.

    Loopback, link-local (cloud metadata 169.254.169.254 lives here), multicast,
    reserved and unspecified addresses are always refused. RFC1918 / ULA space
    is refused unless the admin set ``file_url_allow_private``, which exists for
    a LAN-hosted file server and is an explicit, deliberate decision.

    A narrow TOCTOU window remains (the resolver may answer differently when
    the connection is actually made), which is one reason URL input is off by
    default.
    """

    host = _normalize_host(host)
    if not host:
        raise FileAdapterError("URL has no host")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise FileAdapterError(f"could not resolve host '{host}'") from exc
    ips = {info[4][0] for info in infos}
    if not ips:
        raise FileAdapterError(f"host '{host}' resolved to no address")
    for ip in sorted(ips):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError as exc:
            raise FileAdapterError(f"host '{host}' resolved to an invalid address") from exc
        never_allowed = (
            addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
            or (addr.version == 6 and addr.is_site_local)
        )
        if never_allowed or (addr.is_private and not allow_private):
            raise FileAdapterError(
                f"host '{host}' resolves to a protected address ({ip})"
            )


def _normalize_host(host: str) -> str:
    return host.strip().strip("[]").lower().rstrip(".")


@dataclass
class _UploadCacheEntry:
    payload: dict[str, Any]
    expires_at: float


@dataclass
class FileAdapter:
    """Turn MCP file objects into Dify file parameters.

    ``upload`` is injected so the adapter stays unit-testable and holds no
    session reference.
    """

    upload: UploadFn
    limits: FileLimits = field(default_factory=FileLimits)
    #: (digest, filename, mime) -> uploaded payload, kept well under the
    #: signed-URL lifetime so a reused URL is still fetchable.
    _cache: dict[tuple[str, str, str], _UploadCacheEntry] = field(
        default_factory=dict, repr=False
    )
    #: Payload uploaded for the current request; a FileAdapter is built per
    #: tools/call so this bounds one call, not the process lifetime.
    _total_bytes: int = field(default=0, repr=False)
    cache_ttl: float = 120.0

    # --- public API ---------------------------------------------------------

    def resolve(self, value: Any, *, param_name: str = "file") -> dict[str, Any]:
        """Resolve one file argument to a Dify file parameter dict."""

        if not isinstance(value, dict):
            raise FileAdapterError(
                f"parameter '{param_name}' expects a file object "
                "({\"source\": \"base64\", ...} or {\"source\": \"url\", ...})"
            )
        return self._resolve_one(value, param_name)

    def resolve_list(
        self, value: Any, *, param_name: str = "files"
    ) -> list[dict[str, Any]]:
        """Resolve a files argument, preserving order and dropping nothing."""

        if isinstance(value, dict):
            # A single object for a `files` param is a common client shortcut;
            # treat it as a one-element list rather than failing the call.
            value = [value]
        if not isinstance(value, list):
            raise FileAdapterError(
                f"parameter '{param_name}' expects a list of file objects"
            )
        if not value:
            raise FileAdapterError(f"parameter '{param_name}' received an empty file list")
        if len(value) > self.limits.max_files_per_call:
            raise FileAdapterError(
                f"parameter '{param_name}' has {len(value)} files, "
                f"limit is {self.limits.max_files_per_call}"
            )
        return [self._resolve_one(v, param_name) for v in value]

    # --- internals ----------------------------------------------------------

    def _check_host(self, host: str, allow: tuple[str, ...]) -> None:
        _assert_allowlisted(host, allow)
        _assert_host_address_safe(host, allow_private=self.limits.url_allow_private)

    def _resolve_one(self, value: dict[str, Any], param_name: str) -> dict[str, Any]:
        source = value.get("source")
        if source == "base64":
            return self._from_base64(value, param_name)
        if source == "url":
            return self._from_url(value, param_name)
        if source == "dify_file":
            # An endpoint cannot verify that an arbitrary file id belongs to
            # this workspace/caller, and the download endpoint checks only the
            # signature — so passing ids through would allow cross-workspace
            # file reads. Refuse instead of guessing.
            raise FileAdapterError(
                "file source 'dify_file' is not supported: an endpoint plugin "
                "cannot verify that a client-supplied file id belongs to this "
                "workspace; send the file as base64 (or an allow-listed URL)"
            )
        raise FileAdapterError(
            f"parameter '{param_name}' has unsupported file source "
            f"{source!r}; expected 'base64' or 'url'"
        )

    def _from_base64(self, value: dict[str, Any], param_name: str) -> dict[str, Any]:
        filename = _safe_filename(value.get("filename"))
        data = value.get("data")
        if not isinstance(data, str):
            raise FileAdapterError(f"parameter '{param_name}' is missing base64 'data'")
        declared = value.get("mimeType") or value.get("mime_type")

        # Clients commonly send a data URL; strip the prefix rather than
        # failing, and let it fill in a missing mime type.
        if data.startswith("data:"):
            header, _, data = data.partition(",")
            if ";base64" not in header:
                raise FileAdapterError(
                    f"parameter '{param_name}' data URL is not base64-encoded"
                )
            if not declared and ";" in header:
                candidate = header.split(":", 1)[1].split(";", 1)[0].strip()
                if candidate and candidate != "application/octet-stream":
                    declared = candidate

        padded = data.strip()
        # Tolerate whitespace/newlines from JSON-embedded base64, nothing else.
        compact = "".join(padded.split())
        try:
            content = base64.b64decode(compact, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise FileAdapterError(
                f"parameter '{param_name}' contains invalid base64 data"
            ) from exc

        logger.info(
            "File input param='%s' name='%s' source=base64 declared_bytes=%d decoded_bytes=%d",
            param_name, filename, len(compact), len(content),
        )
        mime = self._finalize_mime(content, declared, filename, param_name)
        return self._upload(filename, content, mime, digest=compact)

    def _from_url(self, value: dict[str, Any], param_name: str) -> dict[str, Any]:
        if not self.limits.url_enabled:
            raise FileAdapterError(
                "file source 'url' is disabled; enable it by setting "
                "file_url_enabled=true together with file_url_allow_hosts"
            )
        url = value.get("url")
        if not isinstance(url, str) or not url.strip():
            raise FileAdapterError(f"parameter '{param_name}' is missing 'url'")

        content, final_url = self._download(url.strip(), param_name)
        filename = value.get("filename")
        if not filename:
            tail = urlsplit(final_url).path.rsplit("/", 1)[-1]
            filename = tail or "downloaded-file"
        filename = _safe_filename(filename)
        mime = self._finalize_mime(
            content, value.get("mimeType") or value.get("mime_type"), filename, param_name
        )
        logger.info(
            "File input param='%s' name='%s' source=url bytes=%d",
            param_name, filename, len(content),
        )
        return self._upload(filename, content, mime, digest=content)

    def _download(self, url: str, param_name: str) -> tuple[bytes, str]:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise FileAdapterError(
                f"parameter '{param_name}': only http(s) URLs are allowed, got "
                f"{parts.scheme or '(none)'}"
            )
        allow = self.limits.url_allow_hosts
        if not allow:
            raise FileAdapterError(
                "file URL fetching has no allowed hosts configured "
                "(set file_url_allow_hosts)"
            )
        self._check_host(parts.hostname or "", allow)

        limit = self.limits.max_file_bytes
        timeout = httpx.Timeout(
            self.limits.download_timeout_seconds, connect=min(10.0, self.limits.download_timeout_seconds)
        )
        try:
            with httpx.Client(
                timeout=timeout, follow_redirects=False, trust_env=False
            ) as client:
                current = url
                for _ in range(6):
                    with client.stream("GET", current) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get("location")
                            if not location:
                                raise FileAdapterError(
                                    f"parameter '{param_name}': redirect without location"
                                )
                            current = str(httpx.URL(current).join(location))
                            # Re-validate the redirect target: a public host
                            # must not bounce to an internal one.
                            redirect = urlsplit(current)
                            if redirect.scheme not in ("http", "https"):
                                raise FileAdapterError(
                                    f"parameter '{param_name}': redirect to "
                                    f"{redirect.scheme or '(none)'} is not allowed"
                                )
                            self._check_host(redirect.hostname or "", allow)
                            continue
                        if response.status_code >= 400:
                            raise FileAdapterError(
                                f"parameter '{param_name}': URL returned "
                                f"status {response.status_code}"
                            )
                        header_len = response.headers.get("content-length")
                        if header_len and int(header_len) > limit:
                            raise FileAdapterError(
                                f"parameter '{param_name}': file exceeds "
                                f"{limit} bytes"
                            )
                        chunks: list[bytes] = []
                        total = 0
                        for chunk in response.iter_bytes():
                            total += len(chunk)
                            if total > limit:
                                raise FileAdapterError(
                                    f"parameter '{param_name}': file exceeds "
                                    f"{limit} bytes"
                                )
                            chunks.append(chunk)
                        return b"".join(chunks), current
        except FileAdapterError:
            raise
        except httpx.TimeoutException as exc:
            raise FileAdapterError(
                f"parameter '{param_name}': URL download timed out after "
                f"{self.limits.download_timeout_seconds}s"
            ) from exc
        except httpx.HTTPError as exc:
            # httpx exception text can embed the URL; keep the class + reason only.
            raise FileAdapterError(
                f"parameter '{param_name}': could not download the URL "
                f"({type(exc).__name__})"
            ) from exc
        raise FileAdapterError(f"parameter '{param_name}': too many redirects")

    def _finalize_mime(
        self,
        content: bytes,
        declared: Any,
        filename: str,
        param_name: str,
    ) -> str:
        if not content:
            raise FileAdapterError(f"parameter '{param_name}' contains an empty file")
        if len(content) > self.limits.max_file_bytes:
            raise FileAdapterError(
                f"parameter '{param_name}': file is {len(content)} bytes, "
                f"limit is {self.limits.max_file_bytes}"
            )
        sniffed = _sniff_mime(content)
        if declared:
            if not isinstance(declared, str) or "/" not in declared:
                raise FileAdapterError(
                    f"parameter '{param_name}' has an invalid mimeType {declared!r}"
                )
            declared = declared.split(";", 1)[0].strip().lower()
            if sniffed and sniffed != declared and not _compatible(sniffed, declared):
                raise FileAdapterError(
                    f"parameter '{param_name}' mimeType '{declared}' does not match "
                    f"the file content ('{sniffed}')"
                )
            return declared
        if sniffed:
            return sniffed
        guessed = mimetypes.guess_type(filename)[0]
        return guessed or "application/octet-stream"

    def _upload(
        self, filename: str, content: bytes, mime: str, *, digest: str | bytes
    ) -> dict[str, Any]:
        raw_digest = digest.encode() if isinstance(digest, str) else digest
        key = (hashlib.sha256(raw_digest).hexdigest()[:32], filename, mime)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and cached.expires_at > now:
            logger.info(
                "File reuse name='%s' mime=%s bytes=%d (already uploaded this call)",
                filename, mime, len(content),
            )
            return dict(cached.payload)

        if self._total_bytes + len(content) > self.limits.max_total_bytes:
            raise FileAdapterError(
                f"total file payload would exceed {self.limits.max_total_bytes} bytes"
            )
        try:
            uploaded = self.upload(filename, content, mime)
        except FileAdapterError:
            raise
        except Exception:
            # The SDK error text embeds the signed upload URL; never forward it.
            logger.exception("Dify file upload failed for parameter file")
            raise FileAdapterError(
                f"uploading '{filename}' to Dify failed"
            ) from None

        url = getattr(uploaded, "preview_url", None)
        if not url:
            raise FileAdapterError(
                "Dify returned no file URL; the plugin cannot pass the file to "
                "the tool (is FILES_URL configured on the Dify side?)"
            )
        payload = {
            "dify_model_identity": DIFY_FILE_IDENTITY,
            "url": url,
            "mime_type": mime,
            "filename": filename,
            "extension": _extension_of(filename, mime),
            "size": len(content),
            "type": _dify_file_type(mime),
        }
        # Prune on write: the adapter lives as long as the request handler.
        for stale in [k for k, v in self._cache.items() if v.expires_at <= now]:
            self._cache.pop(stale, None)
        self._cache[key] = _UploadCacheEntry(payload, now + self.cache_ttl)
        self._total_bytes += len(content)
        logger.info(
            "File uploaded to Dify name='%s' mime=%s bytes=%d type=%s",
            filename, mime, len(content), payload["type"],
        )
        return dict(payload)


def _compatible(sniffed: str, declared: str) -> bool:
    """Tolerate honest imprecision (text/* vs a specific text type)."""

    if sniffed.startswith("text/") and declared.startswith("text/"):
        return True
    # zip container vs an Office XML type (or vice versa).
    zip_family = ("application/zip", "application/octet-stream")
    return sniffed in zip_family and declared.startswith("application/vnd")
