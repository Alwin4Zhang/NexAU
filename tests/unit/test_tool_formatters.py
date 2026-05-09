from __future__ import annotations

from xml.etree import ElementTree

import pytest

from nexau.archs.tool import tool as tool_module
from nexau.archs.tool.formatters import ToolFormatterContext, resolve_tool_formatter
from nexau.archs.tool.formatters.markdown import format_tool_output_as_markdown
from nexau.archs.tool.formatters.shell import format_run_shell_command_output
from nexau.archs.tool.formatters.xml import _wrap_cdata, format_tool_output_as_xml
from nexau.archs.tool.tool import Tool


def _make_formatter_context(tool_output: object, *, is_error: bool = False) -> ToolFormatterContext:
    return ToolFormatterContext(
        tool_name="test_tool",
        tool_input={},
        tool_output=tool_output,
        tool_call_id="call_123",
        is_error=is_error,
    )


def test_resolve_tool_formatter_defaults_to_markdown_and_keeps_xml_alias() -> None:
    assert resolve_tool_formatter(None) is format_tool_output_as_markdown
    assert resolve_tool_formatter("markdown") is format_tool_output_as_markdown
    assert resolve_tool_formatter("xml") is format_tool_output_as_xml


def test_wrap_cdata_handles_cdata_terminator() -> None:
    wrapped = _wrap_cdata("alpha]]>beta")

    root = ElementTree.fromstring(f"<root>{wrapped}</root>")

    assert root.text == "\nalpha]]>beta\n"


def test_format_tool_output_as_xml_handles_cdata_terminator() -> None:
    formatted = format_tool_output_as_xml(
        _make_formatter_context({"status": "ok", "result": "alpha]]>beta"}),
    )

    assert isinstance(formatted, str)
    root = ElementTree.fromstring(formatted)
    body = root.find("body")
    assert body is not None
    assert body.attrib["field"] == "result"
    assert body.text == "\nalpha]]>beta\n"


def test_format_tool_output_as_xml_renders_empty_dict() -> None:
    formatted = format_tool_output_as_xml(_make_formatter_context({}))

    assert formatted == "<tool_result>\n</tool_result>"


def test_format_tool_output_as_markdown_renders_dict_without_xml_tags() -> None:
    formatted = format_tool_output_as_markdown(
        _make_formatter_context(
            {
                "status": "ok",
                "result": "Found 3 matches\nSecond line",
                "returnDisplay": "frontend only",
            }
        )
    )

    assert isinstance(formatted, str)
    assert "<tool_result>" not in formatted
    assert "## Tool Result" in formatted
    assert "### Metadata" in formatted
    assert "- `status`: ok" in formatted
    assert "### Body (`result`)" in formatted
    assert "Found 3 matches" in formatted
    assert "frontend only" not in formatted


def test_format_tool_output_as_markdown_unwraps_single_body_fields() -> None:
    assert format_tool_output_as_markdown(_make_formatter_context({"content": "Plain content"})) == "Plain content"
    assert format_tool_output_as_markdown(_make_formatter_context({"result": "Plain result"})) == "Plain result"


def test_tool_format_output_for_llm_falls_back_to_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = Tool(
        name="fallback_tool",
        description="desc",
        input_schema={"type": "object", "properties": {}},
        implementation=lambda: {"result": "ok"},
    )

    def broken_formatter(context: ToolFormatterContext) -> object:
        raise RuntimeError(f"formatter boom: {context.tool_name}")

    def fake_resolve(formatter: str | object | None) -> object:
        assert formatter == "markdown"
        return format_tool_output_as_markdown

    monkeypatch.setattr(tool, "_resolved_formatter", broken_formatter)
    monkeypatch.setattr(tool_module, "resolve_tool_formatter", fake_resolve)

    formatted = tool.format_output_for_llm(
        tool_input={},
        tool_output={"result": "fallback body", "status": "ok"},
        tool_call_id="call_123",
        is_error=False,
    )

    assert isinstance(formatted, str)
    assert "## Tool Result" in formatted
    assert "fallback body" in formatted


def test_tool_format_output_for_llm_falls_back_to_raw_output_when_markdown_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = Tool(
        name="fallback_tool",
        description="desc",
        input_schema={"type": "object", "properties": {}},
        implementation=lambda: {"result": "ok"},
    )

    raw_output = {"content": "raw body", "returnDisplay": "display only"}

    def broken_formatter(context: ToolFormatterContext) -> object:
        raise RuntimeError(f"formatter boom: {context.tool_name}")

    def broken_markdown_formatter(context: ToolFormatterContext) -> object:
        raise ValueError(f"markdown boom: {context.tool_call_id}")

    def fake_resolve(formatter: str | object | None) -> object:
        assert formatter == "markdown"
        return broken_markdown_formatter

    monkeypatch.setattr(tool, "_resolved_formatter", broken_formatter)
    monkeypatch.setattr(tool_module, "resolve_tool_formatter", fake_resolve)

    formatted = tool.format_output_for_llm(
        tool_input={},
        tool_output=raw_output,
        tool_call_id="call_123",
        is_error=False,
    )

    assert formatted == raw_output


def test_format_run_shell_command_output_flattens_stdout_stderr() -> None:
    formatted = format_run_shell_command_output(
        _make_formatter_context(
            {
                "content": "Output: hello\nwarn",
                "stdout": "\nhello\n",
                "stderr": "warn\n",
                "exit_code": 0,
                "interrupted": False,
                "timed_out": False,
            }
        )
    )

    assert formatted == "hello\nwarn"


def test_format_run_shell_command_output_flattens_background_info() -> None:
    formatted = format_run_shell_command_output(
        _make_formatter_context(
            {
                "content": "Background task started (pid: 123).",
                "backgroundPids": [123],
                "stdout": "",
                "stderr": "",
                "stdout_file": "/tmp/out/stdout.txt",
                "interrupted": False,
                "timed_out": False,
            }
        )
    )

    assert formatted == "Command running in background with ID: 123. Output is being written to: /tmp/out/stdout.txt"


def test_format_run_shell_command_output_marks_interrupted_commands() -> None:
    formatted = format_run_shell_command_output(
        _make_formatter_context(
            {
                "content": "Interrupted",
                "stdout": "partial output",
                "stderr": "",
                "interrupted": True,
                "timed_out": False,
                "error": {"message": "Command interrupted by stop request", "type": "SHELL_EXECUTE_ERROR"},
            },
            is_error=True,
        )
    )

    assert formatted == "partial output\n<error>Command was aborted before completion</error>"


def test_run_shell_command_example_yaml_uses_shell_formatter() -> None:
    tool = Tool.from_yaml(
        "examples/code_agent/tools/run_shell_command.tool.yaml",
        binding=lambda **_: {"result": "ok"},
    )

    assert tool.formatter == "nexau.archs.tool.formatters.shell:format_run_shell_command_output"

    formatted = tool.format_output_for_llm(
        tool_input={"command": "echo hello"},
        tool_output={
            "content": "Output: hello",
            "stdout": "hello",
            "stderr": "",
            "interrupted": False,
            "timed_out": False,
            "exit_code": 0,
        },
        tool_call_id="call_123",
        is_error=False,
    )

    assert formatted == "hello"
