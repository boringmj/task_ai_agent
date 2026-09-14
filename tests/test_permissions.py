"""权限的地基:`FsGrant` 的范围语义 + `safe_path` 的逃逸校验。

**为什么这组最该有**:子 agent 并排跑这件事能成立,全靠"范围划得开"这一个假设。
而这里的失败**全是静默的** —— 多给了一块范围不会报错,只会让两个 agent 改同一批文件,
事后对不上账;少给了一块也不报错,只会让活干不成。两头都不出声,所以只能靠测试盯。

范围语义只有一条规则,但它反直觉,而且判错方向就是多给了权限:

    带 `/` 是**目录**(整个子树),不带是**文件**(只有它自己)——**不看磁盘上是什么**。
"""
from __future__ import annotations

import inspect
import socket

import helpers
import pytest
from agent import core
from agent.ctx import AgentCtx, FS_ANY, FsGrant, use

# ============================== 范围语义 ==============================


def test_a_file_scope_covers_only_that_one_file():
    g = FsGrant(write=("notes/plan.md",))
    assert g.allows("notes/plan.md", "write")
    assert not g.allows("notes/plan.md.bak", "write"), "不带斜杠是文件,不能连 .bak 一起给"
    assert not g.allows("notes/other.md", "write")


def test_a_directory_scope_covers_the_whole_subtree():
    g = FsGrant(write=("reports/",))
    assert g.allows("reports/a.md", "write")
    assert g.allows("reports/deep/b.md", "write")
    assert g.allows("reports", "write"), "目录自己也该算在里面"
    assert not g.allows("reportsx/a.md", "write"), "前缀得是整个路径段,不是字符串前缀"


def test_the_slash_decides_not_the_disk():
    """**判成目录是放宽、判成文件是收紧 —— 凭猜的话猜错方向就是多给了权限。**

    所以范围是什么,由末尾那个 `/` 说了算:磁盘上真有个叫 `plan.md` 的**目录**,
    不带斜杠的范围照样只当它是个文件。
    """
    (helpers.WS / "notes" / "plan.md").mkdir(parents=True)
    (helpers.WS / "notes" / "plan.md" / "inner.txt").write_text("x", encoding="utf-8")

    g = FsGrant(write=("notes/plan.md",))
    assert g.allows("notes/plan.md", "write")
    assert not g.allows("notes/plan.md/inner.txt", "write"), "它是个目录也不行 —— 范围写的是文件"


def test_read_and_write_are_two_grants():
    g = FsGrant(read=(FS_ANY,), write=("reports/",))
    assert g.allows("anything/else.md", "read")
    assert not g.allows("notes/a.md", "write")


def test_delete_needs_both_the_switch_and_a_scope():
    """删除/移动要**两份都成立**:开关是"允许这类操作",范围是"允许在哪"。

    写坏一个文件还能改回来,删掉就没了 —— 所以它不和写权限共用一条。
    """
    assert not FsGrant(write=("reports/",), delete=False).allows("reports/a.md", "delete")
    assert not FsGrant(write=(), delete=True).allows("reports/a.md", "delete")
    assert FsGrant(write=("reports/",), delete=True).allows("reports/a.md", "delete")


def test_an_unknown_mode_is_denied():
    """mode 写错了要**拒绝**,不是放行 —— 新加工具时最容易在这儿出错。"""
    assert not FsGrant(write=(FS_ANY,)).allows("a.md", "wrtie")


def test_explain_names_the_scope_so_it_can_ask_for_the_right_thing():
    """被拒时说的是"你被允许的范围是 X,`Y` 不在里面" —— 它才好照着申请。"""
    why = FsGrant(write=("notes/",)).explain("reports/a.md", "write")
    assert "notes/" in why and "reports/a.md" in why
    assert "suspend" in why, "该告诉它申请的路子,而不是让它绕"

    no_delete = FsGrant(write=("notes/",), delete=False).explain("notes/a.md", "delete")
    assert "删除" in no_delete and "分开" in no_delete

    nothing = FsGrant(read=(), write=()).explain("a.md", "write")
    assert "没有给你写任何文件" in nothing


def test_covers_is_how_overlap_is_decided():
    """两个范围撞不撞,靠这个。**宁可误拦也不能放过** —— 误拦只是换个范围重派。"""
    assert FsGrant.covers("reports/", "reports/a.md")
    assert FsGrant.covers("reports/", "reports/deep/a.md")
    assert FsGrant.covers(FS_ANY, "anywhere/x.md")
    assert not FsGrant.covers("reports/a.md", "reports/"), "文件范围包不住整个目录"
    assert not FsGrant.covers("reports/", "reportsx/")
    assert FsGrant.covers("notes/a.md", "notes/a.md")


# ============================== safe_path ==============================


def test_safe_path_has_no_default_mode():
    """**必填是故意的**:给个默认值的话,漏写的那一处会静默按默认放行 ——
    而漏写最容易发生在**新加的写工具**上,那正是最需要拦的地方。"""
    mode = inspect.signature(core.safe_path).parameters["mode"]
    assert mode.default is inspect.Parameter.empty
    assert mode.kind is inspect.Parameter.KEYWORD_ONLY or mode.kind.name == "POSITIONAL_OR_KEYWORD"


@pytest.mark.parametrize("bad", ["../outside.md", "../../etc/passwd",
                                 "notes/../../outside.md"])
def test_it_refuses_to_climb_out_of_the_workspace(bad):
    with pytest.raises(PermissionError) as e:
        core.safe_path(bad, "write")
    assert "工作区" in str(e.value)


def test_it_refuses_an_absolute_path_outside_the_workspace():
    with pytest.raises(PermissionError):
        core.safe_path(str(helpers.PROJ / "x.md"), "write")


def test_it_refuses_to_follow_a_symlink_out():
    """软链也要挡 —— `resolve()` 会把符号链接展开,所以链接指向外面时这条路径就出界了。"""
    outside = helpers.PROJ          # 工作区之外(项目根),但确实存在
    link = helpers.WS / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("这个环境建不了符号链接(Windows 需要开发者模式)")
    with pytest.raises(PermissionError):
        core.safe_path("link/secret.md", "write")


def test_the_key_is_not_allowed_to_write_outside_its_scope():
    """被收窄的授权(子 agent)连工作区里的别处也写不了。"""
    c = AgentCtx(role="sub", fs=FsGrant(read=(FS_ANY,), write=("reports/",)))
    with use(c):
        assert core.safe_path("reports/a.md", "write").name == "a.md"
        with pytest.raises(PermissionError) as e:
            core.safe_path("notes/a.md", "write")
        assert "reports/" in str(e.value), "被拒时要说清允许的范围"


def test_is_system_dir_keeps_the_agents_own_house_out_of_reach():
    """工作区的根、回收站、git、长期记忆、clones —— 任何时候都不许碰。

    注意不能用 is_relative_to(ROOT) 来判:那样工作区里每个普通文件都会被误挡。
    """
    assert core.is_system_dir(core.ROOT)
    assert core.is_system_dir(core.TRASH_DIR / "a.txt")
    assert core.is_system_dir(core.GIT_DIR / "config")
    assert core.is_system_dir(core.MEMORY_FILE)
    assert core.is_system_dir(core.CLONES_DIR / "repo" / "x")
    assert not core.is_system_dir(helpers.WS / "notes.md"), "普通文件不是系统目录"
    assert not core.is_system_dir(core.ROOT / ".trashx"), "名字像不算数"


def test_clip_text_says_how_much_it_hid():
    """只切断、不说一句,读的人会以为看到的就是全部。"""
    assert core.clip_text("短", 100) == "短"
    assert core.clip_text("任意", 0) == "任意", "0 = 不限"

    out = core.clip_text("字" * 50, 10)
    assert out.startswith("字" * 10)
    assert "另有 40 字未显示" in out


# ============================== 只放行公网地址 ==============================

def _resolve_to(monkeypatch, *ips):
    monkeypatch.setattr(core.socket, "getaddrinfo",
                        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                          (ip, 80)) for ip in ips])


def test_an_internal_address_is_refused_even_via_a_public_name(monkeypatch):
    """**只比对主机名字符串是不够的** —— localtest.me 这类域名就解析到 127.0.0.1。

    所以必须拿 DNS 解析出来的真实 IP 判。云元数据接口(169.254.169.254)也在这一条里。
    """
    for name, ip in [("localtest.me", "127.0.0.1"), ("内网", "192.168.1.10"),
                     ("元数据", "169.254.169.254"), ("保留段", "0.0.0.0")]:
        _resolve_to(monkeypatch, ip)
        with pytest.raises(PermissionError) as e:
            core._assert_public_url(f"http://{name}/x")
        assert "非公网" in str(e.value), f"{name} -> {ip} 该被挡下"


def test_a_public_address_goes_through(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    core._assert_public_url("https://example.com/a?b=1")      # 不抛就是通过


def test_only_http_and_https(monkeypatch):
    for bad in ["file:///etc/passwd", "ftp://example.com/x", "gopher://x"]:
        with pytest.raises(PermissionError):
            core._assert_public_url(bad)


def test_a_name_that_does_not_resolve_is_a_connection_error(monkeypatch):
    """解析不了 ≠ 不允许:两者的处置不同(一个是网络问题,一个是安全边界)。"""
    def boom(*a, **k):
        raise socket.gaierror("nope")
    monkeypatch.setattr(core.socket, "getaddrinfo", boom)
    with pytest.raises(ConnectionError):
        core._assert_public_url("http://no-such-host.invalid/")
