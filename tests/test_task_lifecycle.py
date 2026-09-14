"""子 agent 的一生:派活 → 跑 → 叫停 / 验收 / 中断 → 重启后恢复。

**和另外两组的区别**:接管那组、权限那组盯的是"用户看得见的那条链";这里盯的是
**状态机本身和它说出来的话** —— 因为主 agent 是照着那些话做判断的。踩过的坑都在这上面:

  · 问一个**正在跑**的任务,回了一句「已完成」(兜底分支把新状态当成了 done),
    主 agent 当场把没干完的活当结论用了;
  · 报告太长时**悄悄截断** —— 拿着前 4000 字当全部去下结论;
  · 被 /switch 叫停的任务标成 closed(**丢弃**),而不是 interrupted(**暂停**),
    于是切回去没有人知道那件活还能接着做;
  · 重启后盘上那些 running 的任务没人管 —— 状态一直挂着"在跑",而它其实早死了。
"""
from __future__ import annotations

import json

import helpers
import pytest
from agent import tasks
from agent.commands import Context, dispatch as cmd

# ============================== 报告措辞 ==============================


def _task(tid: str, status: str, **kw) -> tasks.Task:
    return tasks.Task(id=tid, prompt="随便一件活", session=tasks._sid(),
                      status=status, **kw)


@pytest.mark.parametrize("status", ["queued", "running", "waiting_input", "done",
                                    "truncated", "failed", "interrupted", "closed"])
def test_a_report_carries_the_real_status(status):
    """**报告里的状态必须就是它真实的状态。**

    这里曾经是无条件兜底到"完成"那一支 —— 于是新加的状态(比如 running)会**悄悄**
    掉进去:问一个正在跑的活,回一句「子 agent t1 完成」。状态多起来之后,
    兜底就是个陷阱,所以每一个都得显式对上。
    """
    r = tasks.report(_task("t1", status, ask="要写 reports/", error="boom", result="结论"))
    assert r["status"] == status
    assert r["task_id"] == "t1"


def test_a_running_task_is_never_reported_as_finished():
    """**这条是那个坑的钉子**:正在跑 ≠ 完成。"""
    msg = tasks.report(_task("t1", "running"))["message"]
    assert "正在跑" in msg
    assert "完成" not in msg, "把没干完的活说成完成,主 agent 会拿它当结论"
    assert "/subtasks" in msg, "该告诉它怎么查进展、怎么接管过去看"


def test_a_truncated_task_says_the_report_was_never_written():
    """撞步数上限不算完成 —— 已经做的都在,但**报告没写完**。"""
    msg = tasks.report(_task("t1", "truncated"))["message"]
    assert "没干完" in msg
    assert "resume_task" in msg, "得告诉主 agent 可以接着让它干"
    assert "别自己接手" in msg, "不然它会把读过的东西在自己上下文里再读一遍"


def test_an_interrupted_task_is_offered_as_resumable():
    """被中断是**暂停**,不是丢弃 —— 它停在哪儿、接着干会重做什么,都得说清。"""
    msg = tasks.report(_task("t1", "interrupted", ask="正在跑 vm_run"))["message"]
    assert "resume_task" in msg
    assert "结果未知" in msg, "续跑会重做那一步,不说清就是让它蒙着来"


def test_a_long_report_is_clipped_and_says_where_the_rest_is(model):
    """报告太长要截 —— 但**必须说清截了**,否则主 agent 以为那就是全部。

    上限是 RESULT_MAX_CHARS(4000),所以这里真造一份超长的报告。
    """
    long_text = "结论:" + "啰嗦" * 3000                 # 6000 字
    model.reply(long_text)
    tid = tasks.dispatch("写一份很长的报告", fs=helpers.grant(), wait=False)["task_id"]
    t = helpers.wait_status(tid, ("done", "failed", "truncated"))
    assert t.status == "done", f"实际 {t.status},error={t.error!r}"

    msg = tasks.report(t)["message"]
    assert "报告过长" in msg
    assert "messages.jsonl" in msg, "得给出去哪儿读全文"
    assert msg.count("啰嗦") < 3000, "说好了截断,别整个倒进主 agent 的上下文"


# ============================== 派活时的闸门 ==============================


def test_dispatch_refuses_two_agents_writing_the_same_place(model, slow):
    """两个子 agent 同改一处:**撞了不报错**、只是结果对不上,事后查不出是谁改的。

    所以要在派活那一刻就拦下来,而不是等它们跑完对不上账。
    """
    slow["delay"] = 0.8
    model.set(*[("占着 out/。", [model.call("get_current_time")], "")] * 6)
    first = tasks.dispatch("占住 out/", fs=helpers.grant(write=["out/"]),
                           wait=False)["task_id"]
    slow["entered"].wait(10)

    r = tasks.dispatch("也要写 out/", fs=helpers.grant(write=["out/x.md"]), wait=False)

    assert r["status"] == "error", r
    assert "撞上" in r["message"] and first in r["message"]
    slow["delay"] = 0.0
    tasks.kill(first)
    helpers.wait_status(first, ("closed", "done", "failed"))


def test_it_wont_start_more_than_the_limit(model, monkeypatch, slow):
    """同时在跑的个数有上限 —— 每个都是独立的 API 流和上下文,不是越多越好。"""
    monkeypatch.setattr(tasks, "MAX_CONCURRENT", 1)
    slow["delay"] = 0.8
    model.set(*[("占着。", [model.call("get_current_time")], "")] * 6)
    first = tasks.dispatch("占住名额", fs=helpers.grant(), wait=False)["task_id"]
    slow["entered"].wait(10)

    r = tasks.dispatch("第二件", fs=helpers.grant(), wait=False)

    assert r["status"] == "error"
    assert "上限" in r["message"] and first in r["message"], "得说清是谁占着"
    slow["delay"] = 0.0
    tasks.kill(first)
    helpers.wait_status(first, ("closed", "done", "failed"))


def test_the_main_agent_cannot_write_into_a_live_subagents_scope(model, slow):
    """**划范围还有另一半**:主 agent 也不能去掀子 agent 脚下的地板。

    只约束子 agent 之间的话,主 agent 一句话就能让两边同时改同一批文件 ——
    而且撞了不报错。拦在 safe_path 里,是因为那是所有写操作的必经之路。
    """
    from agent import core

    slow["delay"] = 0.8
    model.set(*[("占着 out/。", [model.call("get_current_time")], "")] * 6)
    tid = tasks.dispatch("占住 out/", fs=helpers.grant(write=["out/"]),
                         wait=False)["task_id"]
    slow["entered"].wait(10)

    with pytest.raises(PermissionError) as e:
        core.safe_path("out/report.md", "write")
    assert tid in str(e.value), f"得说清是谁在管这块:{e.value}"

    # 范围之外照常能写 —— 拦的是撞车,不是"主 agent 不许写"
    assert core.safe_path("notes/x.md", "write").name == "x.md"

    slow["delay"] = 0.0
    tasks.kill(tid)
    helpers.wait_status(tid, ("closed", "done", "failed"))


# ============================== 叫停 / 验收 ==============================


def test_kill_stops_it_between_steps(model, slow):
    """叫停是**商量式的**:它把当前这一步做完就停,正在跑的工具调用不打断 ——
    但**不会再多走一步**。
    """
    slow["delay"] = 0.8
    model.set(*[("干着。", [model.call("get_current_time")], "")] * 6)
    tid = tasks.dispatch("慢慢干", fs=helpers.grant(), wait=False)["task_id"]
    slow["entered"].wait(10)                   # 它此刻正卡在一次工具调用里

    out = tasks.kill(tid)
    assert "已叫停" in out
    slow["delay"] = 0.0
    t = helpers.wait_status(tid, ("closed", "done", "failed"))

    assert t.status == "closed", f"实际 {t.status},error={t.error!r}"
    assert "中止" in t.result
    steps = sum(1 for m in t.messages if m.get("role") == "assistant")
    assert steps == 1, f"叫停之后它又走了 {steps - 1} 步"


def test_kill_on_a_task_that_is_not_running_says_so(model):
    model.reply("干完了。")
    tid = tasks.dispatch("小事", fs=helpers.grant(), wait=False)["task_id"]
    helpers.wait_status(tid, ("done", "failed"))

    assert "不用停" in tasks.kill(tid)


def test_accept_keeps_the_conversation_on_disk(model):
    """验收关闭只是**从活跃列表里移走** —— 对话和产出一字不丢,用户之后翻得到。"""
    model.reply("干完了。")
    tid = tasks.dispatch("小事", fs=helpers.grant(), wait=False)["task_id"]
    helpers.wait_status(tid, ("done", "failed"))

    out = tasks.finish(tid, "accept")

    assert "已验收关闭" in out
    assert tasks.get(tid).status == "closed"
    assert tasks.get(tid).ctx is None, "关了就把它那份输出缓冲放掉(不算小)"
    assert (tasks.task_dir(tid) / "messages.jsonl").is_file(), "对话该还在盘上"


def test_rework_sends_it_back_with_the_note(model):
    """打回重做:**必须把「哪里不行」带给它**,不然它只会把同一件事再做一遍。"""
    model.reply("第一版好了。")
    tid = tasks.dispatch("写份小结", fs=helpers.grant(), wait=False)["task_id"]
    helpers.wait_status(tid, ("done", "failed"))
    assert "重做" not in tasks.finish(tid, "rework")      # 空着打回 → 拒绝

    model.reply("按你说的改好了。")
    out = tasks.finish(tid, "rework", note="结论太笼统,把证据列出来")
    assert "重做" in out

    t = tasks.get(tid)
    assert helpers.wait_until(
        lambda: sum(1 for m in t.messages if m.get("role") == "assistant") >= 2, 20)
    said = [m for m in t.messages if "结论太笼统" in str(m.get("content") or "")]
    assert len(said) == 1, f"打回的意见丢了或重复了:{said}"
    assert helpers.structure_ok(t.messages)[0]


def test_finish_refuses_while_it_is_still_running(model, slow):
    """它还在跑时不能验收 —— 那等于默认"它说的就是真的",而它还没说。"""
    slow["delay"] = 0.8
    model.set(*[("干着。", [model.call("get_current_time")], "")] * 6)
    tid = tasks.dispatch("慢慢干", fs=helpers.grant(), wait=False)["task_id"]
    slow["entered"].wait(10)

    assert "还在跑" in tasks.finish(tid, "accept")
    assert "还在跑" in tasks.finish(tid, "rework", note="重做")
    slow["delay"] = 0.0
    tasks.kill(tid)
    helpers.wait_status(tid, ("closed", "done", "failed"))


def test_switching_a_session_pauses_them_instead_of_discarding(model, slow):
    """`/switch` 叫停原会话的子 agent —— 标的是**中断**(暂停),不是 closed(丢弃)。

    两者在报告里说的话完全不同:一个说"接着干?",一个说"不要了"。标错了,
    切回去就没人知道那件活还能接着做。
    """
    slow["delay"] = 0.8
    model.set(*[("干着。", [model.call("get_current_time")], "")] * 6)
    tid = tasks.dispatch("干半截就走", fs=helpers.grant(), wait=False)["task_id"]
    slow["entered"].wait(10)

    assert tasks.stop_for_switch(tasks._sid()) == 1
    slow["delay"] = 0.0
    t = helpers.wait_status(tid, ("interrupted", "closed", "done"))

    assert t.status == "interrupted", f"实际 {t.status} —— 被当成丢弃了"
    msg = tasks.report(t)["message"]
    assert "resume_task" in msg and "中断" in msg


def test_reset_clears_the_subagents_too(model, slow):
    """**`/new`(=/reset)之后子 agent 还留着** —— 用户报的 bug。

    对话被丢掉了,挂在它下面的活却还在:跑着的继续烧 token,干完的继续按「待办」
    通报进一段**全新的**对话(用户刚说了这段不要了)。实测下一次请求立刻被它打扰。
    """
    from agent.ctx import AgentCtx

    slow["delay"] = 0.8
    model.set(*[("干着。", [model.call("get_current_time")], "")] * 6)
    running = tasks.dispatch("跑着的活", fs=helpers.grant(), wait=False)["task_id"]
    slow["entered"].wait(10)
    # 再摆一个"已经干完、还没验收"的(直接造出来,免得和上面那个抢同一份假模型脚本)
    tasks._TASKS["t9"] = tasks.Task(id="t9", prompt="干完的活", session=tasks._sid(),
                                    status="done", result="结论",
                                    ctx=AgentCtx(role="sub", task_id="t9"))
    assert "干完的活" in tasks.listing()
    tasks.attach(running)                 # 正接管着其中一个

    out = cmd("/reset", _ctx())

    slow["delay"] = 0.0
    assert "会话已重置" in out
    assert "干完的活" not in tasks.listing(), "干完的那个还挂在清单上"
    assert not tasks.needs_attention(), "它的结果会被通报进刚清空的对话"
    assert tasks.attached() is None, "接管着的那一个被收掉了,接管也得退出"
    t = helpers.wait_status(running, ("closed", "done", "failed", "interrupted"))
    assert t.status == "closed", f"跑着的那个没停(实际 {t.status})"
    assert t.messages, "只是作废,不是把它的对话删掉 —— 盘上那份该还在"


def test_reset_leaves_other_sessions_alone(model):
    """只清当前会话的子 agent —— 别的会话的活不该被连累。"""
    tasks._TASKS["t8"] = tasks.Task(id="t8", prompt="别的会话的活", session="别的会话",
                                    status="done")

    cmd("/reset", _ctx())

    assert tasks.get("t8").status == "done"


# ============================== 重启之后 ==============================


def test_recover_marks_the_dead_ones_interrupted_and_does_not_rerun_them(model):
    """重启时盘上那些 running 的任务:**只摆正状态、报给主 agent,绝不自动重跑。**

    中断那一步的副作用是未知的(可能已经写了文件、发了请求),自动重跑会把它再做一遍。
    但也不能不管 —— 那样它会一直挂着"在跑",而它其实早死了。
    """
    sid = tasks._sid()
    dead = tasks.Task(id="t7", prompt="被杀之前正在跑的活", session=sid, status="running")
    tasks._save(dead)                                   # 落一份 meta:状态是 running
    tasks.append_message("t7", {"role": "user", "content": "干这个"})
    done = tasks.Task(id="t8", prompt="上次就干完了的活", session=sid, status="done")
    tasks._save(done)
    before = len(tasks._load_messages("t7"))

    tasks._TASKS.clear()                                # 模拟:进程被杀,内存全没了
    notes = tasks.recover()

    assert tasks.get("t7").status == "interrupted"
    assert tasks.get("t7").ask, "得说清它是断在哪儿的"
    assert any("resume_task" in n for n in notes), f"该告诉主 agent 怎么接着干:{notes}"
    assert len(tasks._load_messages("t7")) == before, "不该自动重跑(那会把中断那步再做一遍)"
    assert tasks.get("t7").status == "interrupted", "摆正之后也不该自己跑起来"
    assert tasks.get("t8").status == "done", "上次就干完的照原样读回来"
    assert not any("t8" in n for n in notes), "干完的不该再报一遍"
    meta = json.loads((tasks.task_dir("t7") / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "interrupted", "新状态得落盘,不然下次启动又当它是 running"


def test_a_restart_remembers_which_session_the_task_belongs_to(model):
    """`Task.session` 原来是**没落盘**的 —— 重启后读回来的任务 session 是空的,它的落盘
    和临时区就跟着"此刻谁在前台"走了。

    `Task.session` 那段注释写明了本意("文件放哪不该取决于此刻谁在前台"),但它只对
    "派活那一刻"成立。多会话共用一个工作区时(支持的用法),重启后它可能指到别的会话去。
    """
    model.reply("干完了。")
    tid = tasks.dispatch("干活", fs=helpers.grant(), wait=False)["task_id"]
    helpers.wait_status(tid, ("done", "failed"))
    sid = tasks.get(tid).session

    tasks._TASKS.clear()                       # 模拟:进程重启
    t = tasks.get(tid)

    assert t.session == sid, "会话没落盘 —— 读回来的任务成了无主的"
    assert t.fs.scratch == (f".tmp/{sid}/{tid}/",), "临时区也该认得出是哪个会话的"


# ============================== 清单与说话 ==============================


def test_listing_hides_closed_ones_but_still_counts_them(model):
    """已验收关闭的默认不列 —— 但**数量要告诉你**,免得以为它们消失了。"""
    model.reply("干完了。")
    tid = tasks.dispatch("已经干完的活", fs=helpers.grant(), wait=False)["task_id"]
    helpers.wait_status(tid, ("done", "failed"))
    assert "已经干完的活" in tasks.listing()

    tasks.finish(tid, "accept")
    out = tasks.listing()
    assert "已经干完的活" not in out
    assert "另有 1 个已验收关闭的" in out, "得让人知道它们还在,只是不用看了"
    assert "已经干完的活" in tasks.listing(show_closed=True)


def test_listing_shows_what_a_parked_one_is_waiting_for(model):
    model.set(("写不进去。", [model.call("suspend", ask="要 reports/ 的写权限")], ""))
    tid = tasks.dispatch("写点东西", fs=helpers.grant(), wait=False)["task_id"]
    helpers.wait_status(tid, "waiting_input")

    out = tasks.listing()

    assert "在等: 要 reports/ 的写权限" in out, f"卡在等什么必须一眼看到:{out}"


def test_another_sessions_tasks_are_not_here(model):
    """任务属于**它自己那个会话** —— 换到别的会话,就不该在这儿列出来、也不该来报到。"""
    tasks._TASKS["t9"] = tasks.Task(id="t9", prompt="别的会话的活", session="别的会话",
                                    status="done")
    model.reply("我这边的活干完了。")
    tid = tasks.dispatch("这个会话的活", fs=helpers.grant(), wait=False)["task_id"]
    helpers.wait_status(tid, ("done", "failed"))

    assert "别的会话的活" not in tasks.listing()
    assert "t9" not in [t.id for t in tasks.needs_attention()]
    assert tid in [t.id for t in tasks.needs_attention()], "本会话的照样要报到"


def test_you_can_keep_talking_to_a_finished_subagent(model):
    """干完了还能接着聊 —— 等于"再让它做一件事",而且**照常通报给主 agent**。

    (主 agent 是协调者,该知道下面又发生了什么;这次是用户直接交代的,它没在等,
    所以按"没人等"处理。)
    """
    model.reply("第一件事干完了。")
    tid = tasks.dispatch("做件事", fs=helpers.grant(), wait=False)["task_id"]
    t = helpers.wait_status(tid, ("done", "failed"))

    model.reply("又干完一件。")
    out = tasks.tell(tid, "再去 notes/ 里看一眼")

    assert "接着做" in out
    assert helpers.wait_until(
        lambda: sum(1 for m in t.messages if m.get("role") == "assistant") >= 2, 20)
    assert "再去 notes/ 里看一眼" in str([m for m in t.messages
                                          if m.get("role") == "user"][-1]["content"])
    assert not t.awaited, "用户直接交代的活,主 agent 不在等 —— 它得收到通报"
    assert tid in [x.id for x in tasks.needs_attention()]


def test_tell_refuses_an_empty_line_and_an_unknown_id(model):
    model.reply("干完了。")
    tid = tasks.dispatch("小事", fs=helpers.grant(), wait=False)["task_id"]
    helpers.wait_status(tid, ("done", "failed"))

    assert tasks.tell(tid, "   ") == "(空话,没发)"
    assert "没有" in tasks.tell("t404", "在吗")


# ============================== 指令层 ==============================

def _ctx() -> Context:
    return Context(messages=[])


def test_a_bare_task_number_works_in_the_commands_too(model):
    """列表里印的是 `t2`,人眼看到的是 2 —— `/subtasks 2` 和 `/subtasks log 2` 都得认。"""
    model.reply("干完了。")
    tid = tasks.dispatch("按号码找它", fs=helpers.grant(), wait=False)["task_id"]
    helpers.wait_status(tid, ("done", "failed"))

    assert "按号码找它" in cmd(f"/subtasks {tid[1:]}", _ctx())
    assert "干完了" in cmd(f"/subtasks {tid[1:]} log", _ctx())
    assert "干完了" in cmd(f"/subtasks {tid[1:]} enter", _ctx())
    cmd("/subtasks off", _ctx())


def test_subtasks_all_reaches_the_closed_ones(model):
    model.reply("干完了。")
    tid = tasks.dispatch("已经验收的活", fs=helpers.grant(), wait=False)["task_id"]
    helpers.wait_status(tid, ("done", "failed"))
    tasks.finish(tid, "accept")

    assert "已经验收的活" not in cmd("/subtasks", _ctx())
    assert "已经验收的活" in cmd("/subtasks all", _ctx())
