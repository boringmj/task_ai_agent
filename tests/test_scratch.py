"""临时区:每块活自带一小块**永远可写**的草稿纸,中间产物有地方去。

**为什么非有不可**(用户报的现象):子 agent 的写范围常常只有 `reports/` 那么窄,而技能
脚本要产出原始输出、抽样本、临时 csv。没有指定的地方,它就会往"唯一能写的地方"倒 ——
实测一份 28KB 的 raw.json 落进了 `.pylibs`,和 45 个装好的包混在一起(那本来是容器里
唯一的可写口子,见 tools/container.py)。

设计上有三条是**故意**的,这一组测试就是钉住它们:

  · **单独一个字段、不并进 `write`** —— 并进去会让它参与"两个 agent 撞车"的判定,
    也会顺带把交付目录的删除权放开;
  · **按"会话 + 谁"分开**(`.tmp/<会话>/<任务号>/`)—— 几个子 agent 并排跑时,共用一块
    就等于把它们又放回"改同一批文件"的处境;**会话那一层不能省**:任务号是每个进程各自
    从 t1 数起的,多会话共用工作区时两个 t1 会撞在同一个目录上(必然是撞,不是偶发);
  · **删只在自己那一块里放开** —— 临时产物本来就该边跑边清,但绝不能碰到交付物。
"""
from __future__ import annotations

import os
import time

import helpers
import pytest
from agent import core, tasks
from agent.ctx import AgentCtx, FsGrant, use


def _scope(tid: str, sid: str | None = None) -> str:
    """这个任务在某个会话下的临时区写法(路径里带会话 id,见 core.scratch_scope)。"""
    return core.scratch_scope(sid or tasks._sid(), tid)

# ============================== 权限语义 ==============================


def test_the_scratch_is_writable_even_with_no_write_scope():
    """写范围是空的(一个文件都改不了),草稿纸照样能写 —— 这就是它存在的意义。"""
    g = FsGrant(read=("*",), write=(), scratch=(".tmp/t2/",))
    assert g.allows(".tmp/t2/raw.json", "write")
    assert not g.allows("reports/a.md", "write"), "别的地方一个也不许"


def test_the_scratch_is_deletable_but_only_inside_itself():
    """临时产物该边跑边清,所以要能删 —— 但只在它自己那一块里。

    别处照旧是"开关 + 范围"两份都成立(见 test_permissions.py)。
    """
    g = FsGrant(read=("*",), write=("reports/",), delete=False, scratch=(".tmp/t2/",))
    assert g.allows(".tmp/t2/old.json", "delete")
    assert not g.allows("reports/a.md", "delete"), "为了一块草稿纸,不能把交付目录变成可删的"


def test_the_scratch_does_not_swallow_a_sibling_agent():
    """按"会话 + 谁"分开:`.tmp/s1/t2/` 盖不到同会话的 `.tmp/s1/t3/`,
    **更盖不到另一个会话里那个同号的 `.tmp/s2/t2/`** —— 后者正是多会话共用工作区时会撞的那个。"""
    g = FsGrant(read=("*",), write=(), scratch=(".tmp/s1/t2/",))
    assert g.allows(".tmp/s1/t2/a/b.json", "write")
    assert not g.allows(".tmp/s1/t3/raw.json", "write"), "同一个会话里别人的草稿纸"
    assert not g.allows(".tmp/s2/t2/raw.json", "write"), "另一个会话里同号的那个"
    assert not g.allows(".tmp/s1/main/x.json", "write"), "主 agent 那块也不归它"
    assert not g.allows(".tmp", "write")
    assert not g.allows(".tmpx/a.json", "write")


def test_a_refused_write_points_at_the_scratch():
    """**被拒的时候正急着找地方写** —— 那一句话里如果没有草稿纸,它就会去别处翻。

    (实测:翻到了 .pylibs。)所以解释里必须先给路,再谈"要更多权限就 suspend 申请"。
    """
    why = FsGrant(read=("*",), write=("reports/",), scratch=(".tmp/t7/",)).explain(
        "notes/a.md", "write")
    assert ".tmp/t7/" in why and "草稿纸" in why
    assert why.index(".tmp/t7/") < why.index("suspend"), "先说写哪儿,再说怎么申请"

    no_scratch = FsGrant(read=("*",), write=("reports/",)).explain("notes/a.md", "write")
    assert "草稿纸" not in no_scratch, "没有草稿纸就不能这么说"


def test_the_main_agent_has_its_own_named_scratch():
    """主 agent 本来就哪儿都能写,给它指定一块是为了**别乱放** —— 不说的话全凭当时心情。

    路径**由调用方算**(不带默认值):它跟着当前会话走,而"当前会话"是会变的(/switch)。
    给个默认值就等于把"哪会儿算出来的"藏起来,那种 bug 最难查。
    """
    assert FsGrant.for_main(".tmp/s1/main/").scratch == (".tmp/s1/main/",)
    assert AgentCtx(role="main").fs.scratch == (), \
        "AgentCtx 的默认授权不指定临时区 —— 主 agent 那份由 cli 显式设上(那时才解析得出会话)"


def test_the_scratch_is_scoped_by_session():
    """**这是多会话共用工作区时唯一站得住的分法**:任务号是每个进程各自从 t1 数起的,
    两个会话各自派的第一个活都是 t1 —— 只按任务号分,它们就是同一个目录。"""
    assert core.scratch_scope("s1", "t1") == ".tmp/s1/t1/"
    assert core.scratch_scope("s2", "t1") == ".tmp/s2/t1/"
    assert core.scratch_scope("s1", "t1") != core.scratch_scope("s2", "t1")
    assert core.scratch_scope("s1", "main") == ".tmp/s1/main/"


@pytest.mark.parametrize("bad", ["../../etc", "a/b", "a\\b", "", "."])
def test_a_session_id_cannot_climb_out_of_the_tmp_dir(bad):
    """会话 id 正常是 12 位 hex,但 meta.json 是**可以被人手改的** —— 这条是防御性的:
    不管里面写了什么,拼出来的路径都落在 `.tmp/` 底下。"""
    scope = core.scratch_scope(bad, "t1")
    assert scope.startswith(".tmp/") and ".." not in scope
    assert core.scratch_dir(bad, "t1").is_relative_to(core.SCRATCH_DIR)


# ============================== 派活时自动带上 ==============================


def test_every_task_brings_its_own_scratch(model):
    model.reply("干完了。")
    tid = tasks.dispatch("随便一件活", fs=helpers.grant(write=["reports/"]),
                         wait=False)["task_id"]
    t = helpers.wait_status(tid, ("done", "failed"))

    assert t.fs.scratch == (_scope(tid),), "每块活都该自带一块草稿纸"
    assert t.fs.write == ("reports/",), \
        "草稿纸**不并进 write** —— 并进去会连撞车判定一起卷进来"


def test_the_scratch_does_not_widen_the_write_scope(model):
    """派活时给的范围原样保留 —— 草稿纸是**加**一块,不是改写了写范围。"""
    model.reply("干完了。")
    tid = tasks.dispatch("写报告", fs=helpers.grant(write=["reports/"]),
                         wait=False)["task_id"]
    t = helpers.wait_status(tid, ("done", "failed"))
    c = AgentCtx(role="sub", task_id=tid, fs=t.fs)

    with use(c):
        assert core.safe_path(_scope(tid) + "raw.json", "write").name == "raw.json"
        assert core.safe_path(_scope(tid) + "raw.json", "delete").name == "raw.json"
        assert core.safe_path("reports/a.md", "write").name == "a.md"
        with pytest.raises(PermissionError):
            core.safe_path("notes/a.md", "write")


def test_a_subagent_actually_writes_a_temp_file_end_to_end(model, workspace):
    """真跑一遍:它把中间产物写进草稿纸,文件**真出现在磁盘上**。"""
    assert tasks._new_id() == "t1", "这条测试假设任务号从头开始(autouse fixture 会清表)"
    target = _scope("t1") + "raw.json"          # 路径里带会话 id,派活前就能算出来
    model.set(("先落个中间产物。",
               [model.call("write_file", path=target, content='{"a":1}')], ""),
              ("落好了。", [], ""))
    tid = tasks.dispatch("扫一遍", fs=helpers.grant(write=["reports/"]),
                         wait=False)["task_id"]
    assert tid == "t1"
    t = helpers.wait_status(tid, ("done", "failed"))
    assert t.status == "done", f"实际 {t.status},error={t.error!r}"

    assert (workspace / _scope("t1") / "raw.json").is_file(), "中间产物没落下去"
    assert not (workspace / ".pylibs").exists(), "不该再往 pip 的包目录里倒了"


def test_the_subagent_is_told_where_its_scratch_is(model):
    """**权限里给了还不够,得说清那是干什么用的、容器里叫什么** —— 不说它就不知道能用。"""
    model.reply("干完了。")
    tid = tasks.dispatch("干活", fs=helpers.grant(write=["reports/"]),
                         wait=False)["task_id"]
    t = helpers.wait_status(tid, ("done", "failed"))

    prompt = "\n".join(m["content"] for m in t.messages if m.get("role") == "system")
    assert _scope(tid) in prompt, "没告诉它草稿纸在哪儿"
    assert f"/workspace/{_scope(tid)}" in prompt, "容器里同一个位置,不说它就只会写 /tmp"
    assert ".pylibs" in prompt, "pip 的包目录那条也得说清"


def test_two_subagents_get_the_same_system_prompt(model):
    """**这是省钱的要害**:两条子 agent 的系统提示词必须**逐字相同**。

    缓存按前缀命中,提示词里只要混进一个每个任务都不一样的东西(任务描述、带任务号的
    临时区路径),从那一行起后面全部重新计费。实测代价:原先 `{task}` 在第 8 行、技能
    清单在第 83 行,于是整份 3257 token 里有 3110(95%)**永远共享不到** —— 每起一个
    子 agent 白付一遍全价。

    所以这条钉的不是"内容对不对",而是"**有没有人在共用文本里塞了可变的东西**"。
    以后加变量时它会立刻变红 —— 比账单上发现便宜得多。
    """
    model.reply("干完了。")
    a = tasks.dispatch("第一件活", fs=helpers.grant(), wait=False)["task_id"]
    b = tasks.dispatch("另一件完全不同的活", fs=helpers.grant(), wait=False)["task_id"]
    helpers.wait_status(a, ("done", "failed"))
    helpers.wait_status(b, ("done", "failed"))

    shared_a = tasks.get(a).messages[0]["content"]
    shared_b = tasks.get(b).messages[0]["content"]

    assert shared_a == shared_b, "两条子 agent 的系统提示词不一样 —— 前缀缓存全废"
    assert a != b, "前提:两个任务号不同"
    # 可变的东西一个都不许留在里面
    assert "第一件活" not in shared_a, "任务描述混进共用文本了"
    assert _scope(a) not in shared_a and ".tmp" not in shared_a, "带任务号的路径混进来了"
    assert "临时区是" not in shared_a, "临时区那条要单独成一条(它每个任务都不同)"


def test_the_task_is_sent_once_not_twice(model):
    """任务描述**只发一遍** —— 它是第一条 user 消息(见 loop_run)。"""
    model.reply("干完了。")
    tid = tasks.dispatch("把这件事做了", fs=helpers.grant(), wait=False)["task_id"]
    t = helpers.wait_status(tid, ("done", "failed"))

    sent = " ".join(m["content"] for m in t.messages
                    if isinstance(m.get("content"), str))
    assert sent.count("把这件事做了") == 1, f"任务描述发了两遍:{sent.count('把这件事做了')}"
    first_user = next(m for m in t.messages if m.get("role") == "user")
    assert first_user["content"] == "把这件事做了", "它该是第一条 user 消息"


def test_the_details_show_the_scratch(model):
    """`/subtasks <id>` 是给人看的 —— 临时区在哪儿得看得见(要找它产出的中间文件时)。"""
    model.reply("干完了。")
    tid = tasks.dispatch("干活", fs=helpers.grant(), wait=False)["task_id"]
    helpers.wait_status(tid, ("done", "failed"))

    out = tasks.show(tid)

    assert "临时区" in out and _scope(tid) in out


# ============================== 多会话共用一个工作区 ==============================


def test_two_sessions_never_share_a_scratch(model, monkeypatch):
    """**用户报的漏洞**:多个会话共用一个工作区时,`.tmp/<任务号>/` 会撞。

    撞的方式是**必然**的,不是偶发:任务号由每个进程各自从 t1 数起(`_new_id`),
    两个会话派的第一块活都是 t1。这条测试就照这个来 —— 清空任务表模拟第二个进程,
    于是两边都拿到 t1,再看它们的草稿纸是不是同一个。
    """
    model.reply("干完了。")
    first = tasks.dispatch("会话 A 的活", fs=helpers.grant(write=["reports/"]),
                           wait=False)["task_id"]
    helpers.wait_status(first, ("done", "failed"))
    scope_a = tasks.get(first).fs.scratch[0]

    tasks._TASKS.clear()                       # 模拟:另一个终端/进程,另一个会话
    monkeypatch.setattr(tasks, "_sid", lambda: "另一个会话的id")
    second = tasks.dispatch("会话 B 的活", fs=helpers.grant(write=["reports/"]),
                            wait=False)["task_id"]
    helpers.wait_status(second, ("done", "failed"))
    scope_b = tasks.get(second).fs.scratch[0]

    assert first == second == "t1", "前提:两边都是各自的第一个活(所以才会撞)"
    assert scope_a != scope_b, f"两个会话的草稿纸撞在一起了:{scope_a}"
    assert ".tmp/另一个会话的id/t1/" == scope_b


def test_a_task_from_another_session_is_not_writable(model, monkeypatch):
    """不只是"目录不同",**权限上也得真的隔开** —— 否则另一个会话能改它的中间产物。"""
    model.reply("干完了。")
    tid = tasks.dispatch("本会话的活", fs=helpers.grant(), wait=False)["task_id"]
    t = helpers.wait_status(tid, ("done", "failed"))

    theirs = core.scratch_scope("别的会话", "t1")
    with use(AgentCtx(role="sub", task_id=tid, fs=t.fs)):
        with pytest.raises(PermissionError):
            core.safe_path(theirs + "raw.json", "write")


def test_the_main_scratch_follows_the_session(monkeypatch):
    """主 agent 的草稿纸**跟着会话走**。它是在 cli 启动时算一次的 —— 换了会话不重算的话,
    它还指着上一段的目录:路径照样能写,**只是写进了别人的地盘**(静默的那种错)。"""
    from agent import cli, session as store

    monkeypatch.setattr(store, "SESSIONS_DIR", cli.ROOT.parent / "sessions-x")
    monkeypatch.setattr(store, "_current_session", None)
    monkeypatch.setattr(store, "_resolve_note", "")

    assert cli._main_grant().scratch == (core.scratch_scope(store.current_session_id(),
                                                            "main"),)
    old = store.current_session_id()
    store.set_current_session("另一个会话")
    assert cli._main_grant().scratch == (".tmp/另一个会话/main/",)
    assert old not in cli._main_grant().scratch[0]


def test_switching_sessions_actually_moves_it(monkeypatch):
    """**真正会坏的那条路**:`/switch` 之后主 agent 那份授权得跟着换。

    它是启动时算一次的(见 cli._main_grant)—— 切走之后不换,它还指着上一段的目录。
    而"当前会话"是会变的、`/switch` 是**命令行**触发的,所以要有一条走命令层的测试,
    光测那个函数算不对是看不出来的。
    """
    from agent import ctx as agent_ctx
    from agent import session as store
    from agent.commands import Context, dispatch as cmd
    from agent.tools import vm as vm_tools

    monkeypatch.setattr(store, "_current_session", None)
    monkeypatch.setattr(store, "_resolve_note", "")
    monkeypatch.setattr(vm_tools, "VM_AUTOSTART", False)   # 别在测试里真去起虚拟机

    start = core.scratch_scope(store.current_session_id(), "main")
    ctx_ = Context(messages=[{"role": "system", "content": "x"}])
    with use(AgentCtx(role="main", fs=FsGrant.for_main(start))):
        cmd("/switch new", ctx_)

        now = store.current_session_id()
        assert now != start.split("/")[1], "前提:确实换了一个会话"
        assert agent_ctx.current().fs.scratch == (core.scratch_scope(now, "main"),), \
            "切了会话,主 agent 的草稿纸还指着上一个"
        # 提示词那一头也要跟上 —— 授权换了但提示词还说旧的,它就照旧往旧目录写
        notice = ctx_.messages[-1]["content"]
        assert core.scratch_scope(now, "main") in notice, "切换提示里也要说清新的是哪块"


# ============================== 存下来、读回来 ==============================


def test_the_scratch_survives_a_restart(model):
    """重启后从盘上读回来的任务**还得有自己的草稿纸** —— 漏了的话,续跑时写它自己那块
    会被拒,而它上一步明明刚往那儿写过(现象是"接着干就莫名没权限了")。"""
    model.reply("干完了。")
    tid = tasks.dispatch("干活", fs=helpers.grant(), wait=False)["task_id"]
    helpers.wait_status(tid, ("done", "failed"))

    tasks._TASKS.clear()                       # 模拟:进程重启,内存里的都没了
    t = tasks.get(tid)

    assert t.fs.scratch == (_scope(tid),)


def test_an_old_task_without_a_scratch_gets_one_on_load(model):
    """老数据(meta 里没有 scratch 这个字段)读回来要按规矩补一块,而不是留空。"""
    from agent import session as _session

    sid = tasks._sid()
    old = tasks.Task(id="t8", prompt="上个版本留下的活", session=sid, status="done")
    tasks._save(old)
    meta = tasks.task_dir("t8") / "meta.json"
    raw = meta.read_text(encoding="utf-8").replace('"scratch": [],', "")
    meta.write_text(raw, encoding="utf-8")
    tasks._TASKS.clear()

    assert tasks.get("t8").fs.scratch == (_scope("t8"),)
    assert _session  # (留着这个引用,免得 pyflakes 说未使用)


# ============================== 容器挂载 ==============================


def test_the_container_mounts_the_scratch_writable(workspace):
    """**这才是 raw.json 倒进 .pylibs 的直接原因**:容器里工作区是按授权挂的,受限的
    子 agent 除了 .pylibs 一处可写之外无处可写。草稿纸必须一起挂成可写。"""
    from agent.tools.container import _workspace_mounts

    fs = helpers.grant(write=["reports/"]).__class__(
        read=("*",), write=("reports/",), scratch=(".tmp/t2/",))
    with use(AgentCtx(role="sub", task_id="t2", fs=fs)):
        mounts = _workspace_mounts()

    joined = " ".join(mounts)
    assert "/workspace:ro" in joined, "底还是只读"
    assert f"{workspace / 'reports'}:/workspace/reports" in joined
    assert f"{workspace / '.tmp' / 't2'}:/workspace/.tmp/t2" in joined, \
        "草稿纸没挂进去 —— 那它还是只能在 .pylibs 里写"


def test_git_ignores_the_scratch(workspace):
    """草稿纸不该进版本管理:它天天变,进去会把真正的改动淹掉。"""
    from agent.tools.git import _git_ensure_repo, _git_run

    if not _git_run("--version").returncode == 0:
        pytest.skip("这个环境没有 git")
    (workspace / ".tmp" / "t1").mkdir(parents=True)
    (workspace / ".tmp" / "t1" / "raw.json").write_text("{}", encoding="utf-8")
    _git_ensure_repo()

    out = _git_run("status", "--porcelain", "--untracked-files=all").stdout

    assert ".tmp" not in out, f"临时区进了 git 的视野:{out}"


# ============================== 清理 ==============================

def test_only_the_stale_scratch_areas_are_purged(monkeypatch, tmp_path):
    """按天清、不立刻删:出了事要回头看当时产出的中间文件,那正是排错时最想要的。"""
    monkeypatch.setattr(core, "SCRATCH_DIR", tmp_path / ".tmp")
    old = core.scratch_dir("sess-a", "t1")
    fresh = core.scratch_dir("sess-a", "t2")
    for d in (old, fresh):
        d.mkdir(parents=True)
        (d / "raw.json").write_text("{}", encoding="utf-8")
    stale = time.time() - 9 * 86400                # 9 天没动过
    os.utime(old, (stale, stale))

    note = core.purge_scratch(7)

    assert not old.exists(), "过期的那块该清掉"
    assert fresh.exists() and (fresh / "raw.json").is_file(), "还在干活的那块不能动"
    assert "1 个" in note


def test_purging_is_off_when_the_age_is_zero(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "SCRATCH_DIR", tmp_path / ".tmp")
    d = core.scratch_dir("sess-a", "t1")
    d.mkdir(parents=True)
    assert core.purge_scratch(0) == ""
    assert d.exists(), "0 天 = 关掉清理,不能变成清空全部"
