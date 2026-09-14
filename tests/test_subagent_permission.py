"""子 agent 申请权限(suspend)→ 主 agent 批准(resume)。

**这条链曾经是断的,而且断在最后一步**:子 agent 被拒 → 挂起 → 申请书原文交上来了 →
主 agent 回一句「批准」→ **什么也没发生**。因为权限在派活那一刻就定死在 FsGrant 里了,
`resume` 只接受一句文本,它下一步照样被同一道墙挡住 —— 而它会以为批过了、主 agent 会以为
给过了。所以这一组测试的重点不是"流程走通",而是**批准必须真的落到授权上**。
"""
from __future__ import annotations

import helpers
from agent import tasks

TARGET = "reports/perm.md"


def _park_with_a_permission_request(model, write=()):
    """派一个"先硬写、被拒、再挂起申请"的子 agent,返回任务号。

    这段脚本就是真实流程的样子 —— 真模型撞到拒绝之后,该做的就是 suspend 申请。
    """
    model.set(
        ("我先把结论写到 reports 里。",
         [model.call("write_file", path=TARGET, content="结论")], ""),
        ("写不进去,我申请一下权限。",
         [model.call("suspend", ask="要写 reports/perm.md,但这次的活没给我写权限。"
                                    "给我 reports/ 的写权限,我就把结论和证据写进去。")], ""),
    )
    return tasks.dispatch("把结论写进 reports", fs=helpers.grant(write), wait=False)["task_id"]


def _finish_script(model):
    """批准之后该做的事:再写一次,然后收尾。"""
    model.set(
        ("收到,我写。", [model.call("write_file", path=TARGET, content="结论")], ""),
        ("写完了。", [], ""),
    )


def test_refused_write_parks_the_subagent_instead_of_failing(model, workspace):
    """被拒之后该**停下来问**,而不是硬撑、也不是崩掉。"""
    tid = _park_with_a_permission_request(model)
    t = helpers.wait_status(tid, ("waiting_input", "done", "failed", "truncated"))

    assert t.status == "waiting_input", f"实际 {t.status},error={t.error!r}"
    assert "reports/perm.md" in t.ask          # 申请书原文要交给主 agent
    assert not (workspace / TARGET).exists()   # 它没有绕过去


def test_parked_subagent_shows_up_in_the_main_agents_queue(model):
    """挂起的子 agent 必须**主动出现在主 agent 的待办里** —— 否则它会一直干等。"""
    tid = _park_with_a_permission_request(model)
    t = helpers.wait_status(tid, "waiting_input")

    assert tid in [x.id for x in tasks.needs_attention()]

    msg = tasks.report(t)["message"]
    assert "resume_task" in msg                # 说清怎么接着干
    assert "写权限" in msg                      # 带上它的原话
    assert "批准" in msg                        # 并且点明"光说批准没用"


def test_approval_actually_grants_the_permission(model, workspace):
    """**核心**:批准必须把权限真的给出去,它才能把活干完。"""
    tid = _park_with_a_permission_request(model)
    helpers.wait_status(tid, "waiting_input")
    _finish_script(model)

    tasks.resume(tid, answer="批准:给你 reports/ 的写权限,写完把文件名报给我。",
                 write=["reports/"], wait=True, timeout=20)
    t = helpers.wait_status(tid, ("done", "failed", "waiting_input"))

    assert t.status == "done", f"实际 {t.status},error={t.error!r}"
    assert (workspace / TARGET).is_file(), "权限没真的给到 —— 文件没写出来"
    assert any(m.get("role") == "user" and "批准" in str(m.get("content") or "")
               for m in t.messages), "主 agent 的答复没有进它的对话历史"
    assert helpers.structure_ok(t.messages)[0]


def test_approval_without_a_grant_does_not_help(model, workspace):
    """**反向对照**:同样回一句「批准」但不给权限 → 它第二次照样写不进去。

    没有这一条,上面那条通过了也说明不了什么 —— 万一是"它本来就能写"呢。
    """
    tid = _park_with_a_permission_request(model)
    helpers.wait_status(tid, "waiting_input")
    model.set(
        ("好,我再试。", [model.call("write_file", path=TARGET, content="结论")], ""),
        ("写不了,先干别的。", [], ""),
    )
    tasks.resume(tid, answer="批准,你写吧。", wait=True, timeout=20)
    t = helpers.wait_status(tid, ("done", "failed"))

    assert t.status == "done"
    assert not (workspace / TARGET).exists(), "没给权限却写进去了 —— 说明拦截本身有问题"
    denied = [m for m in t.messages
              if m.get("role") == "tool" and "拒绝" in str(m.get("content") or "")]
    assert len(denied) >= 2, "被拒时它应该拿到明确的拒绝理由(里面会写清允许范围)"


def test_grant_is_additive(model, workspace):
    """扩权是**往已有范围上加**,不是替换 —— 一次批准不该悄悄收走它原有的权限。"""
    model.set(("写不进去。", [model.call("suspend", ask="要写 reports/")], ""))
    tid = tasks.dispatch("写 reports", fs=helpers.grant(write=["notes/"]),
                         wait=False)["task_id"]
    helpers.wait_status(tid, "waiting_input")
    model.reply("好了。")
    tasks.resume(tid, answer="批准", write=["reports/"], wait=True, timeout=20)
    t = helpers.wait_status(tid, ("done", "failed"))

    assert set(t.fs.write) == {"notes/", "reports/"}, f"实际 {t.fs.write}"


def test_grant_must_pass_the_overlap_check(model):
    """**批准不是后门**:扩到别人正在写的范围上,照样得被拒。

    "两个 agent 同改一处、撞了不报错、事后查不出是谁改的" 这件事,不会因为
    「这是主 agent 批准的」就消失。
    """
    tasks._TASKS["tzz"] = tasks.Task(id="tzz", prompt="假的、占着 out/ 的活", session="",
                                     status="running", fs=helpers.grant(write=["out/"]))
    model.set(("干着。", [model.call("suspend", ask="要写 out/inner/")], ""))
    tid = tasks.dispatch("写 notes/ 里的东西", fs=helpers.grant(write=["notes/"]),
                         wait=False)["task_id"]
    helpers.wait_status(tid, "waiting_input")

    out = tasks.resume(tid, answer="批准", write=["out/inner/"], wait=False)

    assert "撞上" in out.get("message", ""), out
    assert tasks.get(tid).fs.write == ("notes/",), "被拒之后范围不该被动过"


def test_resume_cannot_grant_to_a_running_task(model):
    """它没在等回话时,别让"批准"把权限塞进去 —— 那等于绕过派活时的重叠检查。"""
    model.set(*[("干着。", [model.call("get_current_time")], "")] * 8)
    tid = tasks.dispatch("慢慢干", fs=helpers.grant(), wait=False)["task_id"]
    helpers.wait_status(tid, "running")

    out = tasks.resume(tid, answer="批准", write=["reports/"], wait=False)

    assert out["status"] == "error"
    assert tasks.get(tid).fs.write == ()
