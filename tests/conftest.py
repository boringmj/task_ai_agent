"""pytest 的公共装置。

`helpers` 必须**先**导入:它一被导入就把 AGENT_PROJECT_DIR / AGENT_SESSIONS_DIR 指到临时
目录了,而 core / session 是在**导入时**算这些路径的 —— 顺序反了就全指到真仓库上。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))   # 让 `import helpers` 成立

import helpers                                              # noqa: E402  (先建临时环境)
from helpers import FakeModel, cleanup_tasks                 # noqa: E402
from agent import loop                                       # noqa: E402


@pytest.fixture
def model(monkeypatch):
    """假模型:脚本驱动,不花钱。"""
    m = FakeModel()
    monkeypatch.setattr(loop, "stream_model", m)
    return m


@pytest.fixture
def slow(monkeypatch):
    """把工具执行放慢的开关(见 helpers.slow_tools)。默认不慢,要用时设 delay。"""
    from helpers import slow_tools
    state = slow_tools(monkeypatch, 0.0)
    return state


def _wipe_workspace() -> None:
    """清空临时工作区。

    **必须清** —— 所有测试共用同一个临时工作区,而"没给权限就写不进去"这类断言,
    上一个测试写下的同名文件会让它**假失败**(文件在,但那不是这个测试写的)。
    实测就是这么翻的:反过来也会让"写进去了"假通过 —— 比假失败更糟。
    """
    import shutil
    for child in helpers.WS.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def _isolate():
    """每个测试前后都把任务表和临时工作区清干净,免得互相串。"""
    cleanup_tasks()
    _wipe_workspace()
    yield
    cleanup_tasks()
    _wipe_workspace()


@pytest.fixture
def workspace():
    """临时工作区(真仓库的 workspace 一点不碰)。"""
    return helpers.WS
