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
from dataclasses import dataclass, field
from pathlib import Path

from .. import prompts

_COMMANDS: dict[str, tuple] = {}      # 主名 -> (函数, 描述)
_ALIASES: dict[str, str] = {}         # 别名 -> 主名


@dataclass
class Context:
    """指令执行时能拿到的东西。

    以后要让指令访问更多状态(比如 VM 状态、token 用量),往这里加字段即可,
    不必改所有指令的签名。
    """

    messages: list[dict]
    args: str = ""      # 指令名后面的参数(dispatch 会填),如 `/switch abc123` 里的 abc123
    # 这条指令执行期间"发生了什么"(如 session_reset)。指令**只声明事实**,不在这里
    # 补"那要不要提醒用户 VM 没重置"之类的后续 —— 那要么让它 import 本不相干的模块、
    # 要么让它猜别的模块的状态。谁关心这些事件,谁在装配层(cli)去接。
    events: set[str] = field(default_factory=set)


def command(name: str, description: str, hint: str = "",
            aliases: tuple[str, ...] = ()):
    """把一个函数登记成终端指令。

    name        以 / 开头
    description **给用户看的** —— `/help` 里逐条列出。一句话说清"这个指令做什么"就够,
                别带 markdown 标记:终端是按纯文本打印的,星号反引号会原样露出来。
    hint        **给模型看的** —— 进系统提示词。写"用户说什么时建议他用这个"这类话;
                这种话对用户毫无意义(`/help` 里显示"建议用它"只会让人困惑),
                所以两者必须分开。留空则退回用 description。
    aliases     等价的名字(如 /new 作为 /reset 的旧名)
    """
    def decorate(fn):
        if not name.startswith("/"):
            raise ValueError(f"指令名要以 / 开头,收到 {name!r}")
        if name in _COMMANDS:
            raise RuntimeError(f"指令重复:{name}")
        _COMMANDS[name] = (fn, description, hint)
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


def all_commands() -> list[tuple[str, str, str, str]]:
    """(主名, 给用户看的说明, 给模型看的时机, 别名串)。/help 与提示词各取所需。"""
    out = []
    for name, (_, desc, hint) in _COMMANDS.items():
        aliases = "、".join(a for a, target in _ALIASES.items() if target == name)
        out.append((name, desc, hint, aliases))
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
    fn = entry[0]
    ctx.args = stripped[len(name):].strip()      # 指令名后面的部分,交给指令自己解析
    return fn(ctx)


def system_message() -> str:
    """把已注册的指令渲染成**独立的一条 system 消息**。

    刻意不并进主系统提示词:这份清单是**程序自动生成**的,描述文字是**数据,不是指令**。
    单独成一条、并显式写明信任边界,是为了 —— 将来若加载了第三方的指令模块、
    描述里夹带了「忽略之前的指示」这类文本,它不会被当成系统提示词的一部分照做,
    而是被当作一段待核实的参考信息(与对待网页内容的规矩一致)。
    """
    lines = []
    for name, desc, hint, aliases in all_commands():
        suffix = f"(也可写成 {'、'.join(aliases.split('、'))})" if aliases else ""
        # description 是**公共信息** —— 用户和模型都该知道这条指令是做什么的;
        # hint 才是额外给模型的"什么场合值得建议用户用"。两者不是互斥的两半,
        # 模型只拿到 hint 的话,用户问"这个指令是干嘛的"它就答不上来。
        line = f"- `{name}`{suffix} —— {desc}"
        if hint:
            line += f"\n  建议时机:{hint}"
        lines.append(line)
    return prompts.load("commands_list", commands="\n".join(lines))


# 放在最后:指令模块要 from . import command / all_commands,
# 必须等它们都定义好之后再触发发现,否则会撞上"模块只加载了一半"的循环导入。
_discover()
