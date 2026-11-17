"""通报**不能反复吵闹** —— 已经被主 agent 消费过的,别再喊一遍。

两种归零的时机都得兜住(`Task.delivered` 只活在内存里):

  · 压缩:通报可能正好落进被摘要掉的那一段 —— 那种情况**要**重报,它是待办不是背景;
  · 退出再启动 / 切回某个会话:对话历史从盘上读回来,而 delivered 是新的 ——
    这种**不能**重报,历史里就写着呢,主 agent 自己翻得到。

实测现象:都聊完、退出、第二天进来,一屏"某个子 agent 有结果了" —— 全是昨天处理过的。
"""
from __future__ import annotations

import helpers
from agent import loop, tasks


def _finished_task(model, **kw) -> str:
    """派一个立刻干完的子 agent,返回任务号。"""
    model.reply("干完了。")
    tid = tasks.dispatch("小事一桩", fs=helpers.grant(), wait=False, **kw)["task_id"]
    helpers.wait_status(tid, ("done", "failed"))
    return tid


def _announce(messages: list[dict], tid: str) -> None:
    """走一遍真正的注入路径(而不是手写一条假消息)。"""
    loop._inject_notices(messages)


def test_a_notice_is_injected_once(model):
    messages: list[dict] = [{"role": "system", "content": "x"}]
    tid = _finished_task(model)

    _announce(messages, tid)
    n = len(messages)
    _announce(messages, tid)              # 再走一步

    assert len(messages) == n, "同一份报告被塞进对话两次"


def test_restart_does_not_renag(model):
    """**核心**:重启之后,历史里已经有的通报不再重报。"""
    messages: list[dict] = [{"role": "system", "content": "x"}]
    tid = _finished_task(model)
    _announce(messages, tid)
    assert any(f"[子 agent {tid}" in str(m.get("content")) for m in messages)

    # 模拟"退出再启动":delivered 是内存里的,新进程里是空的;历史从盘上读回来。
    t = tasks.get(tid)
    t.delivered = False
    assert t in tasks.needs_attention(), "前提:不重算的话它就会被再喊一遍"

    loop.reconcile_announced(messages)

    assert t not in tasks.needs_attention(), "历史里已经通报过,不该再喊"


def test_a_fresh_session_still_gets_told(model):
    """历史里**没有**那条通报(新会话、或者被 /reset 清过)→ 照报不误。"""
    messages: list[dict] = [{"role": "system", "content": "x"}]
    tid = _finished_task(model)
    t = tasks.get(tid)
    t.delivered = False

    loop.reconcile_announced(messages)    # 对话里没有它的通报

    assert t in tasks.needs_attention(), "主 agent 确实不知道这件事,该报"


def test_compaction_renags_what_it_swallowed(model):
    """压缩把通报吃掉了 → **得重报**。它是待办,不该跟着消息一起消失。"""
    messages: list[dict] = [{"role": "system", "content": "x"}]
    tid = _finished_task(model)
    _announce(messages, tid)
    t = tasks.get(tid)
    assert t.delivered

    # 压缩:那条通报被摘要顶掉了(这里直接模拟"历史里不再有它")
    messages[:] = [{"role": "system", "content": "x"},
                   {"role": "user", "content": loop.session_tag() + " (以上对话已压缩…)"}]
    loop.reconcile_announced(messages)

    assert not t.delivered, "被压缩吃掉的通报必须重报"
    _announce(messages, tid)
    assert any(f"[子 agent {tid}" in str(m.get("content")) for m in messages)


def test_a_resumed_run_is_announced_again(model, slow):
    """被打断的活接着干完,**要再报一次** —— 上一次报的是「它中断了」,不是结果。

    实测现象:主 agent 处理过那条中断通报后,resume_task 让它接着干;它干完了,
    主 agent 却收不到任何通知,只能等用户提醒才想起来去看报告。
    根因是 delivered 在上一轮已经置 True,而恢复时没有归零。
    """
    slow["delay"] = 0.8
    model.set(*[("干着。", [model.call("get_current_time")], "")] * 6)
    tid = tasks.dispatch("干半截就走", fs=helpers.grant(), wait=False)["task_id"]
    slow["entered"].wait(10)

    tasks.stop_for_switch(tasks._sid())
    slow["delay"] = 0.0
    t = helpers.wait_status(tid, ("interrupted",))
    assert t.status == "interrupted"

    messages: list[dict] = [{"role": "system", "content": "x"}]
    _announce(messages, tid)
    assert t.delivered, "前提:中断那条通报已经交给主 agent 了"

    model.reply("这次真做完了。")
    tasks.resume(tid, answer="接着把剩下的做完", wait=False)
    t = helpers.wait_status(tid, ("done",))
    assert t.status == "done"

    assert t in tasks.needs_attention(), (
        "重新跑完的结果没有被通报 —— 主 agent 只能等用户提醒才去看")


def test_closed_tasks_never_come_back(model):
    """已经验收关闭的**永远**不再出现 —— 不管历史怎么样。"""
    tid = _finished_task(model)
    tasks.finish(tid, "accept")
    t = tasks.get(tid)
    t.delivered = False

    assert t not in tasks.needs_attention()
    loop.reconcile_announced([{"role": "system", "content": "x"}])
    assert t not in tasks.needs_attention()
