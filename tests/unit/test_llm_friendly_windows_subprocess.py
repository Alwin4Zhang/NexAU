import subprocess

from nexau.archs.tool.builtin import llm_friendly
from nexau.archs.tool.builtin.llm_friendly import FileFormatHandler


def test_get_file_line_nums_passes_no_window_kwargs(monkeypatch):
    captured_kwargs: dict[str, object] = {}

    def fake_check_output(_cmd, **kwargs):
        captured_kwargs.update(kwargs)
        return b"12 sample.txt\n"

    monkeypatch.setattr(llm_friendly.subprocess, "check_output", fake_check_output)
    monkeypatch.setattr(llm_friendly, "windows_no_window_creationflags", lambda: 1)

    assert FileFormatHandler._get_file_line_nums("sample.txt") == 12
    assert captured_kwargs["creationflags"] == 1


def test_get_file_line_nums_falls_back_when_wc_missing(tmp_path, monkeypatch):
    path = tmp_path / "sample.txt"
    path.write_text("a\nb\n", encoding="utf-8")
    monkeypatch.setattr(
        llm_friendly.subprocess,
        "check_output",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError),
    )

    assert FileFormatHandler._get_file_line_nums(str(path)) == 2


def test_get_file_size_passes_no_window_kwargs(monkeypatch):
    captured_kwargs: dict[str, object] = {}

    def fake_check_output(_cmd, **kwargs):
        captured_kwargs.update(kwargs)
        return b"42 sample.txt\n"

    monkeypatch.setattr(llm_friendly.subprocess, "check_output", fake_check_output)
    monkeypatch.setattr(llm_friendly, "windows_no_window_creationflags", lambda: 1)

    assert FileFormatHandler._get_file_size("sample.txt") == 42
    assert captured_kwargs["creationflags"] == 1


def test_get_file_size_falls_back_when_du_missing(tmp_path, monkeypatch):
    path = tmp_path / "sample.txt"
    path.write_text("hello", encoding="utf-8")
    monkeypatch.setattr(
        llm_friendly.subprocess,
        "check_output",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.CalledProcessError(1, "du")),
    )

    assert FileFormatHandler._get_file_size(str(path)) == 5
