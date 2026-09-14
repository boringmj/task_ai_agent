"""每个 agent 自己的状态(主 agent 一份,每个子 agent 各一份)。

**为什么需要它**:下面这些东西原来是**进程级单份**的 —— 只有一个 agent 在跑时看不出问题,
子 agent 一并发就会互相踩:

    · 待注入的图片     子 agent 截的图会注进**主 agent** 的下一轮请求
    · 本轮搜索配额     子 agent 的搜索记在**主 agent** 头上
    · 上下文占用 / 用量 分不清谁花的,也判断不出该压**谁**的上下文
    · 输出前缀         终端上分不清哪一行是谁打的

**为什么用 ContextVar 而不是"多传一个参数"**:ContextVar 天然按**执行流**隔离 —— 主 agent
在它自己的上下文里,每个子 agent 线程在各自己的上下文里,互相看不见。而工具函数
(`write_file`、`web_search`、`img` …)**一个签名都不用改**,`current()` 随时知道"现在是谁
在干活"。要是改成传参,几十个工具都得跟着改,还容易漏一个就串味。

**有个坑要记住**:新开的线程**不继承**父线程的上下文(这是 ContextVar 的规矩,不是 bug)——
所以子 agent 线程**必须自己 `use(ctx)`**。忘了的话它会落到下面那个默认上下文上,写入的东西
就此消失(注意:默认上下文**故意不是**主 agent 那一份,就是为了让"忘了设置"表现为"数据没了"
而不是"污染了主 agent" —— 前者一眼看得出来,后者要很久以后才发现)。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


def _empty_usage() -> dict:
    return {"requests": 0, "completion": 0, "prompt": 0, "cache_hit": 0}


@dataclass
class AgentCtx:
    """一个 agent 的运行时状态。

    `role` / `task_id` / `label` 是身份:谁在干活。后面接权限(能读写哪些文件、能不能用 VM、
    能加载哪些技能)也挂在这儿 —— 权限跟着 agent 走,不跟着进程走。
    """

    role: str = "main"          # "main" / "sub"
    task_id: str = ""           # 子 agent 的任务 id;主 agent 为空
    label: str = ""             # 输出前缀(如 "a3");主 agent 为空 —— 不加前缀

    # ---- 授权(跟着 agent 走,不跟着进程走)----
    # VM 是**一份**、主和子共享,子 agent 没有自己的机器 —— 所以由主 agent 在派活时
    # 决定这次给不给。默认**不给**:要用的任务才给,不是默认人人有份。
    vm_grant: bool = False

    # ---- 轮次级状态(原来散在各模块的全局变量里)----
    # 待注入的图片 data URL,img 工具跑过就填,下一轮请求前注入进对话
    pending_images: list = field(default_factory=list)
    # 本轮已用掉多少次搜索(配额,每轮开头清零)
    searches_this_turn: int = 0
    # 最近一次请求的用量:prompt_tokens 就是"这个 agent 现在的上下文有多大"
    last_usage: dict = field(default_factory=dict)
    # 本轮起点快照(见 llm.begin_turn)
    turn_start: dict = field(default_factory=dict)
    # 本会话累计
    total_usage: dict = field(default_factory=_empty_usage)

    # 输出出口(rich Console)。None = 用真正那个终端。
    # 子 agent 的默认值是一块**自己的缓冲区**(见 tasks.py)—— 它说的话不该打到主终端:
    # 主终端是用户跟**主 agent** 对话的地方,子 agent 往那儿刷几百行,用户就看不见
    # 主 agent 在说什么了。
    console: object = None
    # 挂起时的交接内容(见 tasks.py)。工具把它填上,循环看到就停下来。
    suspend: dict = field(default_factory=dict)

    @property
    def is_sub(self) -> bool:
        return self.role == "sub"

    def prefix(self) -> str:
        """终端输出的前缀(主 agent 不加前缀,免得每条都多两个字符)。"""
        return f"[{self.label}] " if self.label else ""


def out():
    """当前 agent 该往哪儿输出。

    工具和循环里一律用 `ctx.out().print(...)`,不要直接 import 那个全局 console ——
    否则子 agent 的思考、工具调用、中间说明会全部打到主终端上。
    """
    c = _current.get()
    if c.console is not None:
        return c.console
    from .core import console           # 延迟导入:ctx 要当叶子模块,别拖进 core 那一串
    return console


# 没人在场时的兜底上下文。**刻意不是主 agent 那一份**:测试、独立脚本直接调工具时用它,
# 免得要处处先建上下文;而子 agent 忘了 use() 时也会落到这儿 —— 那时数据写进一个没人读的
# 地方,一眼就能看出"没生效",而不是悄悄改了主 agent 的状态。
_DEFAULT = AgentCtx(role="main")
_current: ContextVar[AgentCtx] = ContextVar("agent_ctx", default=_DEFAULT)


def current() -> AgentCtx:
    """现在是谁在干活。工具函数里随时可以调,不需要别人把上下文传进来。"""
    return _current.get()


@contextmanager
def use(ctx: AgentCtx):
    """把当前执行流切到这个上下文,退出时还原。

    子 agent 线程要在**线程内部**用它(线程不继承上下文,见模块开头)。
    """
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)
