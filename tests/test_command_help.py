"""终端指令的帮助文本。

**为什么要钉住它**:指令光有一句描述不够用 —— `/subtasks` 有一堆子命令(`t1 enter` /
`off` / `kill`),一句话塞不下,而用户**猜不出来的时候不会来问,他不用了**。
所以 `usage` 是**必填**的(缺了在启动时就报错),这一组测试保证它一直有人写。
"""
from __future__ import annotations

import io

import pytest
from agent import cli
from agent.commands import Context, all_commands, dispatch, lookup


def _ctx(args: str = "") -> Context:
    c = Context(messages=[])
    c.args = args
    return c


def test_every_command_registers_a_usage():
    """每条指令都得有详细用法 —— 这是 @command 的硬要求,这里再兜一道。"""
    for name, _desc, _hint, _aliases, usage in all_commands():
        assert usage.strip(), f"{name} 没写 usage"
    assert len(all_commands()) > 5, "指令列表不该是空的"


def test_help_without_args_lists_everything():
    out = dispatch("/help", _ctx())
    for name, *_ in all_commands():
        assert name in out, f"/help 里没有 {name}"
    assert "/help <指令>" in out, "得告诉用户还能看详情"


def test_help_with_a_name_shows_usage():
    out = dispatch("/help subtasks", _ctx())
    assert "/subtasks" in out
    for want in ["enter", "off", "kill", "log"]:      # 子命令都得列出来
        assert want in out, f"/help subtasks 里没有 {want}"
    assert "用法" in out


@pytest.mark.parametrize("written", ["switch", "/switch", "SWITCH"])
def test_help_accepts_slashes_aliases_and_case(written):
    """用户是从清单里抄名字过来的 —— 抄成什么样都得能用,不然就是"看着有、敲了说没有"。"""
    assert dispatch(f"/help {written}", _ctx()) == dispatch("/help /switch", _ctx())


def test_help_finds_a_command_by_its_alias():
    out = dispatch("/help quit", _ctx())
    assert "/exit" in out, "别名该指回主名"
    assert "别名" in out


def test_unknown_help_lists_the_names():
    out = dispatch("/help nosuch", _ctx())
    assert "没有名为 nosuch 的指令" in out
    for name, *_ in all_commands():
        assert name in out


def test_a_bad_subcommand_shows_the_usage():
    """/subtasks t1 乱敲 的时候得把用法打出来,而不是就一句"不认识"。"""
    out = dispatch("/subtasks t1 bogus", _ctx())
    assert "不认识的子命令" in out
    assert "/subtasks" in out and "enter" in out


def test_command_output_survives_brackets(monkeypatch):
    """指令输出里的方括号是**普通文字**,不能被 rich 当样式标记吃掉或弄崩。

    踩过两次:`[running]` 被静默吃掉(列表里那个状态就这么没了,不报错),
    `[/subtasks ...]` 直接抛 MarkupError。所以 cli 那层打印指令结果是 markup=False。
    """
    from rich.console import Console
    out = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=out, width=110))
    cli.console.print("- t1 [running],输出 32 tokens —— 看看这个", markup=False)
    assert "[running]" in out.getvalue()


def test_lookup_normalizes_the_name():
    assert lookup("subtasks")[0] == "/subtasks"
    assert lookup("/subtasks")[0] == "/subtasks"
    assert lookup("  /subtasks  ")[0] == "/subtasks"
    assert lookup("nope") is None


def test_an_unknown_command_lists_the_real_ones():
    """敲了个不存在的指令:得说清"没这个"并**顺便把真有的列出来** ——
    否则用户只知道敲错了、不知道该敲什么。"""
    out = dispatch("/nosuch", _ctx())
    assert "没有名为 /nosuch 的指令" in out
    for name, *_ in all_commands():
        assert name in out


def test_a_slash_that_is_really_a_path_is_left_to_the_model():
    """`/reports/x.md 看一下` 是一句话,不是指令 —— 得放行给模型。

    拦下来的话,用户就没法在对话里提一个以斜杠开头的路径了(而那是很自然的说法)。
    """
    assert dispatch("/reports/x.md 看一下这个文件", _ctx()) is None


@pytest.mark.parametrize("written", ["/HELP", "/Help", "  /help  "])
def test_the_command_name_is_case_and_space_insensitive(written):
    """用户是从清单里抄的名字 —— 大小写不该成为"看着有、敲了说没有"的原因。"""
    assert "没有名为" not in dispatch(written, _ctx())


def test_the_args_are_what_was_actually_typed():
    """参数是按**敲进去的原样**切出来的,且两头空格不算。"""
    c = _ctx()
    assert dispatch("/help    subtasks   ", c) is not None
    assert c.args == "subtasks"
    c2 = _ctx()
    # 用不存在的任务号:只验参数怎么切,不动任何真状态
    dispatch("/subtasks   404   log", c2)
    assert c2.args.split() == ["404", "log"]


def test_the_prompt_list_mentions_every_command():
    """给模型看的那份清单是**自动生成**的 —— 漏一条就会出现"提示词里有、实际没有"
    的假指令(以前要改两处,漏一处就是这样)。"""
    from agent.commands import system_message
    text = system_message()
    for name, *_ in all_commands():
        assert name in text, f"提示词里没有 {name}"
