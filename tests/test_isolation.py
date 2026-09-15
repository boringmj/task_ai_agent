"""子 agent 的上下文**不会**漏给主 agent —— 这条要由测试钉住,不能靠"设计上应该不会"。

主 agent 拿到的**只有 `report()` 那一段**(结论/产出/没做成的/存疑,截断到 4000 字)。
子 agent 的完整对话落在 `sessions/<会话>/tasks/<id>/`,那在**工作区之外** —— 模型的文件
工具够不到(见 `core.safe_path`),所以"主 agent 去读子 agent 的全部上下文"这条路不存在。

**这种隔离最容易被一次"顺手加个便利"破坏**:比如报告里塞一句"细节去读 <那个路径>",
或者某个工具为了省事直接用 `open()` 而不走 `safe_path`。而破坏之后**不会报错** ——
只会让子 agent 读过的整片上下文(几十万 token)悄悄倒进主 agent 最宝贵的那个上下文里。
所以这里钉两件事:①**读不到**;②报告里**不出现**那些路径 —— 出现了就等于告诉了模型一个
它够不到的地方,而它够不到的时候倾向于编。
"""
from __future__ import annotations

import helpers
import pytest
from agent import core, tasks
from agent.ctx import FsGrant, use
from agent.loop import _inject_notices


def _main_ctx():
    """主 agent 的运行时上下文(授权和 cli 里给的一致)。"""
    from agent.ctx import AgentCtx
    return AgentCtx(role="main",
                    fs=FsGrant.for_main(core.scratch_scope(tasks._sid(), "main")))


def _finished(model, text: str = "干完了。") -> str:
    model.reply(text)
    tid = tasks.dispatch("干活", fs=helpers.grant(), wait=False)["task_id"]
    helpers.wait_status(tid, ("done", "failed"))
    return tid


# ============================== 读不到 ==============================


def test_the_main_agent_cannot_read_a_subagents_transcript(model):
    """子 agent 的完整对话在**工作区之外** —— 它读过的那几十万 token 就锁在那儿。"""
    tid = _finished(model)
    transcript = tasks.task_dir(tid) / "messages.jsonl"
    assert transcript.is_file(), "前提:它的对话真的落盘了"

    with use(_main_ctx()):
        with pytest.raises(PermissionError) as e:
            core.safe_path(str(transcript), "read")
        assert "工作区" in str(e.value)


def test_the_whole_session_dir_is_out_of_reach(model):
    """整个会话目录(对话、占用标记、虚拟机磁盘)都在工作区外面。"""
    from agent import session as store

    tid = _finished(model)
    for target in (store.SESSIONS_DIR, store.session_dir(tasks._sid()),
                   store.session_file(tasks._sid()), tasks.task_dir(tid)):
        with use(_main_ctx()):
            with pytest.raises(PermissionError):
                core.safe_path(str(target), "read")


def test_it_cannot_be_reached_by_a_relative_climb_either(model):
    """`../...` 这种绕法也不行 —— `resolve()` 会把 `..` 展开再判。"""
    import os
    from agent import session as store

    _finished(model)
    target = store.session_file(tasks._sid())
    rel = os.path.relpath(target, core.ROOT)
    if rel.startswith("..") is False:
        pytest.skip("这个环境的会话目录在工作区里(测试装置不同),绕行测试不适用")
    with use(_main_ctx()):
        with pytest.raises(PermissionError):
            core.safe_path(rel, "read")


def test_two_subagents_cannot_read_each_others_transcripts(model):
    """子 agent 之间也一样:对方的对话读不到 —— 不然"并排跑"就成了互相抄答案。"""
    from agent.ctx import AgentCtx

    a = _finished(model)
    b = _finished(model)
    with use(AgentCtx(role="sub", task_id=a, fs=tasks.get(a).fs)):
        with pytest.raises(PermissionError):
            core.safe_path(str(tasks.task_dir(b) / "messages.jsonl"), "read")


def test_a_scratch_area_is_write_protected_but_not_read_protected(model):
    """**临时区只防写、不防读** —— 这条要写清楚,免得被当成"私密空间"用。

    它在工作区**里面**(必须如此:容器只挂工作区,技能脚本要在容器里产出中间文件),
    而工作区本来就是所有 agent 共读的。所以:

      · **写**:别人进不来(`.tmp/<会话>/<任务号>/` 是分开的,这是它防撞车的那一半);
      · **读**:进得来 —— 和它能读工作区里任何文件一样。中间产物**不是私密数据**,
        真要放敏感东西就不该放这儿(子 agent 的对话本身在工作区之外,那个才读不到)。
    """
    a = _finished(model)
    b = _finished(model)
    theirs = core.scratch_scope(tasks._sid(), b)

    assert not tasks.get(a).fs.allows(theirs + "raw.json", "write"), "写:该挡住"
    assert tasks.get(a).fs.allows(theirs + "raw.json", "read"), \
        "读:挡不住(它在工作区里)—— 这条是事实,别把它当私密空间"


# ============================== 报告里有什么 ==============================


def test_the_report_carries_the_conclusion_and_not_the_reading(model):
    """**工具结果不会自动倒给主 agent。** 它在自己那边读了多少东西都留在自己那边;
    跨过来的只有它最后那一段话(而且截断)。"""
    secret = "SECRET-MARKER-" + "x" * 40
    model.set(("我读一下那个文件。",
               [model.call("write_file", path="notes/a.txt", content=secret)], ""),
              ("读完了,结论是:一切正常。", [], ""))
    tid = tasks.dispatch("看一眼", fs=helpers.grant(write=["notes/"]),
                         wait=False)["task_id"]
    t = helpers.wait_status(tid, ("done", "failed"))

    assert secret in str(t.messages), "前提:那段内容确实进了它自己的上下文"
    msg = tasks.report(t)["message"]
    assert secret not in msg, "子 agent 读到的内容漏进主 agent 的上下文了"
    assert "一切正常" in msg, "该过来的结论要过来"


def test_the_report_does_not_hand_out_a_path_it_cannot_use(model):
    """报告里**不许出现它读不到的路径**。

    这里踩过一次:报告过长被截断时,原文写着"全文在 <会话目录>/messages.jsonl ——
    需要细节就去读,不要凭猜"。可主 agent 的文件工具**读不到那儿**(工作区之外),
    于是那句话等于让它去编 —— 而它确实会编。
    """
    tid = _finished(model, "结论:" + "啰嗦" * 3000)          # 超长 → 走截断那条路
    msg = tasks.report(tasks.get(tid))["message"]
    assert "报告过长" in msg, "前提:这条真的走了截断分支"
    assert "messages.jsonl" not in msg, "把够不到的路径交给它了"
    assert str(core.ROOT) not in msg and "sessions" not in msg


def test_a_failed_task_report_does_not_hand_out_paths_either(model):
    """失败那条同理 —— 要查原因得**问它本人**,不是让它去读一个读不到的文件。"""
    from agent import tasks as T

    t = T.Task(id="t9", prompt="炸了的活", session=T._sid(), status="failed",
               error="RuntimeError: boom")

    msg = T.report(t)["message"]

    assert "boom" in msg, "错误内容要带过来"
    assert "messages.jsonl" not in msg and "sessions" not in msg


def test_the_notice_into_the_main_agent_is_the_same_clipped_report(model):
    """通报注入主 agent 对话时,**内容和 report() 一模一样** —— 没有第二份更详细的版本。"""
    tid = _finished(model, "结论:" + "啰嗦" * 3000)
    messages: list[dict] = [{"role": "system", "content": "x"}]

    _inject_notices(messages)

    notice = [m for m in messages if m.get("role") == "user"][-1]["content"]
    assert tid in notice
    assert len(notice) < 5000, f"注入的那条没有截断({len(notice)} 字符)"
    assert "messages.jsonl" not in notice
