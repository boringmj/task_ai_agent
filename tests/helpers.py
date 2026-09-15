"""测试的公共装置:临时环境 + 假模型 + 几个断言用的小工具。

**为什么要临时环境**:agent 的沙箱、会话目录、技能目录全挂在 PROJECT_DIR 下。测试要是
跑在真仓库上,子 agent 写的文件会落进真工作区、对话会落进真会话目录 —— 这个坑踩过:
一次单元测试往真会话文件里写进了一条假通报,事后才发现。所以整套测试跑在一个临时目录里,
真仓库一个字节都不动(测试产物一律不进仓库,是仓库外那条规矩的延伸)。

**假模型只替掉 `loop.stream_model` 这一个函数**(也就是那一次 HTTP 请求)。底下
tasks / loop / registry / safe_path / FsGrant 全是生产代码 —— 这样既不花钱,又不至于
测了个假东西。为什么要假模型:下面这些场景真模型根本跑不出来(它得恰好撞上一次拒绝、
再恰好想起来申请权限),真跑要烧掉一整轮对话的钱还不一定撞上。

**import 顺序要紧**:这个模块一被导入就把环境变量设好了,所以 conftest 必须先导入它、
再导入 agent.* —— core / session 是在**导入时**就把 PROJECT_DIR、SESSIONS_DIR 算出来的,
之后再改环境变量已经没用了。
"""
from __future__ import annotations

import itertools
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
_TMP = Path(tempfile.mkdtemp(prefix="agent-tests-"))
PROJ = _TMP / "project"
WS = PROJ / "workspace"

WS.mkdir(parents=True)
shutil.copytree(REPO / "prompts", PROJ / "prompts")
os.environ["AGENT_PROJECT_DIR"] = str(PROJ)
os.environ["AGENT_SESSIONS_DIR"] = str(_TMP / "sessions")
os.environ["DEEPSEEK_API_KEY"] = "sk-fake-tests-never-used"
os.environ["MODEL"] = "fake"
os.environ["SUBAGENT_MAX_STEPS"] = "20"      # 测试里的脚本都很短,步数上限调小更早暴露问题
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agent import tasks                        # noqa: E402
from agent.ctx import FS_ANY, FsGrant          # noqa: E402


# ============================== 假模型 ==============================

class FakeModel:
    """替掉 `loop.stream_model`:脚本说这一步模型该调哪个工具,它就调哪个。

    还额外复刻了真实现的三个**副作用**,漏掉它们测试就会得出假结论:
      · 中间轮次说的话要打到当前 agent 的输出口上(见 llm.stream_model 末尾)——
        不复刻的话,接管测试会"发现"接管看不到子 agent 说话。
      · **记账**(`llm._record_usage`):真实实现每收到一帧都会记用量,所以
        `usage_line` / `_cost_line` / "总"都是活的。不复刻的话,凡是跟用量有关的断言
        都只能测到 0 —— 那不是"没测",是测了个假的。
      · 每步可以睡一会儿(step_delay),用来让子 agent 活得够久,好让接管看得见。
    """

    # 每次请求报出来的"用量"。挑一组好认的数字:命中率恰好 90%,一眼能看出算没算错。
    PROMPT = 1000
    HIT = 900
    COMPLETION = 50

    def __init__(self) -> None:
        self.script: list = []
        self.seen: list = []          # 每次请求模型看到的完整历史(排查用)
        self.step_delay = 0.0
        self._ids = itertools.count(1)

    def call(self, name: str, **args) -> dict:
        """造一个 tool_call(形状和 DeepSeek 流式拼出来的一致)。"""
        return {"id": f"c{next(self._ids)}", "type": "function",
                "function": {"name": name,
                             "arguments": json.dumps(args, ensure_ascii=False)}}

    def set(self, *steps) -> None:
        """排好接下来每一步:(正文, [tool_calls], 思考)。"""
        self.script = list(steps)

    def reply(self, text: str) -> None:
        """排一个"不再调工具、直接说完"的收尾步。"""
        self.set((text, [], ""))

    def __call__(self, messages):
        self.seen.append([dict(m) for m in messages])
        if self.step_delay:
            time.sleep(self.step_delay)
        # 复刻真实现每帧都记账这件事(见类注释)—— 记在**当前 agent**的账上
        from agent import llm
        llm._record_usage(SimpleNamespace(
            prompt_tokens=self.PROMPT, completion_tokens=self.COMPLETION,
            prompt_cache_hit_tokens=self.HIT,
            prompt_cache_miss_tokens=self.PROMPT - self.HIT))
        if not self.script:
            return ("(脚本用光了)", [], "")
        content, calls, reason = self.script.pop(0)
        if calls and content.strip():
            from agent import ctx
            ctx.out().print("AI >", style="bold green", markup=False)
            ctx.out().print(content, markup=False)
        return content, calls, reason


# ============================== 小工具 ==============================

def grant(write=(), read=None, delete=False) -> FsGrant:
    """造一份授权(口径和 tools/tasks.py 里 dispatch_task 造的那份一致)。"""
    return FsGrant(read=tuple(read) if read else (FS_ANY,),
                   write=tuple(write), delete=delete)


def wait_status(tid: str, want, timeout: float = 25.0):
    """等子 agent 落到某个状态。状态是它自己那条线程写的,没有回调,只能轮询。"""
    if isinstance(want, str):
        want = (want,)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        t = tasks.get(tid)
        if t is not None and t.status in want:
            return t
        time.sleep(0.05)
    return tasks.get(tid)


def wait_until(pred, timeout: float = 25.0) -> bool:
    """等某个条件成立(返回它最终成不成立)。

    给"状态是它自己那条线程写的、没有回调"的场合用 —— 比如"它又被叫起来跑了第二轮"
    这种,光看 status 是看不出来的(第一次跑完就是 done,第二轮开始又会短暂回到 running,
    等它跑完还是 done)。所以判据得是"历史里多了一条新消息"之类的东西。
    """
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return bool(pred())


def structure_ok(messages: list[dict]) -> tuple[bool, str]:
    """历史结构合法吗:assistant(带 tool_calls) 后面必须紧跟它那几个 tool 结果。

    **这是"半路插话"的要害** —— 中间被插进来的那条是 user 身份。一旦它落在
    assistant(tool_calls) 和它的 tool 结果之间,下一次请求就是非法的(API 直接 400),
    而子 agent 只会表现为"莫名失败",离真正的原因很远。
    """
    i = 0
    while i < len(messages):
        calls = messages[i].get("tool_calls") if messages[i].get("role") == "assistant" else None
        if calls:
            want = [c.get("id") for c in calls]
            got = []
            j = i + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                got.append(messages[j].get("tool_call_id"))
                j += 1
            if got != want:
                nxt = messages[i + 1].get("role") if i + 1 < len(messages) else "(末尾)"
                return False, (f"第 {i} 条 assistant 要的结果是 {want},"
                               f"紧随其后的却是 {nxt}(tool 结果:{got or '一个都没有'})")
            i = j
            continue
        i += 1
    return True, ""


def slow_tools(monkeypatch, delay: float):
    """把工具执行放慢,并把"已经进到工具里"这个时刻暴露出来。

    **为什么需要它**:真实场景里 `vm_run` 一条命令能跑几十秒、`grep_files` 翻一棵大树也要
    几秒,那期间子 agent 的历史正好停在"assistant 写完、tool 结果还没写"的中间态。
    不把时间窗放大到秒级,就只能靠碰运气才撞得上 —— 而撞不上的测试等于没有。

    返回一个 dict:{"entered": Event, "delay": float}。entered 一置上就说明它正卡在工具里。
    """
    from agent import loop
    real = loop.dispatch
    state = {"entered": threading.Event(), "delay": delay}

    def wrapper(name, arguments):
        state["entered"].set()
        if state["delay"]:
            time.sleep(state["delay"])
        return real(name, arguments)

    monkeypatch.setattr(loop, "dispatch", wrapper)
    return state


def cleanup_tasks() -> None:
    """把测试留下的子 agent 收干净 —— 见 conftest 里那个 autouse fixture。

    **每个测试都要收**:任务表是模块级的,不清的话上一个测试派的子 agent 会留在
    `needs_attention()` / `listing()` 里,让下一个测试看到不属于它的东西 ——
    那种失败排查起来极其费劲(现象是"另一个测试的行为影响了这一个")。
    """
    for t in list(tasks._TASKS.values()):
        t.cancel_status = "closed"
        if t.ctx is not None:
            t.ctx.cancelled = True
    tasks._TASKS.clear()
    tasks.detach()
