"""Default Markdown formatter for tool outputs.

RFC-0017: Tool output flattening

Converts structured tool outputs into stable Markdown text so the LLM sees a
single, flat, truncation-friendly representation without XML-like tags that may
interfere with provider chat templates.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

from . import ToolFormatterContext, is_image_like_tool_output

_DISPLAY_ONLY_KEYS: frozenset[str] = frozenset({"returnDisplay"})


def format_tool_output_as_markdown(context: ToolFormatterContext) -> object:
    """Format a tool output as Markdown unless it should stay multimodal.

    RFC-0017: 默认 Markdown formatter

    1. 字符串输出直接透传，避免无意义包裹
    2. 图片 / multimodal 输出绕过 Markdown，保留既有多模态链路
    3. 其他 Dict / List / 标量输出统一转成稳定 Markdown 文本
    """

    sanitized_output = _strip_display_only_fields(context.tool_output)
    # RFC-0017: 单字段正文快捷路径。
    # 当工具输出在剥离 returnDisplay 后只剩一个正文键时，直接把值交给 LLM，
    # 避免多余 Markdown 外壳；这同样适用于 multimodal/image 值。
    direct_content = _unwrap_single_content_field(sanitized_output)
    if direct_content is not None:
        return direct_content

    if is_image_like_tool_output(sanitized_output):
        return sanitized_output

    if isinstance(sanitized_output, str):
        return sanitized_output

    return _render_markdown_document(sanitized_output, is_error=context.is_error)


def _unwrap_single_content_field(value: object) -> object | None:
    """Return the bare value for the single-body-field fast path.

    RFC-0017: 单字段直通捷径

    当剥离 display-only 字段后只剩 ``{"content": ...}`` 或
    ``{"result": ...}`` 时，直接返回其值，避免无意义 Markdown 外壳。
    """

    if not isinstance(value, dict):
        return None

    value_dict = cast(dict[str, object], value)
    keys = set(value_dict.keys())
    if keys == {"content"}:
        return value_dict["content"]

    if keys == {"result"}:
        return value_dict["result"]

    return None


def _strip_display_only_fields(value: object) -> object:
    if isinstance(value, list):
        return [_strip_display_only_fields(item) for item in cast(list[object], value)]

    if isinstance(value, dict):
        value_dict = cast(dict[str, object], value)
        return {key: _strip_display_only_fields(item) for key, item in value_dict.items() if key not in _DISPLAY_ONLY_KEYS}

    return value


def _render_markdown_document(value: object, *, is_error: bool) -> str:
    if isinstance(value, dict):
        return _render_mapping_document(cast(dict[str, object], value), is_error=is_error)

    body_text = _serialize_body_value(value)
    return "\n\n".join(["## Tool Result", _render_body_section("result", body_text)])


def _render_mapping_document(value: dict[str, object], *, is_error: bool) -> str:
    body_key = _select_body_key(value, is_error=is_error)
    meta_lines: list[str] = []
    body_section = ""

    for key, item in value.items():
        if body_key is not None and key == body_key:
            body_section = _render_body_section(key, _serialize_body_value(item))
            continue
        meta_line = _render_meta_line(key, item)
        if meta_line is not None:
            meta_lines.append(meta_line)

    document_sections = ["## Tool Result"]
    if meta_lines:
        document_sections.append("### Metadata\n" + "\n".join(meta_lines))
    if body_section:
        document_sections.append(body_section)
    return "\n\n".join(document_sections)


def _select_body_key(value: Mapping[str, object], *, is_error: bool) -> str | None:
    preferred_keys: tuple[str, ...]
    if is_error:
        preferred_keys = ("error", "traceback", "stderr", "message", "result", "content", "stdout")
    else:
        preferred_keys = ("result", "content", "stdout", "stderr", "message")

    for key in preferred_keys:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return key

    longest_multiline_key: str | None = None
    longest_multiline_length = -1
    for key, item in value.items():
        if isinstance(item, str) and "\n" in item and len(item) > longest_multiline_length:
            longest_multiline_key = key
            longest_multiline_length = len(item)
    return longest_multiline_key


def _render_meta_line(key: str, value: object) -> str | None:
    if value is None:
        return None

    if isinstance(value, bool):
        value_text = "true" if value else "false"
    elif isinstance(value, (str, int, float)):
        value_text = str(value)
    else:
        value_text = _serialize_meta_value(value)

    if "\n" in value_text:
        indented = _indent_block(value_text)
        return f"- `{key}`:\n{indented}"
    return f"- `{key}`: {value_text}"


def _render_body_section(field_name: str | None, body_text: str) -> str:
    if field_name:
        return f"### Body (`{field_name}`)\n\n{body_text}"
    return f"### Body\n\n{body_text}"


def _serialize_meta_value(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(value)


def _serialize_body_value(value: object) -> str:
    if isinstance(value, str):
        return value
    return _serialize_meta_value(value)


def _indent_block(text: str) -> str:
    return "\n".join(f"  {line}" if line else "" for line in text.splitlines())


__all__ = ["format_tool_output_as_markdown"]
