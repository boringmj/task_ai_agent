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
    # miss 是**没吃到缓存的那部分输入**。它和 hit 一起才看得出"这次请求有多少是全价":
    # prompt = cache_hit + cache_miss,只看 hit 的百分比看不出分母有多大。
    return {"requests": 0, "completion": 0, "prompt": 0, "cache_hit": 0, "cache_miss": 0}


FS_ANY = "*"        # 通配:整个工作区


@dataclass
class FsGrant:
    """这个 agent 能读写**哪儿**。路径相对工作区,`*` 表示整个工作区。

    **为什么要有范围,而不是简单的"能写/不能写"**:几个子 agent 并排跑的时候,
    它们改的是同一批文件。给整片工作区的写权限,等于让它们互相踩 —— 而且踩了**不报错**,
    只是结果对不上,事后根本查不出是谁改的。把范围划开,冲突就变成**当场可判**的事。
    """

    read: tuple = (FS_ANY,)
    write: tuple = ()
    # 删除/移动要**单独**开:写坏一个文件还能改回来,删掉就没了。
    # 这两件事不该共用一个"写"权限。
    delete: bool = False
    # **临时区**:一块永远可写、也随便删的草稿纸(见 core.scratch_scope)。
    # 技能脚本要产出中间文件(扫描的原始输出、抽出来的样本),而它的写范围常常只有
    # "报告目录"那么窄 —— 没有指定的地方,它就会往唯一能写的地方倒(实测:一份
    # 28KB 的 raw.json 倒进了 .pylibs,和装好的包混在一起)。
    #
    # **为什么不并进 write,而是单开一个字段**:
    #   · 并进 write 会让它参与"两个 agent 是不是撞车"的判定 —— 而临时区是**按
    #     "会话 + 谁"分开**的(.tmp/<会话>/<谁>/,见 core.scratch_scope),本来就不该撞;
    #   · 并进 write 还会顺带给出**删除**权(删是跟着 write 范围走的),那等于为了给
    #     一块草稿纸,把它交付目录里的东西也变成可删的。
    scratch: tuple = ()

    @classmethod
    def full(cls) -> "FsGrant":
        """不限制,连临时区都没指定。给"没人在场的独立调用"和测试用。"""
        return cls(read=(FS_ANY,), write=(FS_ANY,), delete=True)

    @classmethod
    def for_main(cls, scratch: str) -> "FsGrant":
        """主 agent 的:`full()` 再加一块**指定的**临时区(路径由调用方算,见 core.scratch_scope)。

        它本来就哪儿都能写,所以这不是"权限"问题 —— 是**没个指定的地方,临时文件
        就全凭当时心情放**(实测:一份 28KB 的中间产物落在了 .pylibs 里)。

        路径**不带默认值**:它跟着当前会话走,而"当前会话"是会变的(/switch)——
        给个默认值就等于把"哪会儿算出来的"藏起来,那种 bug 最难查。
        """
        return cls(read=(FS_ANY,), write=(FS_ANY,), delete=True, scratch=(scratch,))

    @staticmethod
    def _within(rel: str, scopes: tuple) -> bool:
        """在不在这些范围里。

        **范围是不是目录,由末尾那个 `/` 说了算,不看文件系统:**

        - `reports/`(带斜杠)→ 目录,它下面的一切都算
        - `notes/plan.md`(不带)→ 文件,**只算它自己**

        为什么不让"它现在是什么"来决定:那样同一个范围会随磁盘状态变意思 —— 文件还没
        建出来的时候判一次、建出来之后再判一次,结果不一样。而且**判成目录是放宽、
        判成文件是收紧**,凭猜的话猜错方向就是多给了权限。写清楚是唯一安全的路。
        """
        for s in scopes:
            if s == FS_ANY:
                return True
            if s.endswith("/"):               # 目录:整个子树
                base = s.rstrip("/")
                if rel == base or rel.startswith(base + "/"):
                    return True
            elif rel == s:                    # 文件:只有它自己
                return True
        return False

    def allows(self, rel: str, mode: str) -> bool:
        # 临时区**三样都算数**(读、写、删)。它是草稿纸:自己那一块里随便折腾,
        # 出界了就照旧按上面的规矩判 —— 那才是"范围"要守的地方。
        # 删也放开的理由:临时产物本来就该边跑边清,不给删的话它会越堆越多,
        # 而"删"在这里的作用域只有它自己那一块,**不碰任何交付物**。
        if self._within(rel, self.scratch):
            return True
        if mode == "read":
            return self._within(rel, self.read)
        if mode == "write":
            return self._within(rel, self.write)
        if mode == "delete":
            # 删/移既要有 delete 开关,也得落在写范围里 —— 开关是"允许这类操作",
            # 范围是"允许在哪",两件事都成立才行。
            return self.delete and self._within(rel, self.write)
        return False

    @staticmethod
    def covers(a: str, b: str) -> bool:
        """`a` 这个范围包不包得住 `b` 这个范围(或者那份文件)。给重叠检测用。"""
        if a == FS_ANY:
            return True
        if a.endswith("/"):
            base = a.rstrip("/")
            b2 = b.rstrip("/") if b.endswith("/") else b
            return b2 == base or b2.startswith(base + "/")
        return a == b

    def explain(self, rel: str, mode: str) -> str:
        what = {"read": "读", "write": "写", "delete": "删除或移动"}.get(mode, mode)
        if mode == "delete" and not self.delete:
            why = "这次的活没有给你删除/移动的权限(它和写权限是分开的)"
        elif not self.write and not self.read:
            why = f"这次的活没有给你{what}任何文件"
        else:
            scope = "、".join(self.write or self.read) or "(空)"
            why = f"你被允许的范围是:{scope},`{rel}` 不在里面"
        # **先给出临时区** —— 被拒时它正急着找个地方写,而"能写的地方"这句话里要是
        # 没有草稿纸,它就会去别处找(实测:倒进了 .pylibs)。给了路,它就不用绕。
        tip = f"纯中间产物写 {self.scratch[0]} 就行(那是给你的草稿纸,随便用)。" \
            if self.scratch else ""
        return f"拒绝{what} `{rel}`:{why}。" + tip + (
            "如果确实需要,**用 suspend 向主 agent 申请**,别绕。"
            if self.write != (FS_ANY,) else "")


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
    # 能读写哪儿。默认**不限制** —— 这一条是给"主 agent 和独立调用"用的兼容默认值。
    # 子 agent **必须**由 tasks.py 显式给一份受限的,那里是唯一的入口。
    # 主 agent 的临时区由 cli 显式设上(FsGrant.for_main(路径))—— 它跟着**当前会话**
    # 走,而"当前会话"在这儿还解析不出来(那要 import session,而 ctx 是叶子模块)。
    fs: FsGrant = field(default_factory=FsGrant.full)

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
    # 半路插给这个 agent 的话(用户接管子 agent 时敲的,见 tasks.tell)。
    # **排队而不是直接写进对话** —— 写的人(另一个线程)不知道它现在停在哪一步:
    # 它可能正好卡在"assistant(带 tool_calls) 写了、tool 结果还没写"的中间态,
    # 这时插一条 user 进去,下一次请求就是非法的(API 直接 400),而它只会表现为
    # "莫名失败"。所以由**它自己**在每一步的开头(安全点)取走 —— 见 loop._take_pending。
    pending_input: list = field(default_factory=list)
    # 叫停旗子(见 tasks.kill)。线程没法从外面强杀,所以只能每步之间 check 一次 ——
    # 它**不打断正在跑的那一次工具调用**,但不会再多走一步。
    cancelled: bool = False
    # 撞上步数上限被强制中止了(见 loop.run)。**和"说完了"是两回事**:它交回来的
    # 东西是半截的,调用方必须区别对待 —— 否则状态行写着"完成"、正文写着"强制中止",
    # 而看的人只会记住状态行。实测就是这么翻车的。
    truncated: bool = False

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
