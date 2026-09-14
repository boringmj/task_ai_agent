"""终端指令:用户在输入框敲的 `/xxx`。

和 tools 的区别:工具是**给模型调用**的、需要 JSON Schema;指令是**给用户敲**的,
由终端程序自己处理,根本不会传给模型。但模型有必要知道它们存在 —— 否则用户问
「怎么清一下上下文」时,它没法建议 `/compact`。所以系统提示词里那段说明**也由这里
自动生成**:新增指令只要写个 `@command`,提示词不用再手动改一遍(以前是要改两处,
漏一处就会出现"提示词里有、实际没有"的假指令)。

一条指令要写**三份文字**,各给不同的人看:`description` 和 `usage` 给用户(`/help` 一句话/
详细用法),`hint` 给模型(什么场合该建议用户用它)。三者必须有,**usage 尤其不能省** ——
有子命令的指令(如 `/subtasks t1 enter`)光靠一句描述说不清,而用户猜不出来的时候
不会来问,他不用了。

与 tools 一致,本包下的模块会被自动发现导入,所以把相关指令归到一个文件里即可。
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path

from .. import prompts

_COMMANDS: dict[str, tuple] = {}      # 主名 -> (函数, 描述, 给模型的时机, 用法)
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


def command(name: str, description: str, *, usage: str,
            hint: str = "", aliases: tuple[str, ...] = ()):
    """把一个函数登记成终端指令。

    name        以 / 开头
    description **给用户看的** —— `/help` 里逐条列出。一句话说清"这个指令做什么"就够,
                别带 markdown 标记:终端是按纯文本打印的,星号反引号会原样露出来。
    usage       **给用户看的详细用法** —— `/help <指令>` 打出来的那段。每行一个写法,
                左边写怎么敲(带参数的就写清参数长什么样)、右边写它会干什么。
                **没有默认值,必须写。** 指令光有一句描述不够用:`/subtasks` 有一堆子命令
                (`t1 enter` / `off` / `kill`),描述里塞不下,不写用法用户就只能猜 —— 而
                猜不出来的时候他不会来问,他不用了。给默认值的话,新加的指令会默认"没有用法"
                并且**不报错**,这正是最该拦住的地方(和 safe_path 的 mode 同一个道理)。
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
        if not (usage or "").strip():
            raise RuntimeError(f"指令 {name} 没写 usage —— 用法是必填的,见本函数说明")
        _COMMANDS[name] = (fn, description, hint, usage.strip())
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


def all_commands() -> list[tuple[str, str, str, str, str]]:
    """(主名, 给用户看的说明, 给模型看的时机, 别名串, 详细用法)。

    /help 与提示词各取所需:提示词只要 description + hint(用法那份太长,进上下文
    是白花钱),/help 三份都要。所以五个一起返回,谁用谁取。
    """
    out = []
    for name, (_, desc, hint, usage) in _COMMANDS.items():
        aliases = "、".join(a for a, target in _ALIASES.items() if target == name)
        out.append((name, desc, hint, aliases, usage))
    return out


def _key(name: str) -> str:
    """把敲进来的指令名归一:`/Switch`、`/SWITCH` 都当 `/switch`。

    指令名全是 ASCII,大小写不该成为"看着有、敲了说没有"的原因 —— 和任务号既认
    `t2` 又认 `2` 是同一类事(见 tasks._norm)。这种失败最气人:用户明明照着清单敲的。
    """
    return (name or "").strip().lower()


def lookup(name: str) -> tuple[str, str, str, str, str] | None:
    """按名字查一条指令,返回**和 all_commands() 同形状**的一项。

    别名也认(`/help quit` 会找到 `/exit`)、前导 `/` 可有可无 —— 用户是从 `/help` 的
    清单里抄名字过来的,抄哪个都得能用,不然就成了"看着有、敲了说没有"。

    返回的形状刻意和 all_commands() 一致(主名/说明/时机/别名串/用法):两边都是
    "一条指令的几面",形状不同的话调用处就得记住两套解包顺序,迟早记错。
    """
    key = _key(name)
    if not key:
        return None
    if not key.startswith("/"):
        key = "/" + key
    key = _ALIASES.get(key, key)
    entry = _COMMANDS.get(key)
    if entry is None:
        return None
    aliases = "、".join(a for a, target in _ALIASES.items() if target == key)
    return (key, entry[1], entry[2], aliases, entry[3])


def dispatch(text: str, ctx: Context) -> str | None:
    """尝试把一行输入当指令执行。

    返回要打印的文本;返回 None 表示"这不是指令",交给模型当普通消息处理。
    长得像指令却没注册的(单个 /开头 的词)会明确报错,免得用户以为是自己敲错了。
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    raw = stripped.split(maxsplit=1)[0]          # 用户敲成什么样(error 里要原样回显)
    name = _key(raw)
    key = _ALIASES.get(name, name)
    entry = _COMMANDS.get(key)
    if entry is None:
        if " " in stripped:             # 带空格的多半是消息(如以 / 开头的路径),放行
            return None
        known = "、".join(n for n, *_ in all_commands())
        return f"没有名为 {raw} 的指令。可用:{known}"
    fn = entry[0]
    # 按**原样那个名字**的长度切,不用归一后的 —— 归一里有个 lower(),个别字符
    # (如 'İ')小写化之后长度会变,拿它切会把参数切歪。这里只关心敲了几个字符。
    ctx.args = stripped[len(raw):].strip()       # 指令名后面的部分,交给指令自己解析
    return fn(ctx)


def system_message() -> str:
    """把已注册的指令渲染成**独立的一条 system 消息**。

    刻意不并进主系统提示词:这份清单是**程序自动生成**的,描述文字是**数据,不是指令**。
    单独成一条、并显式写明信任边界,是为了 —— 将来若加载了第三方的指令模块、
    描述里夹带了「忽略之前的指示」这类文本,它不会被当成系统提示词的一部分照做,
    而是被当作一段待核实的参考信息(与对待网页内容的规矩一致)。
    """
    lines = []
    for name, desc, hint, aliases, _usage in all_commands():
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
