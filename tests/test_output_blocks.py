"""Multimodal output: Dify ToolInvokeMessage stream -> MCP content blocks."""

import base64

from dify_plugin.entities.tool import ToolInvokeMessage

from core.mcp_handler import dispatch
from core.tool_adapter import format_invoke_content, format_invoke_result

from fixtures import FakeFileUpload, string_only_tool

TEXT = ToolInvokeMessage(
    type=ToolInvokeMessage.MessageType.TEXT,
    message=ToolInvokeMessage.TextMessage(text="hello"),
)
JSON = ToolInvokeMessage(
    type=ToolInvokeMessage.MessageType.JSON,
    message=ToolInvokeMessage.JsonMessage(json_object={"a": 1}),
)
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"z" * 20
PDF_BYTES = b"%PDF-1.4\n body \n%%EOF"


def _image(mime: str | None = None) -> ToolInvokeMessage:
    meta = {"mime_type": mime} if mime else None
    return ToolInvokeMessage(
        type=ToolInvokeMessage.MessageType.IMAGE,
        message=ToolInvokeMessage.BlobMessage(blob=PNG_BYTES),
        meta=meta,
    )


def test_text_message_becomes_text_block():
    assert format_invoke_content([TEXT]) == [{"type": "text", "text": "hello"}]


def test_json_message_is_bare_json_without_prefix():
    blocks = format_invoke_content([JSON])
    assert blocks == [{"type": "text", "text": '{"a": 1}'}]


def test_image_message_becomes_image_block_with_base64():
    blocks = format_invoke_content([_image("image/png")])
    assert blocks[0]["type"] == "image"
    assert blocks[0]["mimeType"] == "image/png"
    assert base64.b64decode(blocks[0]["data"]) == PNG_BYTES


def test_image_without_mime_defaults_to_png():
    blocks = format_invoke_content([_image()])
    assert blocks[0]["mimeType"] == "image/png"


def test_non_image_inline_blob_is_reported_not_faked_as_image():
    # 2024-11-05 has no content block for arbitrary inline bytes; an `image`
    # block with mimeType application/pdf would be a lie clients cannot render.
    msg = ToolInvokeMessage(
        type=ToolInvokeMessage.MessageType.BLOB,
        message=ToolInvokeMessage.BlobMessage(blob=PDF_BYTES),
        meta={"mime_type": "application/pdf"},
    )
    blocks = format_invoke_content([msg])
    assert blocks[0]["type"] == "text"
    assert "application/pdf" in blocks[0]["text"]
    assert str(len(PDF_BYTES)) in blocks[0]["text"]


def test_oversized_blob_degrades_to_explicit_note():
    msg = ToolInvokeMessage(
        type=ToolInvokeMessage.MessageType.BLOB,
        message=ToolInvokeMessage.BlobMessage(blob=b"x" * 5000),
        meta={"mime_type": "image/png"},
    )
    blocks = format_invoke_content([msg], inline_max_bytes=100)
    assert blocks[0]["type"] == "text"
    assert "exceeds the 100 byte inline limit" in blocks[0]["text"]
    assert "image/png" in blocks[0]["text"]


def test_blob_chunks_are_reassembled_in_sequence_order():
    def chunk(seq, data, end):
        return ToolInvokeMessage(
            type=ToolInvokeMessage.MessageType.BLOB_CHUNK,
            message=ToolInvokeMessage.BlobChunkMessage(
                id="b1", sequence=seq, total_length=6, blob=data, end=end
            ),
            meta={"mime_type": "image/jpeg"},
        )

    blocks = format_invoke_content([chunk(1, b"ABC", False), chunk(0, b"DEF", True)])
    assert blocks[0]["type"] == "image"
    assert base64.b64decode(blocks[0]["data"]) == b"DEFABC"
    assert blocks[0]["mimeType"] == "image/jpeg"


def test_non_image_blob_chunks_degrade_to_text():
    def chunk(seq, data, end):
        return ToolInvokeMessage(
            type=ToolInvokeMessage.MessageType.BLOB_CHUNK,
            message=ToolInvokeMessage.BlobChunkMessage(
                id="b1", sequence=seq, total_length=6, blob=data, end=end
            ),
            meta={"mime_type": "application/pdf"},
        )

    blocks = format_invoke_content([chunk(0, b"ABC", False), chunk(1, b"DEF", True)])
    assert blocks[0]["type"] == "text"
    assert "application/pdf" in blocks[0]["text"]


def test_image_link_becomes_accessible_url_text():
    msg = ToolInvokeMessage(
        type=ToolInvokeMessage.MessageType.IMAGE_LINK,
        message=ToolInvokeMessage.TextMessage(text="https://x.test/pic.png"),
    )
    blocks = format_invoke_content([msg])
    assert blocks[0]["type"] == "text"
    assert "https://x.test/pic.png" in blocks[0]["text"]
    assert "image/png" in blocks[0]["text"]


def test_file_message_with_url_is_surfaced():
    msg = ToolInvokeMessage(
        type=ToolInvokeMessage.MessageType.FILE,
        message=ToolInvokeMessage.TextMessage(text="ignored"),
        meta={"file": {"url": "https://f.test/r.pdf?sign=1", "filename": "r.pdf",
                        "mime_type": "application/pdf"}},
    )
    blocks = format_invoke_content([msg])
    assert "r.pdf" in blocks[0]["text"] and "https://f.test/r.pdf" in blocks[0]["text"]


def test_file_message_without_url_is_reported_not_dropped():
    msg = ToolInvokeMessage(
        type=ToolInvokeMessage.MessageType.FILE,
        message=ToolInvokeMessage.TextMessage(text="ignored"),
        meta=None,
    )
    blocks = format_invoke_content([msg])
    assert "no accessible URL" in blocks[0]["text"]


def test_variable_message_is_preserved():
    msg = ToolInvokeMessage(
        type=ToolInvokeMessage.MessageType.VARIABLE,
        message=ToolInvokeMessage.VariableMessage(
            variable_name="answer", variable_value={"k": "v"}
        ),
    )
    blocks = format_invoke_content([msg])
    assert blocks[0]["text"] == 'answer={"k": "v"}'


def test_mixed_output_keeps_original_order():
    blocks = format_invoke_content([TEXT, _image("image/png"), JSON])
    assert [b["type"] for b in blocks] == ["text", "image", "text"]


def test_unknown_message_type_degrades_to_text():
    msg = ToolInvokeMessage(
        type=ToolInvokeMessage.MessageType.LOG,
        message=ToolInvokeMessage.LogMessage(
            label="trace", status="start", data={"secret": "x"}
        ),
    )
    assert format_invoke_content([msg]) == [{"type": "text", "text": ""}]


def test_empty_stream_yields_one_empty_text_block():
    assert format_invoke_content([]) == [{"type": "text", "text": ""}]


def test_format_invoke_result_helper_still_returns_text():
    assert format_invoke_result([TEXT, JSON]) == 'hello\n{"a": 1}'


# --- dispatcher level: multimodal content must reach the MCP result ---

def test_tools_call_returns_image_blocks_not_placeholder(fake_invoke):
    def invoke(tool, params):
        return [TEXT, _image("image/png")]

    resp = dispatch(
        "tools/call", 1,
        {"name": "echo", "arguments": {"text": "x"}},
        {"tools": [string_only_tool()]},
        invoke,
        FakeFileUpload(),
    )
    content = resp["result"]["content"]
    assert resp["result"]["isError"] is False
    assert content[0] == {"type": "text", "text": "hello"}
    assert content[1]["type"] == "image"
    assert "Not support message type" not in str(content)


def test_text_only_tools_call_unchanged_for_clients(fake_invoke):
    resp = dispatch(
        "tools/call", 2,
        {"name": "echo", "arguments": {"text": "x"}},
        {"tools": [string_only_tool()]},
        lambda t, p: [TEXT],
        FakeFileUpload(),
    )
    assert resp["result"]["content"] == [{"type": "text", "text": "hello"}]
