"""会话持久化:历史怎么落盘、读回来、崩了怎么办、两个进程别抢同一个会话。

**为什么单开一组**:这一层出错的表现是**数据没了或串了**,而不是报错。

  · 半行 JSON(崩在写的中途)→ 整个文件读不出来?不,一行一行读,坏的跳过就行;
  · 悬空的工具调用 → 直接发给 API 是 **400**,而用户只看到"Agent 出错了";
  · 两个进程共用一个会话 → 两边互相覆盖历史、VM 磁盘也互踩,而且**当场不报错**;
  · 过期提示词冻在会话里 → 改了 system.md 之后老会话还用着旧的,查半天查不出来。

会话目录指到本测试专属的临时目录:索引(哪个工作区有哪些会话)是**全局一份**,
不隔离的话测试之间会互相看见对方建的会话。
"""
from __future__ import annotations

import json
import os

import pytest
from agent import session


@pytest.fixture
def store(tmp_path, monkeypatch):
    """把会话目录/索引指到本测试专属的临时目录,并让"当前会话"回到未解析状态。"""
    monkeypatch.setattr(session, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(session, "INDEX_FILE", tmp_path / "sessions" / "index.json")
    monkeypatch.setattr(session, "_current_session", None)
    monkeypatch.setattr(session, "_resolve_note", "")
    return session


def _lines(sid: str) -> list[str]:
    return session.session_file(sid).read_text(encoding="utf-8").splitlines()


def _write_raw(sid: str, text: str) -> None:
    session.session_dir(sid).mkdir(parents=True, exist_ok=True)
    with session.session_file(sid).open("a", encoding="utf-8") as fh:
        fh.write(text)


# ============================== 存与读 ==============================

def test_the_history_round_trips(store):
    session.append_messages([{"role": "user", "content": "你好"}], "s1")
    session.append_messages([{"role": "assistant", "content": "在"}], "s1")

    msgs, note = session.load_session("s1")

    assert [m["content"] for m in msgs] == ["你好", "在"]
    assert "已恢复 2 条" in note
    assert json.loads(_lines("s1")[0])["_meta"]["version"] == 1, "第一行是元信息,不是对话"


def test_the_fresh_system_prompt_wins_over_the_stored_one(store):
    """开头的 system 不落盘、读回来也不返回。

    存下来的话,改了 `prompts/system.md` 之后老会话会**继续用旧的那份**,而且看不出
    为什么 —— 提示词是每次启动重新生成的,不是历史的一部分。
    """
    session.append_messages([
        {"role": "system", "content": "旧的系统提示词"},
        {"role": "system", "content": "旧的长期记忆"},
        {"role": "user", "content": "问题"},
    ], "s1")

    msgs, _ = session.load_session("s1")

    assert [m["content"] for m in msgs] == ["问题"]


def test_a_system_message_in_the_middle_is_kept(store):
    """**中段**的 system 要留下 —— 那不是每次重生的模板,是程序当时真的插进对话的内容
    (比如"本次会话是恢复的"那条提示)。用"一律不存 system"处理就会把它丢掉。"""
    session.append_messages([
        {"role": "system", "content": "系统提示词"},
        {"role": "user", "content": "问题"},
        {"role": "system", "content": "本次会话是恢复的"},
        {"role": "assistant", "content": "答"},
    ], "s1")

    msgs, _ = session.load_session("s1")

    assert [m["content"] for m in msgs] == ["问题", "本次会话是恢复的", "答"]


def test_a_half_written_line_is_skipped_not_fatal(store):
    """崩在写的中途 → 最后一行是半截 JSON。**整份历史不该因此读不出来。**"""
    session.append_messages([{"role": "user", "content": "第一句"}], "s1")
    _write_raw("s1", '{"role": "user", "content": "半截')

    msgs, note = session.load_session("s1")

    assert [m["content"] for m in msgs] == ["第一句"]
    assert "损坏" in note, f"跳过了坏行得说一声:{note}"


def test_loading_a_session_that_never_existed(store):
    msgs, note = session.load_session("没有这个会话")
    assert msgs == [] and note == "无历史会话"


def test_rewriting_replaces_the_whole_history(store):
    """压缩之后历史被换掉,只能整体重写 —— 而且**先写临时文件再原子替换**:
    写一半崩掉的话,原来那份还在。"""
    session.append_messages([{"role": "user", "content": "很长的旧对话"}], "s1")
    session.rewrite_session([{"role": "system", "content": "x"},
                             {"role": "user", "content": "摘要"}], "s1")

    msgs, _ = session.load_session("s1")

    assert [m["content"] for m in msgs] == ["摘要"]
    assert not session.session_file("s1").with_suffix(".jsonl.tmp").exists(), "临时文件该清掉"


def test_clear_only_drops_the_history(store):
    """清历史 ≠ 删会话:虚拟机磁盘就在同一个目录里,不能跟着一起没了。"""
    session.append_messages([{"role": "user", "content": "话"}], "s1")
    (session.session_dir("s1") / "work-s1.qcow2").write_bytes(b"disk")

    session.clear_session("s1")

    assert session.load_session("s1")[0] == []
    assert (session.session_dir("s1") / "work-s1.qcow2").exists()


# ============================== 崩了之后 ==============================

def test_a_dangling_tool_call_gets_an_explanation_not_a_fake_result():
    """模型发了 tool_calls、结果还没写回来就被打断 —— 这种历史发给 API **直接 400**。

    对策是**补一条说明**而不是整轮回滚:回滚会把用户等了半天的思考和已经跑完的工具调用
    全丢掉。但补的必须说"未知",**不能编一个成功** —— 编了它会以为自己做过了。
    """
    messages = [
        {"role": "user", "content": "跑一下"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "vm_run", "arguments": "{}"}}]},
    ]

    added = session.repair_dangling(messages)

    assert added == 1
    assert messages[-1]["role"] == "tool"
    assert messages[-1]["tool_call_id"] == "c1"
    assert "未知" in messages[-1]["content"], "不许把没跑完的说成跑完了"


def test_only_the_missing_results_are_filled_and_they_go_after_the_real_ones():
    """补的位置也要对:工具结果的顺序该和 tool_calls 一致,插在前面会把真的挤到后面。"""
    messages = [
        {"role": "user", "content": "两件事"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "b", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "第一件的结果"},
    ]

    added = session.repair_dangling(messages)

    assert added == 1
    ids = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
    assert ids == ["c1", "c2"], f"顺序乱了:{ids}"
    assert messages[2]["content"] == "第一件的结果", "已有的结果不该被动"


def test_a_complete_step_is_left_alone():
    messages = [
        {"role": "user", "content": "跑"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "a", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "好了"},
    ]
    before = [dict(m) for m in messages]

    assert session.repair_dangling(messages) == 0
    assert messages == before


def test_an_incomplete_tail_is_cut_off_when_a_session_is_loaded(store):
    """主会话走的是**截断**这条路(子 agent 的任务走 repair)——
    截掉的是"最后那步没结果"的尾巴,前面完整的部分照留。"""
    session.append_messages([
        {"role": "user", "content": "第一句"},
        {"role": "assistant", "content": "第一答"},
        {"role": "user", "content": "第二句"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c9", "type": "function", "function": {"name": "a", "arguments": "{}"}}]},
    ], "s1")

    msgs, note = session.load_session("s1")

    assert [m["content"] for m in msgs] == ["第一句", "第一答", "第二句"]
    assert "丢弃了 1 条" in note


# ============================== 谁在占这个会话 ==============================

def test_a_session_id_cannot_climb_out_of_the_sessions_dir(store):
    assert session.session_dir("../../evil") == session.SESSIONS_DIR / "evil"
    assert session.session_dir("") == session.SESSIONS_DIR / "invalid"
    assert session.session_dir("a/b\\c") == session.SESSIONS_DIR / "abc"


def test_pid_alive_does_not_reach_for_a_gun(store):
    """Windows 上**不能**用 `os.kill(pid, 0)` 判活 —— 那会真的去杀进程。"""
    assert session.pid_alive(os.getpid()) is True
    assert session.pid_alive(2 ** 22) is False, "一个不存在的 pid 该判成死的"
    assert session.pid_alive(0) is False


def test_it_does_not_steal_a_session_from_a_live_agent(store, monkeypatch):
    """**实测踩到**:同工作区开第二个 agent,它直接接管了第一个**正在用**的会话 ——
    两个进程共用一个历史、一块虚拟机磁盘,互相覆盖,而当场什么错都不报。
    """
    session.register_session("busy1")                      # 索引里的 last 指向它
    session.session_dir("busy1").mkdir(parents=True)
    session.owner_file("busy1").write_text(json.dumps({"pid": 424242}), encoding="utf-8")
    monkeypatch.setattr(session, "pid_alive", lambda pid: pid == 424242)

    session._resolve_session()

    assert session.current_session_id() != "busy1", "抢了别人正在用的会话"
    assert "占用" in session.session_note() and "另开" in session.session_note()


def test_a_dead_agents_marker_is_stale_and_gets_taken_over(store, monkeypatch):
    """上次被强杀留下的占用标记要能清掉 —— 否则用户再也接不回自己的会话。"""
    session.register_session("old1")
    session.session_dir("old1").mkdir(parents=True)
    session.owner_file("old1").write_text(json.dumps({"pid": 424242}), encoding="utf-8")
    monkeypatch.setattr(session, "pid_alive", lambda pid: False)

    session._resolve_session()

    assert session.current_session_id() == "old1", "陈迹该让出来"
    assert session.session_note() == "继续上次会话"
    assert json.loads(session.owner_file("old1").read_text(encoding="utf-8"))["pid"] \
        == os.getpid(), "认领之后标记得换成自己的"


def test_release_only_removes_your_own_marker(store):
    session.session_dir("s1").mkdir(parents=True)
    session.owner_file("s1").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    session.release_owner("s1")
    assert not session.owner_file("s1").exists()

    session.owner_file("s1").write_text(json.dumps({"pid": 424242}), encoding="utf-8")
    session.release_owner("s1")
    assert session.owner_file("s1").exists(), "别把别人的占用标记顺手删了"


# ============================== 索引 ==============================

def test_the_index_lists_sessions_newest_first(store):
    index = session._index_load()
    index["workspaces"][session._workspace_key()] = {"last": "old", "sessions": {
        "old": {"created": "2026-01-01T00:00:00", "last_used": "2026-01-01T00:00:00"},
        "new": {"created": "2026-02-02T00:00:00", "last_used": "2026-02-02T00:00:00"},
    }}
    session._index_save(index)

    out = session.list_sessions()

    assert [s["id"] for s in out] == ["new", "old"]
    assert "T" not in out[0]["last_used"], "给人看的时间不该带 ISO 的 T"
    assert all(s["missing"] for s in out), "目录还没建出来,该标成已丢失"


def test_a_broken_index_does_not_block_startup(store):
    """索引坏了就当空的 —— 一个坏文件不该让程序起不来(会话本身还在盘上)。"""
    session.INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    session.INDEX_FILE.write_text("{ 这不是 json", encoding="utf-8")

    assert session._index_load()["workspaces"] == {}

    session.register_session("recovered")
    assert [s["id"] for s in session.list_sessions()] == ["recovered"]


def test_registering_twice_is_harmless(store):
    session.register_session("s1")
    session.register_session("s1")
    assert [s["id"] for s in session.list_sessions()] == ["s1"]


# ============================== 程序插的话 vs 用户打的字 ==============================

def test_program_inserted_messages_are_recognizable():
    """压缩摘要、子 agent 的通报**必须以 user 身份**进对话(别的角色模型不重视),
    可那样一来重放时就和用户亲手打的字长得一模一样 —— 屏幕上冒出一行"你 > (以上对话
    已压缩…",像是用户自己说的。所以要有个**可判的标记**,而不是靠人去比对措辞。
    """
    assert session.is_system_message(
        {"role": "user", "content": session.SYSTEM_TAG + " (以上对话已压缩…)"})
    assert session.is_system_message(
        {"role": "user", "content": session.SYSTEM_TAG + "[子 agent t1 完成]"})

    assert not session.is_system_message({"role": "user", "content": "我自己说的话"})
    assert not session.is_system_message(
        {"role": "assistant", "content": session.SYSTEM_TAG + " 我在回答"})
    assert not session.is_system_message({"role": "user", "content": None})
