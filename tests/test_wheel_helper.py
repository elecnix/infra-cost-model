"""The wheel-build helper picks a tool that the environment has (#279).

A virtual environment made by `uv venv` has no `pip`. The wheel tests build
with `pip` when it's importable, else with `uv`, else they skip.
"""

import sys
from pathlib import Path

import pytest

import _wheel

SRC = Path("/src")
OUT = Path("/out")


def _tools(monkeypatch, *, modules, uv):
    monkeypatch.setattr(_wheel, "_importable", lambda name: name in modules)
    monkeypatch.setattr(_wheel.shutil, "which", lambda name: uv if name == "uv" else None)


def test_uses_pip_when_pip_is_importable(monkeypatch):
    _tools(monkeypatch, modules={"pip"}, uv="/bin/uv")
    cmd = _wheel.wheel_build_command(SRC, OUT)
    assert cmd[:4] == [sys.executable, "-m", "pip", "wheel"]
    assert "--no-build-isolation" not in cmd


def test_pip_reuses_setuptools_when_present(monkeypatch):
    _tools(monkeypatch, modules={"pip", "setuptools"}, uv=None)
    cmd = _wheel.wheel_build_command(SRC, OUT)
    assert cmd[:4] == [sys.executable, "-m", "pip", "wheel"]
    assert "--no-build-isolation" in cmd


def test_uses_uv_when_pip_is_missing(monkeypatch):
    _tools(monkeypatch, modules=set(), uv="/bin/uv")
    cmd = _wheel.wheel_build_command(SRC, OUT)
    assert cmd[:3] == ["/bin/uv", "build", "--wheel"]
    assert str(SRC) in cmd
    assert cmd[cmd.index("--out-dir") + 1] == str(OUT)


def test_skips_when_no_build_tool_is_present(monkeypatch):
    _tools(monkeypatch, modules=set(), uv=None)
    with pytest.raises(pytest.skip.Exception, match="pip and uv are both missing"):
        _wheel.wheel_build_command(SRC, OUT)
