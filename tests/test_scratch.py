"""临时区:每块活自带一小块**永远可写**的草稿纸,中间产物有地方去。

**为什么非有不可**(用户报的现象):子 agent 的写范围常常只有 `reports/` 那么窄,而技能
脚本要产出原始输出、抽样本、临时 csv。没有指定的地方,它就会往"唯一能写的地方"倒 ——
实测一份 28KB 的 raw.json 落进了 `.pylibs`,和 45 个装好的包混在一起(那本来是容器里
唯一的可写口子,见 tools/container.py)。

设计上有三条是**故意**的,这一组测试就是钉住它们:

  · **单独一个字段、不并进 `write`** —— 并进去会让它参与"两个 agent 撞车"的判定,
    也会顺带把交付目录的删除权放开;
  · **按 agent 分开**(`.tmp/<任务号>/`)—— 几个子 agent 并排跑时,共用一块就等于
    把它们又放回"改同一批文件"的处境;
  · **删只在自己那一块里放开** —— 临时产物本来就该边跑边清,但绝不能碰到交付物。
"""
from __future__ import annotations

import os
import time

import helpers
import pytest
from agent import core, tasks
from agent.ctx import AgentCtx, FsGrant, use

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
    """按 agent 分开:`.tmp/t2/` 盖不到 `.tmp/t3/`,也盖不到 `.tmp/` 这个根。"""
    g = FsGrant(read=("*",), write=(), scratch=(".tmp/t2/",))
    assert g.allows(".tmp/t2/a/b.json", "write")
    assert not g.allows(".tmp/t3/raw.json", "write"), "别人的草稿纸"
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
    """主 agent 本来就哪儿都能写,给它指定一块是为了**别乱放** —— 不说的话全凭当时心情。"""
    assert FsGrant.for_main().scratch == (".tmp/main/",)
    assert AgentCtx(role="main").fs.scratch == (".tmp/main/",), "默认就是主 agent 的授权"
    # 子 agent 不用看这里的默认值:它的授权**必须**由 tasks.dispatch 显式给
    # (见 test_every_task_brings_its_own_scratch)—— 默认那份是给"没人在场的独立调用"的。


def test_the_two_definitions_of_the_main_scratch_agree():
    """`.tmp/main/` 在两处各写了一遍(`ctx.MAIN_SCRATCH` 是字面量,`core.scratch_scope`
    算出来的)—— ctx 要保持叶子模块不 import core,所以只能这样。**两边一致由这条钉住。**"""
    assert core.scratch_scope("main") == AgentCtx(role="main").fs.scratch[0]
    assert core.scratch_scope("t2") == ".tmp/t2/"


# ============================== 派活时自动带上 ==============================


def test_every_task_brings_its_own_scratch(model):
    model.reply("干完了。")
    tid = tasks.dispatch("随便一件活", fs=helpers.grant(write=["reports/"]),
                         wait=False)["task_id"]
    t = helpers.wait_status(tid, ("done", "failed"))

    assert t.fs.scratch == (f".tmp/{tid}/",), "每块活都该自带一块草稿纸"
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
        assert core.safe_path(f".tmp/{tid}/raw.json", "write").name == "raw.json"
        assert core.safe_path(f".tmp/{tid}/raw.json", "delete").name == "raw.json"
        assert core.safe_path("reports/a.md", "write").name == "a.md"
        with pytest.raises(PermissionError):
            core.safe_path("notes/a.md", "write")


def test_a_subagent_actually_writes_a_temp_file_end_to_end(model, workspace):
    """真跑一遍:它把中间产物写进草稿纸,文件**真出现在磁盘上**。"""
    assert tasks._new_id() == "t1", "这条测试假设任务号从头开始(autouse fixture 会清表)"
    model.set(("先落个中间产物。",
               [model.call("write_file", path=".tmp/t1/raw.json", content='{"a":1}')], ""),
              ("落好了。", [], ""))
    tid = tasks.dispatch("扫一遍", fs=helpers.grant(write=["reports/"]),
                         wait=False)["task_id"]
    assert tid == "t1"
    t = helpers.wait_status(tid, ("done", "failed"))
    assert t.status == "done", f"实际 {t.status},error={t.error!r}"

    assert (workspace / ".tmp" / "t1" / "raw.json").is_file(), "中间产物没落下去"
    assert not (workspace / ".pylibs").exists(), "不该再往 pip 的包目录里倒了"


def test_the_subagent_is_told_where_its_scratch_is(model):
    """**权限里给了还不够,得说清那是干什么用的、容器里叫什么** —— 不说它就不知道能用。"""
    model.reply("干完了。")
    tid = tasks.dispatch("干活", fs=helpers.grant(write=["reports/"]),
                         wait=False)["task_id"]
    t = helpers.wait_status(tid, ("done", "failed"))

    prompt = t.messages[0]["content"]
    # 断言整句,而不是"里面有没有这几个字符":容器路径 `/workspace/.tmp/t1/` 天然包含
    # `.tmp/t1/`,光比对子串的话**宿主那条不写也照样通过**(实测漏过一次)。
    assert f"中间产物有指定的地方:`.tmp/{tid}/`**" in prompt, "没告诉它草稿纸在哪儿"
    assert f"/workspace/.tmp/{tid}/" in prompt, "容器里同一个位置,不说它就只会写 /tmp"
    assert "别把产物放进去" in prompt, "pip 的包目录那条也得说清"


def test_the_details_show_the_scratch(model):
    """`/subtasks <id>` 是给人看的 —— 临时区在哪儿得看得见(要找它产出的中间文件时)。"""
    model.reply("干完了。")
    tid = tasks.dispatch("干活", fs=helpers.grant(), wait=False)["task_id"]
    helpers.wait_status(tid, ("done", "failed"))

    out = tasks.show(tid)

    assert "临时区" in out and f".tmp/{tid}/" in out


# ============================== 存下来、读回来 ==============================


def test_the_scratch_survives_a_restart(model):
    """重启后从盘上读回来的任务**还得有自己的草稿纸** —— 漏了的话,续跑时写它自己那块
    会被拒,而它上一步明明刚往那儿写过(现象是"接着干就莫名没权限了")。"""
    model.reply("干完了。")
    tid = tasks.dispatch("干活", fs=helpers.grant(), wait=False)["task_id"]
    helpers.wait_status(tid, ("done", "failed"))

    tasks._TASKS.clear()                       # 模拟:进程重启,内存里的都没了
    t = tasks.get(tid)

    assert t.fs.scratch == (f".tmp/{tid}/",)


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

    assert tasks.get("t8").fs.scratch == (".tmp/t8/",)
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
    old = core.scratch_dir("t1")
    fresh = core.scratch_dir("t2")
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
    d = core.scratch_dir("t1")
    d.mkdir(parents=True)
    assert core.purge_scratch(0) == ""
    assert d.exists(), "0 天 = 关掉清理,不能变成清空全部"
