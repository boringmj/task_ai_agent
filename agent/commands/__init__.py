"""终端指令:用户在输入框敲的 `/xxx`。

和 tools 的区别:工具是**给模型调用**的、需要 JSON Schema;指令是**给用户敲**的,
由终端程序自己处理,根本不会传给模型。但模型有必要知道它们存在 —— 否则用户问
「怎么清一下上下文」时,它没法建议 `/compact`。所以系统提示词里那段说明**也由这里
自动生成**:新增指令只要写个 `@command`,提示词不用再手动改一遍(以前是要改两处,
漏一处就会出现"提示词里有、实际没有"的假指令)。

与 tools 一致,本包下的模块会被自动发现导入,所以把相关指令归到一个文件里即可。
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path

_COMMANDS: dict[str, tuple] = {}      # 主名 -> (函数, 描述)
_ALIASES: dict[str, str] = {}         # 别名 -> 主名


@dataclass
class Context:
    """指令执行时能拿到的东西。

    以后要让指令访问更多状态(比如 VM 状态、token 用量),往这里加字段即可,
    不必改所有指令的签名。
    """

    messages: list[dict]


def command(name: str, description: str, aliases: tuple[str, ...] = ()):
    """把一个函数登记成终端指令。

    name 要以 / 开头;description 会进系统提示词,写清"什么时候建议用户用它";
    aliases 是等价的名字(如 /new 作为 /reset 的旧名保留)。
    """
    def decorate(fn):
        if not name.startswith("/"):
            raise ValueError(f"指令名要以 / 开头,收到 {name!r}")
        if name in _COMMANDS:
            raise RuntimeError(f"指令重复:{name}")
        _COMMANDS[name] = (fn, description)
        for alias in aliases:
            if alias in _ALIASES or alias in _COMMANDS:
                raise RuntimeError(f"指令别名重复:{alias}")
            _ALIASES[alias] = name
        return fn
    return decorate


def _discover() -> None:
    """导入本包下所有指令模块,触发它们的 @command 注册。"""
    pkg = __name__                      # "agent.commands"
    for path in sorted(Path(__file__).parent.glob("*.py")):
        if path.stem.startswith("_") or path.stem == "__init__":
            continue
        importlib.import_module(f"{pkg}.{path.stem}")


def all_commands() -> list[tuple[str, str, str]]:
    """(主名, 描述, 别名串) 列表,供 /help 与提示词生成使用。"""
    out = []
    for name, (_, desc) in _COMMANDS.items():
        aliases = "、".join(a for a, target in _ALIASES.items() if target == name)
        out.append((name, desc, aliases))
    return out


def dispatch(text: str, ctx: Context) -> str | None:
    """尝试把一行输入当指令执行。

    返回要打印的文本;返回 None 表示"这不是指令",交给模型当普通消息处理。
    长得像指令却没注册的(单个 /开头 的词)会明确报错,免得用户以为是自己敲错了。
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    name = stripped.split(maxsplit=1)[0]
    key = _ALIASES.get(name, name)
    entry = _COMMANDS.get(key)
    if entry is None:
        if " " in stripped:             # 带空格的多半是消息(如以 / 开头的路径),放行
            return None
        known = "、".join(n for n, _, _ in all_commands())
        return f"没有名为 {name} 的指令。可用:{known}"
    fn, _ = entry
    return fn(ctx)


def prompt_section() -> str:
    """把已注册的指令渲染成系统提示词里的一段(由 load_system_prompt 注入)。"""
    lines = [
        "用户可以直接在输入框敲下面这些指令。它们由终端程序自己处理、**不会传给你**,"
        "所以你看不到它们,如果用户有相关需求,可以主动建议用户使用:",
        "",
    ]
    for name, desc, aliases in all_commands():
        suffix = f"(也可写成 {'、'.join(aliases.split('、'))})" if aliases else ""
        lines.append(f"- `{name}`{suffix} —— {desc}")
    lines += [
        "",
        "除了这些,只有 `exit`/`quit` 退出。**不要编造其它指令**;用户问起时如实说明它们的作用。",
    ]
    return "\n".join(lines)


# 放在最后:指令模块要 from . import command / all_commands,
# 必须等它们都定义好之后再触发发现,否则会撞上"模块只加载了一半"的循环导入。
_discover()
