"""接管子 agent(`/subtasks <id> enter`):它的输出实时过来,你敲的字进它的对话。

**这组测试锁的是"用户看不懂自己在哪儿"那一类问题** —— 它们全都在真终端上暴露过:
接管之后提示符里的 `[t2]` 被 rich 当作样式标记吃掉(于是完全看不出自己在接管)、
"怎么退出"那行字排在几十行历史前面被顶出屏幕、照着列表敲 `2` 说没这个任务、
以及最要命的:跟它说话却**看不到它答什么**。
"""
from __future__ import annotations

import io
import time

import helpers
import pytest
from agent import cli, tasks
from agent.commands import Context, dispatch as cmd

ENTER = "enter"


def _ctx() -> Context:
    return Context(messages=[])


def _dispatch_talker(model, steps: int = 5):
    """派一个"一步一步慢慢干"的子 agent(每步睡 0.4 秒,好让接管看得见)。"""
    model.step_delay = 0.4
    model.set(*([("先四处看看。", [model.call("get_current_time")], "")] * steps
                + [("看完了。", [], "")]))
    return tasks.dispatch("随便看看", fs=helpers.grant(), wait=False)["task_id"]


def _capture(seconds: float) -> str:
    """把那几秒里直接写到 stdout 的东西抓回来(子 agent 的转发走的就是这条路)。"""
    import sys
    buf, real = io.StringIO(), sys.stdout
    try:
        sys.stdout = buf
        time.sleep(seconds)
    finally:
        sys.stdout = real
    return buf.getvalue()


# ============================== 接管与转发 ==============================

def test_attach_replays_history_and_streams_live(model):
    tid = _dispatch_talker(model)
    time.sleep(0.9)                       # 先让它跑两步,接管时才有"过去"可补

    out = cmd(f"/subtasks {tid} {ENTER}", _ctx())
    assert f"已接管 {tid}" in out
    assert "以下是它到刚才为止的记录" in out
    assert "先四处看看" in out, "接管时应该补上它到刚才为止说过的话"

    live = _capture(1.6)
    assert f"[{tid}]" in live, f"输出没有实时转发过来:{live[:200]!r}"
    assert "get_current_time" in live, "它调的工具也该看得到"
    model.step_delay = 0.0


def test_the_replayed_history_is_the_conversation_not_the_terminal_buffer(model):
    """**接管补出来的必须是它的对话,不是终端输出缓冲。**

    缓冲里只有"打到终端上的东西" —— 它说的话、工具调用行、一行账目。用户给它的任务、
    工具返回的结果、它交的结论**都不在里面**。实测接管过去只补出一行
    "t2 [done] 1 次请求 / 输出 32 tokens",跟没补一样。
    """
    model.set(("我去看看文件。", [model.call("read_file", path="没有这个文件.txt")], ""),
              ("看完了,这是我的结论:一切正常。", [], ""))
    tid = tasks.dispatch("检查一下工作区", fs=helpers.grant(), wait=False)["task_id"]
    helpers.wait_status(tid, ("done", "failed", "truncated"))

    out = cmd(f"/subtasks {tid} {ENTER}", _ctx())

    assert "检查一下工作区" in out, "用户给它的任务应该在记录里(终端缓冲里没有这个)"
    assert "我去看看文件。" in out, "它说过的话应该在"
    assert "read_file" in out, "它调过什么工具应该在"
    assert "没有这个文件.txt" in out, "工具返回的结果应该在 —— 不然看不出它到底拿到了什么"
    assert "一切正常" in out, "它交的结论应该在"
    cmd("/subtasks off", _ctx())


def test_history_is_rebuilt_after_a_restart(model):
    """**回来之后接管,历史也得在。**

    进程重启后任务是从盘上读回来的,`ctx` 是 None —— 原来那个"补历史"读的正是挂在
    ctx 上的终端缓冲,于是**补出来一片空白**。对话是逐条落盘的,所以从它重建就不会空。
    """
    model.set(("我先记一笔。", [model.call("write_file", path="notes/x.md", content="好")], ""),
              ("记好了。", [], ""))
    tid = tasks.dispatch("把这件事记下来", fs=helpers.grant(write=["notes/"]),
                         wait=False)["task_id"]
    helpers.wait_status(tid, ("done", "failed", "truncated"))
    before = cmd(f"/subtasks {tid} {ENTER}", _ctx())
    cmd("/subtasks off", _ctx())

    # 模拟"退出再启动":内存里的任务表清空,连活的上下文一起丢掉
    tasks._TASKS.clear()
    assert tasks.get(tid).ctx is None, "前提:读回来的任务没有活的上下文"

    after = cmd(f"/subtasks {tid} {ENTER}", _ctx())
    cmd("/subtasks off", _ctx())

    assert "把这件事记下来" in after, "重启后接管应该能重建出它的历史"
    assert "我先记一笔。" in after and "记好了。" in after
    assert "notes/x.md" in after, "落盘的工具调用也该重建出来"
    assert "以下是它到刚才为止的记录" in before and "以下是它到刚才为止的记录" in after


def test_a_very_long_history_is_clipped_and_says_where_to_read_it(model, monkeypatch):
    """太长就只补最后一段 —— 但**必须说清还有多少、去哪儿看全的**。

    上限调小来验这条分支(不然得让它真跑几百步,一条测试要等好几分钟)。
    """
    monkeypatch.setattr(tasks, "REPLAY_MAX_CHARS", 300)
    model.set(*[("又看了一个文件。", [model.call("get_current_time")], "")] * 6
              + [("终于看完了。", [], "")])
    tid = tasks.dispatch("翻很多文件", fs=helpers.grant(), wait=False)["task_id"]
    helpers.wait_status(tid, ("done", "failed", "truncated"))

    out = cmd(f"/subtasks {tid} {ENTER}", _ctx())
    cmd("/subtasks off", _ctx())

    assert "只补最后一段" in out
    assert f"/subtasks {tid} log" in out, "得告诉用户去哪儿看完整的"
    assert "终于看完了。" in out, "最后那一段(最近的)才是最该看到的"
    assert len(tasks.replay_text(tid)) < 600, "说好了只补一段,别整个倒出来"


def test_the_way_out_is_printed_last(model):
    """**"怎么退出"必须排在补出来的历史后面。**

    实测就是在这儿卡住的:提示语排在前面,几十行历史一冲就没了,于是用户接管完
    不知道该敲什么出去,试了 /exit —— 把整个程序关掉了。
    """
    tid = _dispatch_talker(model, steps=6)
    time.sleep(0.9)
    out = cmd(f"/subtasks {tid} {ENTER}", _ctx())

    assert "退出接管" in out
    assert out.index("退出接管") > out.index("以下是它到刚才为止的记录"), \
        "怎么退出的提示被历史顶到前面去了"
    # 顺带:得说清斜杠指令归谁 —— 用户最担心的就是"那我还敲得动 /subtasks off 吗"
    assert "/subtasks off" in out
    model.step_delay = 0.0


def test_detach_stops_forwarding(model):
    tid = _dispatch_talker(model)
    helpers.wait_status(tid, ("done", "failed", "truncated"))
    cmd(f"/subtasks {tid} {ENTER}", _ctx())
    assert tasks.attached() == tid
    cmd("/subtasks off", _ctx())
    assert tasks.attached() is None

    model.step_delay = 0.3
    model.set(*([("另一个活。", [model.call("get_current_time")], "")] * 4
                + [("好了。", [], "")]))
    other = tasks.dispatch("另一件事", fs=helpers.grant(), wait=False)["task_id"]
    live = _capture(1.2)
    assert f"[{other}]" not in live, "退出接管之后不该再往终端上刷"
    model.step_delay = 0.0
    assert tasks.replay_text(other).strip(), "它的记录还得在(不然 /subtasks log 就没内容了)"


def test_task_id_accepts_a_bare_number(model):
    """列表里印的是 `- t2 [done]`,人眼看到的就是 2 —— 照着敲不该说"没这个任务"。"""
    tid = _dispatch_talker(model)
    helpers.wait_status(tid, ("done", "failed", "truncated"))
    assert tasks.get(tid[1:]) is not None, "纯数字任务号应该认"
    assert "没有" not in cmd(f"/subtasks {tid[1:]}", _ctx())
    assert f"已接管 {tid}" in cmd(f"/subtasks {tid[1:]} {ENTER}", _ctx())
    cmd("/subtasks off", _ctx())


# ============================== 提示符与退出 ==============================

def test_the_prompt_shows_which_subagent_you_are_talking_to(monkeypatch):
    """**接管时提示符里的 `[t2]` 必须显示出来。**

    它走的是 rich 的 `console.print`,而 rich 默认开着 markup —— `[t2]` 会被当成样式标记
    **静默吃掉**,用户看到的是一个光秃秃的 ` > `,完全不知道自己正在接管某个子 agent。
    这条断言就是钉住那个 markup=False 的。
    """
    from rich.console import Console
    out = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=out, width=110))
    cli._INPUT.put("\n")                       # 预置一个空行 = 立刻提交
    cli._read_multiline("[t2] > ")
    assert "[t2]" in out.getvalue(), f"提示符被吃掉了:{out.getvalue()!r}"


def test_exit_while_attached_only_detaches(model, monkeypatch):
    """/exit 在接管中只退到主终端 —— 不能把整个程序关掉。"""
    from rich.console import Console
    tid = _dispatch_talker(model)
    helpers.wait_status(tid, ("done", "failed", "truncated"))
    cmd(f"/subtasks {tid} {ENTER}", _ctx())
    monkeypatch.setattr(cli, "console", Console(file=io.StringIO(), width=110))

    assert cli._exit_or_detach() is False, "接管中敲 /exit 不该退出程序"
    assert tasks.attached() is None, "应该已经退出接管"
    assert cli._exit_or_detach() is True, "再敲一次才真的退出"


# ============================== 半路插话 ==============================

def test_talk_to_it_while_it_is_busy(model, slow):
    """它卡在一次工具调用里时插话 —— **不能插在 assistant 和它的 tool 结果之间**。

    `vm_run` 一条命令几十秒、`grep_files` 翻大树也要几秒,这个中间态很宽。真插进去的话,
    下一次请求的历史就是非法的(API 直接 400),而它只会表现为"莫名失败"。
    """
    slow["delay"] = 1.2
    model.set(("先看一眼。", [model.call("get_current_time")], ""), ("看完了。", [], ""))
    tid = tasks.dispatch("边看边等", fs=helpers.grant(), wait=False)["task_id"]
    slow["entered"].wait(10)                   # 等它真的卡进工具里
    time.sleep(0.1)

    tasks.tell(tid, "插一句:只看 reports/,别翻别的。")
    slow["delay"] = 0.0
    t = helpers.wait_status(tid, ("done", "failed", "truncated"))

    said = [m for m in t.messages if "插一句" in str(m.get("content") or "")]
    assert len(said) == 1, f"插进去的话丢了或重复了:{said}"
    assert said[0].get("role") == "user"
    ok, why = helpers.structure_ok(t.messages)
    assert ok, why


def test_the_interjection_lands_at_a_safe_point(model, slow):
    """插话必须落在**上一步的工具结果之后** —— 那是它唯一安全的落脚点。

    上面那条测的是"结构合法";这条测的是**为什么合法**:插话是排队等它自己取的
    (见 loop._take_pending),而不是写的人直接往历史尾巴上摁。直接摁的话,它正好卡在
    "assistant 写了、tool 结果还没写"那会儿,下一次请求就非法了 —— 而它只会表现为
    "莫名失败"。
    """
    slow["delay"] = 1.0
    model.set(("先看一眼。", [model.call("get_current_time")], ""), ("看完了。", [], ""))
    tid = tasks.dispatch("边看边等", fs=helpers.grant(), wait=False)["task_id"]
    slow["entered"].wait(10)
    time.sleep(0.1)

    tasks.tell(tid, "插一句:只看 reports/。")
    slow["delay"] = 0.0
    t = helpers.wait_status(tid, ("done", "failed", "truncated"))

    at = [i for i, m in enumerate(t.messages)
          if "插一句" in str(m.get("content") or "")]
    assert len(at) == 1, f"插进去的话丢了或重复了:{at}"
    prev = t.messages[at[0] - 1]
    assert prev.get("role") == "tool", \
        f"插话前一条是 {prev.get('role')} —— 它插到工具配对中间去了"


def test_you_can_see_what_it_answers(model):
    """**跟它说话,得能看到它答什么。**

    它最后一轮说的话原来只 return 出去存进 result —— 用户说完一句,屏幕上只有一行账目
    ("1 次请求 / 输出 41 tokens")。那不叫接管,那叫往门缝里塞纸条。
    """
    tid = _dispatch_talker(model)
    helpers.wait_status(tid, ("done", "failed", "truncated"))
    cmd(f"/subtasks {tid} {ENTER}", _ctx())

    model.step_delay = 0.0
    model.reply("在。有什么要我看的?")
    tasks.tell(tid, "你好")
    live = _capture(2.0)

    assert "在。有什么要我看的?" in live, f"它的回答没打出来:{live!r}"
    assert f"{tid} >" in live, "该有个前缀让人分得清谁在说"
    cmd("/subtasks off", _ctx())


def test_a_suspended_subagent_says_what_it_is_waiting_for(model):
    """它挂起等回话时,接管的用户得看见它在等什么 —— 不然就是"它好像卡住了"。"""
    model.set(("写不进去。", [model.call("suspend", ask="要写 reports/ 的权限。")], ""))
    tid = tasks.dispatch("写点东西", fs=helpers.grant(), wait=False)["task_id"]
    helpers.wait_status(tid, "waiting_input")
    cmd(f"/subtasks {tid} {ENTER}", _ctx())

    out = cmd(f"/subtasks {tid}", _ctx())
    assert "要写 reports/ 的权限。" in out, "详情里得能看到它在等什么"
    cmd("/subtasks off", _ctx())


def test_the_prompt_comes_back_after_the_subagent_speaks(model, monkeypatch):
    """子 agent 说完一段之后,输入提示符要**自己回来**,不用用户敲回车去叫它。

    实测:它输出完了,`[t2] > ` 却不见了 —— 用户敲了个空行才看见它回来。根因是提示符
    只由主线程打**一次**(打完就在等输入),而子 agent 的输出是另一条线程直接写 stdout 的:
    糊在提示符后面,那一行提示符就此作废。
    """
    import sys
    import threading

    from rich.console import Console

    monkeypatch.setattr(tasks, "ATTACH_QUIET", 0.05)
    monkeypatch.setattr(cli, "_IDLE_POLL", 0.05)
    while not cli._INPUT.empty():          # 别的测试可能往队列里塞过行
        cli._INPUT.get_nowait()

    tid = _dispatch_talker(model)
    helpers.wait_status(tid, ("done", "failed", "truncated"))
    sink = tasks.get(tid).ctx.console.file    # 它的输出口(关掉之后这个对象还在)
    tasks.finish(tid, "accept")               # 关掉:它就不会来打断下面那次"等输入"
    tasks.attach(tid)

    buf, real = io.StringIO(), sys.stdout
    try:
        sys.stdout = buf
        sink.write("需要我做什么?直接说完整指令就行。\n")
        begin = buf.getvalue()
    finally:
        sys.stdout = real

    assert begin.startswith("\n"), "一段输出开始时该先换行,别糊在提示符后面"
    assert f"[{tid}] 需要我做什么?" in begin, "转发时要带前缀"

    assert tasks.attach_settle() is False, "刚说完就判安静 —— 提示符会插在它输出中间"
    time.sleep(0.1)
    assert tasks.attach_settle() is True, "安静下来了就该收尾"
    assert tasks.attach_settle() is False, "同一段只收尾一次(不然提示符会重复冒出来)"

    # 主线程在等输入的过程中,应该自己把提示符补回来
    out = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=out, width=110))
    sink.write("又说了一句。\n")
    time.sleep(0.1)                        # 让它安静下来
    th = threading.Thread(target=cli._read_multiline, args=(f"[{tid}] > ",), daemon=True)
    th.start()
    time.sleep(0.35)                       # 它每 0.05 秒醒一次,该补的早补了
    cli._INPUT.put("\n")                   # 提交,让它返回
    th.join(3)
    got = out.getvalue()
    assert got.count(f"[{tid}] > ") >= 2, f"提示符没补回来,用户得自己敲回车:{got!r}"


@pytest.mark.parametrize("word", ["off", "exit", "detach"])
def test_all_the_ways_to_leave(model, word):
    """用户记不住是哪个词 —— 常用的三个都认(他试错的成本是全程序退出)。"""
    tid = _dispatch_talker(model)
    helpers.wait_status(tid, ("done", "failed", "truncated"))
    cmd(f"/subtasks {tid} {ENTER}", _ctx())
    cmd(f"/subtasks {word}", _ctx())
    assert tasks.attached() is None, f"{word} 应该能退出接管"
