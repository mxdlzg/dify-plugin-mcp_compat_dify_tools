"""File adapter: base64 input, limits, MIME checks, and URL safety.

Network access is fully faked — these tests prove the SSRF gates refuse before
any socket is opened, so a passing run means no real request was made.
"""

import base64
import ipaddress
import socket as _socket

import httpx
import pytest

from core.file_adapter import (
    DIFY_FILE_IDENTITY,
    FileAdapter,
    FileAdapterError,
    FileLimits,
    _assert_allowlisted,
    _assert_host_address_safe,
    _sniff_mime,
)

from fixtures import FakeFileUpload


@pytest.fixture(autouse=True)
def _controlled_dns(monkeypatch):
    """No test in this module may touch real DNS.

    IP literals resolve to themselves (as the real resolver does) so the
    address gate is exercised; any other name is either a known public host or
    fails to resolve.
    """

    def fake(host, port, *a, **kw):
        literal = host.strip("[]")
        try:
            ipaddress.ip_address(literal)
        except ValueError:
            pass
        else:
            family = 2 if ":" not in literal else 10
            return [(family, 1, 6, "", (literal, 0))]
        if host in ("files.example.com",):
            return [(2, 1, 6, "", ("93.184.216.34", 0))]
        if host == "localhost":
            return [(2, 1, 6, "", ("127.0.0.1", 0))]
        raise _socket.gaierror("unresolved in test")

    monkeypatch.setattr(_socket, "getaddrinfo", fake)

PDF = b"%PDF-1.4\n fake pdf body \n%%EOF"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 40


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _adapter(limits=None, **limit_kwargs):
    upload = FakeFileUpload()
    resolved_limits = limits or FileLimits(**limit_kwargs)
    return FileAdapter(upload, resolved_limits), upload


# --- base64 happy paths ---

def test_pdf_base64_becomes_a_dify_file_parameter():
    adapter, upload = _adapter()
    result = adapter.resolve(
        {"source": "base64", "filename": "report.pdf",
         "mimeType": "application/pdf", "data": b64(PDF)},
        param_name="file",
    )
    assert result["dify_model_identity"] == DIFY_FILE_IDENTITY
    assert result["mime_type"] == "application/pdf"
    assert result["type"] == "document"
    assert result["filename"] == "report.pdf"
    assert result["extension"] == ".pdf"
    assert result["size"] == len(PDF)
    # url must be the signed URL Dify handed back, not our own wrapper
    assert result["url"].startswith("https://files.invalid/files/tools/tool-file-1.pdf?")
    assert upload.calls == [{"filename": "report.pdf", "bytes": len(PDF),
                             "mime": "application/pdf"}]
    # The MCP wrapper must NOT be what reaches the tool.
    assert "source" not in result and "data" not in result


def test_png_is_typed_as_image():
    adapter, _ = _adapter()
    result = adapter.resolve(
        {"source": "base64", "filename": "a.png", "data": b64(PNG)}
    )
    assert result["type"] == "image"
    assert result["mime_type"] == "image/png"


def test_missing_mime_is_sniffed():
    adapter, _ = _adapter()
    result = adapter.resolve(
        {"source": "base64", "filename": "blob", "data": b64(PDF)}
    )
    assert result["mime_type"] == "application/pdf"


def test_data_url_prefix_is_tolerated():
    adapter, upload = _adapter()
    result = adapter.resolve(
        {"source": "base64", "filename": "r.pdf",
         "data": "data:application/pdf;base64," + b64(PDF)}
    )
    assert result["mime_type"] == "application/pdf"
    assert upload.calls[0]["bytes"] == len(PDF)


def test_multiple_files_keep_order_and_count():
    adapter, upload = _adapter()
    results = adapter.resolve_list([
        {"source": "base64", "filename": "a.txt", "mimeType": "text/plain",
         "data": b64(b"first")},
        {"source": "base64", "filename": "b.txt", "mimeType": "text/plain",
         "data": b64(b"second")},
    ], param_name="files")
    assert [r["filename"] for r in results] == ["a.txt", "b.txt"]
    assert [c["filename"] for c in upload.calls] == ["a.txt", "b.txt"]


def test_identical_file_is_uploaded_once_per_call():
    adapter, upload = _adapter()
    payload = {"source": "base64", "filename": "x.pdf",
               "mimeType": "application/pdf", "data": b64(PDF)}
    first = adapter.resolve(payload)
    second = adapter.resolve(payload)
    assert first == second
    assert len(upload.calls) == 1


# --- validation failures ---

def test_invalid_base64_rejected():
    adapter, upload = _adapter()
    with pytest.raises(FileAdapterError, match="invalid base64"):
        adapter.resolve({"source": "base64", "filename": "a.pdf", "data": "!!!not base64!!!"})
    assert upload.calls == []


def test_empty_file_rejected():
    adapter, upload = _adapter()
    with pytest.raises(FileAdapterError, match="empty file"):
        adapter.resolve({"source": "base64", "filename": "a.pdf", "data": b64(b"")})
    assert upload.calls == []


def test_oversized_file_rejected():
    adapter, upload = _adapter(max_file_bytes=10)
    with pytest.raises(FileAdapterError, match="limit is 10"):
        adapter.resolve({"source": "base64", "filename": "a.pdf", "data": b64(PDF)})
    assert upload.calls == []


def test_too_many_files_rejected():
    adapter, _ = _adapter(max_files_per_call=1)
    with pytest.raises(FileAdapterError, match="limit is 1"):
        adapter.resolve_list([
            {"source": "base64", "filename": "a.txt", "data": b64(b"a")},
            {"source": "base64", "filename": "b.txt", "data": b64(b"b")},
        ])


def test_total_budget_across_files_enforced():
    adapter, upload = _adapter(max_total_bytes=6, max_file_bytes=10)
    with pytest.raises(FileAdapterError, match="would exceed"):
        adapter.resolve_list([
            {"source": "base64", "filename": "a.txt", "data": b64(b"12345")},
            {"source": "base64", "filename": "b.txt", "data": b64(b"12345")},
        ])


def test_mime_mismatch_rejected():
    adapter, upload = _adapter()
    with pytest.raises(FileAdapterError, match="does not match"):
        adapter.resolve({"source": "base64", "filename": "x.png",
                         "mimeType": "image/png", "data": b64(PDF)})
    assert upload.calls == []


def test_path_traversal_filename_rejected():
    adapter, _ = _adapter()
    for name in ("../../etc/passwd", "a/b.pdf", "..", ".env", "a\x00b.pdf"):
        with pytest.raises(FileAdapterError):
            adapter.resolve({"source": "base64", "filename": name, "data": b64(b"hi")})


def test_missing_required_file_object_shape_rejected():
    adapter, _ = _adapter()
    with pytest.raises(FileAdapterError, match="expects a file object"):
        adapter.resolve("just a string")
    with pytest.raises(FileAdapterError, match="unsupported file source"):
        adapter.resolve({"source": "path", "path": "/etc/passwd"})


def test_dify_file_reference_refused():
    # An endpoint cannot verify a client-supplied file id belongs to this
    # workspace; the download endpoint only checks the signature.
    adapter, upload = _adapter()
    with pytest.raises(FileAdapterError, match="not supported"):
        adapter.resolve({"source": "dify_file", "fileId": "abc123"})
    assert upload.calls == []


def test_upload_failure_hides_sdk_details():
    upload = FakeFileUpload(error=RuntimeError("signed url secret-token expired"))
    adapter = FileAdapter(upload, FileLimits())
    with pytest.raises(FileAdapterError) as exc:
        adapter.resolve({"source": "base64", "filename": "a.pdf", "data": b64(PDF)})
    assert "secret-token" not in str(exc.value)


def test_missing_preview_url_is_reported():
    class NoUrl:
        id, name, size, extension, mime_type, type = "1", "a", 1, ".pdf", "application/pdf", "document"
        preview_url = None

    adapter = FileAdapter(lambda *a: NoUrl(), FileLimits())
    with pytest.raises(FileAdapterError, match="no file URL"):
        adapter.resolve({"source": "base64", "filename": "a.pdf", "data": b64(PDF)})


# --- URL safety ---

URL_LIMITS = FileLimits(
    url_enabled=True,
    url_allow_hosts=("files.example.com",),
    max_file_bytes=1024,
)


def _patch_client(monkeypatch, handler) -> None:
    """Make file_adapter's httpx.Client use a fake transport (no real network)."""
    import httpx
    from core import file_adapter as fa

    real = httpx.Client

    def factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(**kwargs)

    monkeypatch.setattr(fa.httpx, "Client", factory)


def test_allowlisted_url_is_fetched_and_uploaded(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=PDF,
                              headers={"content-type": "application/pdf"})

    _patch_client(monkeypatch, handler)
    upload = FakeFileUpload()
    adapter = FileAdapter(upload, URL_LIMITS)
    result = adapter.resolve(
        {"source": "url", "url": "https://files.example.com/report.pdf"}
    )
    assert result["filename"] == "report.pdf"
    assert result["mime_type"] == "application/pdf"
    assert result["size"] == len(PDF)
    assert upload.calls[0]["filename"] == "report.pdf"


def test_redirect_to_protected_host_refused(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "files.example.com" in str(request.url):
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
        return httpx.Response(200, content=b"stolen")

    _patch_client(monkeypatch, handler)
    adapter = FileAdapter(FakeFileUpload(), URL_LIMITS)
    with pytest.raises(FileAdapterError, match="not in the trusted-host list"):
        adapter.resolve({"source": "url", "url": "https://files.example.com/a.pdf"})
    # Only the allow-listed hop was ever requested.
    assert calls == ["https://files.example.com/a.pdf"]


def test_oversized_download_refused(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (URL_LIMITS.max_file_bytes + 1))

    _patch_client(monkeypatch, handler)
    adapter = FileAdapter(FakeFileUpload(), URL_LIMITS)
    with pytest.raises(FileAdapterError, match="exceeds"):
        adapter.resolve({"source": "url", "url": "https://files.example.com/big.pdf"})


def test_http_error_status_refused(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    _patch_client(monkeypatch, handler)
    adapter = FileAdapter(FakeFileUpload(), URL_LIMITS)
    with pytest.raises(FileAdapterError, match="status 404"):
        adapter.resolve({"source": "url", "url": "https://files.example.com/gone.pdf"})


def test_download_timeout_reported(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    _patch_client(monkeypatch, handler)
    adapter = FileAdapter(FakeFileUpload(), URL_LIMITS)
    with pytest.raises(FileAdapterError, match="timed out"):
        adapter.resolve({"source": "url", "url": "https://files.example.com/slow.pdf"})


def test_broken_download_does_not_leak_url(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused to internal-host:5432")

    _patch_client(monkeypatch, handler)
    adapter = FileAdapter(FakeFileUpload(), URL_LIMITS)
    with pytest.raises(FileAdapterError) as exc:
        adapter.resolve({"source": "url", "url": "https://files.example.com/x.pdf"})
    assert "internal-host" not in str(exc.value)


def test_url_disabled_by_default():
    adapter, upload = _adapter()
    with pytest.raises(FileAdapterError, match="disabled"):
        adapter.resolve({"source": "url", "url": "https://files.example.com/a.pdf"})
    assert upload.calls == []


def test_non_http_scheme_rejected():
    adapter, upload = _adapter(limits=URL_LIMITS)
    for url in ("file:///etc/passwd", "gopher://x/y", "//etc/passwd"):
        with pytest.raises(FileAdapterError, match="only http"):
            adapter.resolve({"source": "url", "url": url})
    assert upload.calls == []


def test_unlisted_host_rejected_before_any_dns_lookup(monkeypatch):
    # The allow-list is the primary gate: it must refuse without even
    # resolving, so an attacker cannot probe internal names.
    import socket as _socket

    def boom(*a, **kw):
        raise AssertionError("allow-list gate must run before DNS")

    monkeypatch.setattr(_socket, "getaddrinfo", boom)
    adapter, upload = _adapter(limits=URL_LIMITS)
    with pytest.raises(FileAdapterError, match="not in the trusted-host list"):
        adapter.resolve({"source": "url", "url": "https://evil.example.org/a.pdf"})
    with pytest.raises(FileAdapterError, match="not in the trusted-host list"):
        adapter.resolve({"source": "url", "url": "http://127.0.0.1:5001/console/api/x"})
    assert upload.calls == []


def test_protected_addresses_refused_even_when_allowlisted(monkeypatch):
    # Defense in depth: a listed name must not resolve to loopback/link-local.
    def fake_getaddrinfo(host, port, *a, **kw):
        return [(2, 1, 6, "", ("169.254.169.254", 0))]

    monkeypatch.setattr(_socket, "getaddrinfo", fake_getaddrinfo)
    adapter, upload = _adapter(limits=URL_LIMITS)
    with pytest.raises(FileAdapterError, match="protected address"):
        adapter.resolve({"source": "url", "url": "https://files.example.com/a.pdf"})
    assert upload.calls == []


def test_private_range_allowed_only_with_explicit_opt_in(monkeypatch):
    import socket as _socket

    def fake_getaddrinfo(host, port, *a, **kw):
        return [(2, 1, 6, "", ("192.168.195.200", 0))]

    monkeypatch.setattr(_socket, "getaddrinfo", fake_getaddrinfo)
    adapter, _ = _adapter(limits=URL_LIMITS)
    with pytest.raises(FileAdapterError, match="protected address"):
        adapter.resolve({"source": "url", "url": "https://files.example.com/a.pdf"})

    permissive = FileLimits(
        url_enabled=True,
        url_allow_hosts=("files.example.com",),
        url_allow_private=True,
        max_file_bytes=1024,
    )
    lenient_adapter, upload = _adapter(limits=permissive)
    import httpx

    _patch_client(monkeypatch, lambda request: httpx.Response(200, content=PDF))
    result = lenient_adapter.resolve(
        {"source": "url", "url": "https://files.example.com/a.pdf"}
    )
    assert result["size"] == len(PDF)
    assert len(upload.calls) == 1


def test_allowlisted_public_host_passes_address_check(monkeypatch):
    import socket as _socket

    def fake_getaddrinfo(host, port, *a, **kw):
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(_socket, "getaddrinfo", fake_getaddrinfo)
    _assert_host_address_safe("files.example.com", allow_private=False)


def test_dns_failure_reported_not_raised_raw(monkeypatch):
    import socket as _socket

    def fake_getaddrinfo(host, port, *a, **kw):
        raise _socket.gaierror("no such host")

    monkeypatch.setattr(_socket, "getaddrinfo", fake_getaddrinfo)
    adapter, _ = _adapter(limits=URL_LIMITS)
    with pytest.raises(FileAdapterError, match="could not resolve"):
        adapter.resolve({"source": "url", "url": "https://files.example.com/a.pdf"})


def test_literal_protected_addresses_are_refused_even_when_allowlisted():
    # Even if an admin allow-lists "localhost", loopback/link-local stay
    # refused; only RFC1918 is opt-in, and never these.
    for host in ("localhost", "127.0.0.1", "169.254.169.254", "0.0.0.0", "[::1]"):
        with pytest.raises(FileAdapterError):
            _assert_host_address_safe(host, allow_private=True)


def test_allowlist_matches_subdomains_only():
    _assert_allowlisted("cdn.files.example.com", ("files.example.com",))
    with pytest.raises(FileAdapterError, match="trusted-host"):
        _assert_allowlisted("notfiles.example.com", ("files.example.com",))


def test_sniff_recognizes_office_zip_variants():
    zip_body = b"PK\x03\x04" + b"word/document.xml" + b"\x00" * 32
    assert _sniff_mime(zip_body).endswith("wordprocessingml.document")


# --- the shipped group yaml must be installable ---

def test_group_yaml_settings_types_are_allowed_by_dify():
    """Dify's console API has no `number` type for endpoint settings, so a
    numeric setting silently passes local tests and then makes the whole
    plugin refuse to install (`type: number` fails pydantic enum validation
    on install/upload/package). Only these types exist:
    """
    import yaml
    from pathlib import Path

    allowed = {
        "secret-input", "text-input", "select", "boolean",
        "app-selector", "model-selector", "array[tools]",
    }
    group = yaml.safe_load(
        (Path(__file__).parents[1] / "group" / "mcp_compat_dify_tools.yaml")
        .read_text(encoding="utf-8")
    )
    offenders = {s["name"]: s["type"] for s in group["settings"] if s["type"] not in allowed}
    assert offenders == {}


def test_numeric_limits_parse_from_string_settings():
    """With text-input settings the values arrive as strings — and `default`
    in the yaml is a string too, so a fresh endpoint must still get real caps.
    """
    limits = FileLimits.from_settings({
        "file_max_bytes": "1048576",
        "file_max_count": "3",
        "file_max_total_bytes": "2097152",
        "file_url_timeout_seconds": "15",
        "file_url_enabled": "true",
        "file_url_allow_private": "false",
        "file_url_allow_hosts": "Files.Example.com, cdn.example.com",
    })
    assert limits.max_file_bytes == 1048576
    assert limits.max_files_per_call == 3
    assert limits.max_total_bytes == 2097152
    assert limits.download_timeout_seconds == 15.0
    assert limits.url_enabled is True
    assert limits.url_allow_private is False
    assert limits.url_allow_hosts == ("files.example.com", "cdn.example.com")


def test_numeric_limits_fall_back_on_junk_and_out_of_range():
    assert FileLimits.from_settings({"file_max_bytes": "abc"}).max_file_bytes == 5 * 1024 * 1024
    assert FileLimits.from_settings({"file_max_bytes": ""}).max_file_bytes == 5 * 1024 * 1024
    # Above the daemon's own 15 MiB cap: refuse the value, keep the default.
    assert FileLimits.from_settings(
        {"file_max_bytes": str(60 * 1024 * 1024)}
    ).max_file_bytes == 5 * 1024 * 1024
