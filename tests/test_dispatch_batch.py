"""批量派发 + 缓存预热闸门。

**闸门是干什么的**(实测数据支撑):DeepSeek 的缓存按**前缀**命中,而"落盘"发生在请求
**结束之后**(文档说构建是秒级)。所以同一瞬间发出的几条子 agent,谁都还没替对方铺下
缓存 —— 合成前缀实测:并发三条,每条白付 1,024 tokens;而等第一条跑完再发的那条只付 237。

所以:**先放一条、等它第一次请求落盘,再一起放其余的**。代价是主 agent 那一轮停几秒,
一个进程只发生一次(缓存 TTL 几小时)。

这一组测试全部用假模型 —— 闸门是**时序**逻辑,靠真 API 测既贵又不稳。
"""
from __future__ import annotations

import time

import helpers
import pytest
from agent import ctx as agent_ctx
from agent import tasks
from agent.tools.tasks import dispatch_task


@pytest.fixture
def fast_warm(monkeypatch):
    """把 settle 调到几十毫秒 —— 要验的是"等没等、等多久",不是真等 5 秒。"""
    monkeypatch.setattr(tasks, "SUB_WARM_SETTLE", 0.15)
    monkeypatch.setattr(tasks, "SUB_WARM_TIMEOUT", 5.0)
    monkeypatch.setattr(tasks, "_WARMED", False)
    return 0.15


def _steps(model, n=4):
    model.set(*[("干着。", [model.call("get_current_time")], "")] * n
              + [("干完了。", [], "")])


def test_the_first_wave_waits_for_the_leader_to_land(model, fast_warm):
    """**闸门的全部意义**:后面那几条要等到第一条第**一次请求已经发出并落盘**。

    判据用"谁先发的请求":leader 的第一次 stream 调用,必须早于后面的派发。
    """
    _steps(model)
    started = time.monotonic()
    results, note = tasks.dispatch_batch([
        {"prompt": "第一件", "fs": helpers.grant(), "wait": False},
        {"prompt": "第二件", "fs": helpers.grant(), "wait": False},
        {"prompt": "第三件", "fs": helpers.grant(), "wait": False},
    ])
    elapsed = time.monotonic() - started

    assert [r["task_id"] for r in results] == ["t1", "t2", "t3"]
    assert not note, f"正常等到落盘就不该有告警:{note}"
    assert elapsed >= fast_warm, f"没等就放行了({elapsed:.3f}s)"
    leader = tasks.get("t1")
    assert (leader.ctx.total_usage or {}).get("requests", 0) >= 1, "领头的那条还没发请求"
    # 后面两条确实是在领头那条有了第一次请求之后才起的
    assert model.seen, "假模型一次都没被调用"


def test_only_one_wave_per_process(model, fast_warm):
    """**只做一波** —— 第二批不该再等(缓存 TTL 几小时,本来就是热的)。"""
    _steps(model)
    tasks.dispatch_batch([{"prompt": "A", "fs": helpers.grant(), "wait": False},
                          {"prompt": "B", "fs": helpers.grant(), "wait": False}])
    assert tasks._WARMED

    started = time.monotonic()
    results, _ = tasks.dispatch_batch([
        {"prompt": "C", "fs": helpers.grant(), "wait": False},
        {"prompt": "D", "fs": helpers.grant(), "wait": False},
    ])
    elapsed = time.monotonic() - started

    assert len(results) == 2
    assert elapsed < fast_warm, f"第二批又等了一遍({elapsed:.3f}s)"


def test_a_single_dispatch_never_waits(model, fast_warm):
    """派一件活不该等 —— 没有"后面那一批"值得等。"""
    _steps(model)
    started = time.monotonic()
    tasks.dispatch_batch([{"prompt": "就一件", "fs": helpers.grant(), "wait": False}])
    assert time.monotonic() - started < fast_warm


def test_it_can_be_turned_off(model, monkeypatch):
    """`SUBAGENT_WARM_ENABLED=0` 时退回老行为:一起放,不等。"""
    monkeypatch.setattr(tasks, "SUB_WARM", False)
    monkeypatch.setattr(tasks, "SUB_WARM_SETTLE", 5.0)
    monkeypatch.setattr(tasks, "_WARMED", False)
    _steps(model)
    started = time.monotonic()
    results, note = tasks.dispatch_batch([
        {"prompt": "A", "fs": helpers.grant(), "wait": False},
        {"prompt": "B", "fs": helpers.grant(), "wait": False},
    ])
    assert len(results) == 2
    assert time.monotonic() - started < 0.5, "关掉了还在等"
    assert note == ""


def test_the_wait_times_out_and_says_so(model, monkeypatch):
    """**绝不能把主 agent 永远卡住**:等不到就放行,并如实说"这一批可能没吃上缓存"。"""
    monkeypatch.setattr(tasks, "SUB_WARM_SETTLE", 0.05)
    monkeypatch.setattr(tasks, "SUB_WARM_TIMEOUT", 0.1)     # 比"第一次请求"还短
    monkeypatch.setattr(tasks, "_WARMED", False)
    model.step_delay = 0.6                                  # 它的第一次请求要 0.6 秒才回来
    _steps(model)

    started = time.monotonic()
    results, note = tasks.dispatch_batch([
        {"prompt": "慢的", "fs": helpers.grant(), "wait": False},
        {"prompt": "后面的", "fs": helpers.grant(), "wait": False},
    ])
    elapsed = time.monotonic() - started
    model.step_delay = 0.0

    assert len(results) == 2, "超时也必须把整批派出去"
    assert elapsed < 0.5, f"超时没生效({elapsed:.2f}s)"
    assert "没等到" in note, f"该如实说一句:{note}"
    assert tasks._WARMED, "超时也要标记已预热,不然下一批又白等"


def test_a_leader_that_already_finished_counts_as_warmed(monkeypatch):
    """**"它已经跑完了" = "等到了",不是"没等到"。**

    命短的活(测试里那种、或者真实里第一步就失败退出的)可能在闸门第一次轮询**之前**
    就结束了。原来的轮询先判状态、后判用量,于是这种情况会被当成超时,**报一个假警报**
    ("没等到缓存落盘")—— 而它明明请求都发完了。这条只在负载高的时候才露出来
    (单跑通过、全量跑失败),正是那种最难查的错。
    """
    monkeypatch.setattr(tasks, "SUB_WARM_SETTLE", 0.01)
    monkeypatch.setattr(tasks, "SUB_WARM_TIMEOUT", 5.0)
    t = tasks.Task(id="t9", prompt="命短的活", status="done")
    t.ctx = agent_ctx.AgentCtx(role="sub", task_id="t9")
    t.ctx.total_usage["requests"] = 1
    tasks._TASKS["t9"] = t

    assert tasks._wait_cache_warm("t9") == "", "跑完了却报'没等到'"


def test_a_leader_that_fails_to_dispatch_does_not_block_the_rest(model, fast_warm):
    """领头那条压根没派出去(比如并发到顶了),剩下的不该跟着卡住。"""
    monkeypatch_ok = tasks.MAX_CONCURRENT
    tasks.MAX_CONCURRENT = 1
    try:
        _steps(model, n=8)
        tasks.dispatch("先占住名额", fs=helpers.grant(), wait=False)
        results, note = tasks.dispatch_batch([
            {"prompt": "派不出去的", "fs": helpers.grant(), "wait": False},
            {"prompt": "后面的", "fs": helpers.grant(), "wait": False},
        ])
    finally:
        tasks.MAX_CONCURRENT = monkeypatch_ok

    assert results[0].get("status") == "error", "前提:第一条该因为到上限被拒"
    assert tasks._WARMED, "派不出去也要标记,别让后面每批都等"


# ============================== 工具层 ==============================

def test_the_batch_tool_dispatches_everything(model, fast_warm):
    _steps(model)
    out = dispatch_task(batch=[
        {"task": "第一件", "write": ["reports/"]},
        {"task": "第二件", "write": ["notes/"]},
        {"task": "第三件", "write": []},
    ])

    assert "已派 3 件" in out, out
    ids = [t.id for t in tasks._TASKS.values()]
    assert len(ids) == 3
    assert tasks.get("t1").fs.write == ("reports/",), "每件自己的写范围要分别生效"
    assert tasks.get("t2").fs.write == ("notes/",)


def test_the_batch_tool_checks_its_input(model):
    assert "只能给一个" in dispatch_task(task="单件", batch=[{"task": "另一件"}])
    assert "第 1 项没有 task" in dispatch_task(batch=[{"write": ["reports/"]}])
    assert "任务描述是空的" in dispatch_task(batch=None, task="   ")
