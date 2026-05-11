"""Unit tests for read_visual_file ffmpeg degradation paths."""

from __future__ import annotations

import base64
from unittest.mock import Mock

import pytest

from nexau.archs.sandbox.base_sandbox import CommandResult, FileOperationResult, SandboxStatus
from nexau.archs.tool.builtin.file_tools.read_visual_file import (
    _convert_svg_to_png_in_sandbox,
    _is_missing_inkscape,
    _read_image_file,
    _read_video_frames,
    read_visual_file,
)


class TestReadVisualFileFfmpegDegradation:
    def test_video_frame_extraction_reports_missing_ffmpeg(self) -> None:
        """RFC-0020: ffmpeg 缺失时视频路径返回可诊断错误。"""
        sandbox = Mock()
        sandbox.get_temp_dir.return_value = "/tmp"
        sandbox.join_path.side_effect = lambda base, child: f"{base.rstrip('/')}/{child}"
        sandbox.to_shell_path.side_effect = lambda path: str(path)
        sandbox.execute_shell.return_value = CommandResult(
            status=SandboxStatus.ERROR,
            stderr="ffmpeg: command not found",
            exit_code=127,
        )

        with pytest.raises(RuntimeError, match="ffmpeg not found"):
            _read_video_frames("/videos/sample.mp4", sandbox)

        sandbox.delete_file.assert_called_once()

    def test_video_frame_extraction_uses_sandbox_file_apis_and_sorts_frames(self) -> None:
        """RFC-0020: frame directory handling stays backend-neutral on Windows."""
        sandbox = Mock()
        sandbox.get_temp_dir.return_value = r"C:\Temp"
        sandbox.join_path.side_effect = lambda base, child: f"{base.rstrip('/\\')}\\{child}"
        sandbox.to_shell_path.side_effect = lambda path: str(path)
        sandbox.execute_shell.return_value = CommandResult(
            status=SandboxStatus.SUCCESS,
            stdout="",
            stderr="",
            exit_code=0,
        )
        sandbox.list_files.return_value = [
            Mock(path=r"C:\Temp\nexau_video_frames_test\frame_0002.jpg", is_file=True),
            Mock(path=r"C:\Temp\nexau_video_frames_test\frame_0001.jpg", is_file=True),
            Mock(path=r"C:\Temp\nexau_video_frames_test\note.txt", is_file=False),
        ]
        sandbox.read_file.side_effect = [
            FileOperationResult(status=SandboxStatus.SUCCESS, file_path="frame_0001.jpg", content=b"one", size=3),
            FileOperationResult(status=SandboxStatus.SUCCESS, file_path="frame_0002.jpg", content=b"two", size=3),
        ]

        result = _read_video_frames(r"C:\videos\sample.mp4", sandbox, frame_interval=5, max_frames=10)

        sandbox.create_directory.assert_called_once()
        created_dir = sandbox.create_directory.call_args.args[0]
        sandbox.list_files.assert_called_once_with(created_dir, recursive=False, pattern="frame_*.jpg")
        sandbox.delete_file.assert_called_once_with(created_dir)
        assert [item["image_url"] for item in result] == [
            f"data:image/jpeg;base64,{base64.b64encode(b'one').decode('utf-8')}",
            f"data:image/jpeg;base64,{base64.b64encode(b'two').decode('utf-8')}",
        ]

    def test_image_resize_missing_ffmpeg_falls_back_to_original_image(self) -> None:
        """RFC-0020: ffmpeg 缺失时图片缩放降级为读取原图。"""
        original = b"fake-image-bytes"
        sandbox = Mock()
        sandbox.get_temp_dir.return_value = "/tmp"
        sandbox.join_path.side_effect = lambda base, child: f"{base.rstrip('/')}/{child}"
        sandbox.to_shell_path.side_effect = lambda path: str(path)
        sandbox.read_file.return_value = FileOperationResult(
            status=SandboxStatus.SUCCESS,
            file_path="/images/source.png",
            content=original,
            size=len(original),
        )
        sandbox.execute_shell.return_value = CommandResult(
            status=SandboxStatus.ERROR,
            stderr="ffmpeg: command not found",
            exit_code=127,
        )

        result = _read_image_file("/images/source.png", sandbox, image_max_size=320)

        assert result["image_url"] == f"data:image/png;base64,{base64.b64encode(original).decode('utf-8')}"
        assert result["detail"] == "auto"


class TestReadVisualFileSvgConversion:
    def test_missing_inkscape_detection_handles_non_inkscape_errors(self) -> None:
        """Only missing-Inkscape diagnostics should use the install hint path."""
        result = CommandResult(
            status=SandboxStatus.ERROR,
            stderr="permission denied while exporting",
            exit_code=1,
        )

        assert _is_missing_inkscape(result) is False

    def test_missing_inkscape_detection_handles_windows_message(self) -> None:
        """Windows command-not-recognized output should be treated as missing Inkscape."""
        result = CommandResult(
            status=SandboxStatus.ERROR,
            stderr="'inkscape' is not recognized as an internal or external command",
            exit_code=1,
        )

        assert _is_missing_inkscape(result) is True

    def test_svg_is_converted_to_png_with_inkscape(self) -> None:
        """SVG files are rasterized to PNG before returning an image block."""
        png_bytes = b"converted-png-bytes"
        sandbox = Mock()
        sandbox.get_temp_dir.return_value = "/tmp"
        sandbox.join_path.side_effect = lambda base, child: f"{base.rstrip('/')}/{child}"
        sandbox.to_shell_path.side_effect = lambda path: str(path)
        sandbox.execute_shell.return_value = CommandResult(
            status=SandboxStatus.SUCCESS,
            stdout="",
            stderr="",
            exit_code=0,
        )
        sandbox.read_file.return_value = FileOperationResult(
            status=SandboxStatus.SUCCESS,
            file_path="/tmp/converted.png",
            content=png_bytes,
            size=len(png_bytes),
        )

        result = _read_image_file("/images/icon.svg", sandbox)

        assert result["image_url"] == f"data:image/png;base64,{base64.b64encode(png_bytes).decode('utf-8')}"
        assert result["detail"] == "auto"
        cmd = sandbox.execute_shell.call_args.args[0]
        assert cmd.startswith("inkscape ")
        assert "--export-type=png" in cmd
        assert "--export-filename=" in cmd
        sandbox.delete_file.assert_called_once()

    def test_svg_missing_inkscape_returns_actionable_tool_error(self) -> None:
        """When Inkscape is unavailable, SVG reads return a clear hint instead of raw SVG."""
        sandbox = Mock()
        sandbox.work_dir = "/work"
        sandbox.file_exists.return_value = True
        info = Mock()
        info.is_directory = False
        info.size = 256
        sandbox.get_file_info.return_value = info
        sandbox.get_temp_dir.return_value = "/tmp"
        sandbox.join_path.side_effect = lambda base, child: f"{base.rstrip('/')}/{child}"
        sandbox.to_shell_path.side_effect = lambda path: str(path)
        sandbox.execute_shell.return_value = CommandResult(
            status=SandboxStatus.ERROR,
            stderr="inkscape: command not found",
            exit_code=127,
        )
        agent_state = Mock()
        agent_state.get_sandbox.return_value = sandbox

        result = read_visual_file("diagram.svg", agent_state=agent_state)

        assert result["error"]["type"] == "SVG_REQUIRES_INKSCAPE"
        assert "SVG files cannot be read directly" in result["content"]
        assert "Install Inkscape" in result["content"]
        sandbox.read_file.assert_not_called()
        sandbox.delete_file.assert_called_once()

    def test_svg_conversion_failure_reports_inkscape_diagnostics(self) -> None:
        """Non-missing Inkscape failures should preserve conversion diagnostics."""
        sandbox = Mock()
        sandbox.get_temp_dir.return_value = "/tmp"
        sandbox.join_path.side_effect = lambda base, child: f"{base.rstrip('/')}/{child}"
        sandbox.to_shell_path.side_effect = lambda path: str(path)
        sandbox.execute_shell.return_value = CommandResult(
            status=SandboxStatus.ERROR,
            stderr="inkscape export failed: invalid svg",
            exit_code=2,
        )

        with pytest.raises(RuntimeError, match="Inkscape SVG conversion failed"):
            _convert_svg_to_png_in_sandbox("/images/broken.svg", sandbox)

        sandbox.read_file.assert_not_called()
        sandbox.delete_file.assert_called_once()

    def test_svg_conversion_readback_failure_reports_error(self) -> None:
        """Successful Inkscape execution still fails if the PNG cannot be read back."""
        sandbox = Mock()
        sandbox.get_temp_dir.return_value = "/tmp"
        sandbox.join_path.side_effect = lambda base, child: f"{base.rstrip('/')}/{child}"
        sandbox.to_shell_path.side_effect = lambda path: str(path)
        sandbox.execute_shell.return_value = CommandResult(
            status=SandboxStatus.SUCCESS,
            stdout="",
            stderr="",
            exit_code=0,
        )
        sandbox.read_file.return_value = FileOperationResult(
            status=SandboxStatus.ERROR,
            file_path="/tmp/converted.png",
            error="converted file missing",
        )

        with pytest.raises(RuntimeError, match="converted file missing"):
            _convert_svg_to_png_in_sandbox("/images/icon.svg", sandbox)

        sandbox.delete_file.assert_called_once()

    def test_svg_conversion_accepts_text_png_readback(self) -> None:
        """String readback content is encoded defensively like other image reads."""
        sandbox = Mock()
        sandbox.get_temp_dir.return_value = "/tmp"
        sandbox.join_path.side_effect = lambda base, child: f"{base.rstrip('/')}/{child}"
        sandbox.to_shell_path.side_effect = lambda path: str(path)
        sandbox.execute_shell.return_value = CommandResult(
            status=SandboxStatus.SUCCESS,
            stdout="",
            stderr="",
            exit_code=0,
        )
        sandbox.read_file.return_value = FileOperationResult(
            status=SandboxStatus.SUCCESS,
            file_path="/tmp/converted.png",
            content="png-text",
            size=8,
        )

        assert _convert_svg_to_png_in_sandbox("/images/icon.svg", sandbox) == b"png-text"
        sandbox.delete_file.assert_called_once()
