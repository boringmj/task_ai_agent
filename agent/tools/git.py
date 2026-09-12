from __future__ import annotations

from .registry import tool

import os
import shlex
import subprocess
from pathlib import Path

from ..core import (
    CLONES_DIR,
    GIT_DIR,
    GIT_EXCLUDE,
    GIT_FORBIDDEN,
    GIT_MAX_OUTPUT,
    GIT_RISKY,
    GIT_SAFE,
    ROOT,
    safe_path,
    _assert_public_url,
)


# ---------------- git:工作区内容的版本管理 ----------------
# 与项目根的那个仓库无关。它只管 workspace/ 里的东西,元数据也锁在 workspace/.git,
# 所以整个 git 工具一条命令都不会碰到沙箱外。


def _git_run(*args: str) -> subprocess.CompletedProcess:
    """在 workspace 根仓库里跑 git:固定工作区,并强制英文输出。

    强制 LC_ALL=C 很重要 —— Windows 中文系统下 git 默认按 GBK 输出,和 Python
    的 UTF-8 对不上,内容会被 errors=replace 替换成乱码。英文输出是稳定的。
    """
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    return subprocess.run(
        ["git", "-c", "core.quotepath=false",
         "--git-dir", str(GIT_DIR), "--work-tree", str(ROOT), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env,
    )


def _git_run_in(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """在任意仓库(克隆进来的或 workspace 根)里跑 git。

    用 -C 让 git 自行发现该目录下的 .git,不预先假定仓库结构。
    """
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env,
    )


def _git_is_repo() -> bool:
    """让 git 自己确认仓库是否有效。只看目录存在不够:Windows 上删除 .git 时
    常因文件被锁定留下残骸,目录是有的但仓库已损坏。"""
    result = _git_run("rev-parse", "--git-dir")
    return result.returncode == 0


def _git_ensure_exclude() -> None:
    """把本地状态目录补进 .git/info/exclude(幂等,已存在的仓库也会补)。

    始终写全 GIT_EXCLUDE 里缺失的项,而不是只在 init 时写一次 —— 否则后续新增
    的忽略项(如 .pylibs/)对被忽略的旧仓库不生效。
    """
    exclude_file = GIT_DIR / "info" / "exclude"
    existing = exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
    have = set(existing.splitlines())
    missing = [e for e in GIT_EXCLUDE if e not in have]
    if missing:
        prefix = existing if existing.rstrip() else ""
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        exclude_file.write_text(prefix + "\n".join(missing) + "\n", encoding="utf-8")


def _git_ensure_repo() -> None:
    """确保 workspace 是一个有效的仓库,并忽略 agent 自己的内部目录。

    不能靠 GIT_DIR 是否存在来判断 —— Windows 上 rmtree 删不掉被锁定的 git 文件,
    可能留下一个"目录还在但仓库已烂"的残骸。必须让 git 验证有效性,无效则清理重建。
    """
    if not _git_is_repo():
        if GIT_DIR.exists():
            # 清掉残缺的 .git(先去掉只读属性,否则 Windows 删不掉)
            for root, dirs, files in os.walk(GIT_DIR, topdown=False):
                for name in files:
                    p = Path(root) / name
                    p.chmod(0o666)
                    try:
                        p.unlink()
                    except OSError:
                        pass
            try:
                GIT_DIR.rmdir()
            except OSError:
                pass
        _git_run("init")
        _git_run("config", "user.name", "boringmj")
        _git_run("config", "user.email", "boringmj@github")
        (GIT_DIR / "info").mkdir(parents=True, exist_ok=True)
    _git_ensure_exclude()  # 无论新建还是已存在,都确保忽略项齐全


def _git_clone(repo: Path, *args: str) -> str:
    """把外部仓库克隆进 CLONES_DIR 下的一个新子目录。

    clone 自带 .git,不需要 workspace 根仓库预先 init —— 这正是之前"自动初始化
    导致冲突"的根源。所以克隆走这条路,并在克隆后再检查目标是否是个有效仓库。
    """
    if not args:
        raise ValueError("clone 需要一个仓库 URL。")

    url = next((a for a in args[1:] if a.startswith(("http://", "https://"))), None)
    if url is None:
        raise PermissionError("clone 只接受以 http:// 或 https:// 开头的 URL。")
    _assert_public_url(url)  # 复用抓取那套 SSRF 防护:公网才可,不许打内网

    # 目标目录必须显式给出,且落在 CLONES_DIR 下;缺省则直接用仓库名,头、尾都按 URL 来
    parts = list(args)
    given_dest = next((i for i, a in enumerate(parts) if a == url), None)
    dest_arg = parts[given_dest + 1] if given_dest is not None and given_dest + 1 < len(parts) else ""

    if dest_arg:
        dest = safe_path(dest_arg)
        if dest == CLONES_DIR or not dest.is_relative_to(CLONES_DIR):
            raise PermissionError(f"克隆目标必须位于 clones/ 目录内(不能是工作区根),收到:{dest}")
    else:
        # 没给目标目录,就按 URL 的仓库名落到 clones/ 下
        name = url.rstrip("/").split("/")[-1]
        dest = CLONES_DIR / name
        parts.append(str(dest))

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and any(dest.iterdir()):
        raise FileExistsError(f"克隆目标 {dest} 已存在且非空,不能覆盖。换一个名字。")

    result = _git_run_in(repo, *parts)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        return f"git clone 失败(exit {result.returncode}):\n{output}"
    return f"已克隆到 {dest}(外部仓库,独立于 workspace 根仓库)。"


@tool(
    description="执行 git 子命令。默认(不给 repo)操作工作区根仓库,管 workspace 内容自己的版本。"
                "clone 外部仓库时用 'clone <url> <clones/下的目录>',仓库会落在 clones/ 下,独立于根仓库。"
                "操作克隆进来的仓库时,把 repo 设成 'clones/xxx'。"
                "常用:status、diff、log、add、commit、branch。只白名单放行安全指令;"
                "reset/merge/pull/push 等会改动历史或连远程的必须 confirm=true。改完文件先 status 看看,再 add + commit 存版本。",
    parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的 git 子命令,例如 'status' 或 'add .',不含开头的 git",
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": "仅用于会改动 git 历史或连远程的命令(reset/merge/pull/push)。是否已获得用户确认,默认 false",
                    },
                    "repo": {
                        "type": "string",
                        "description": "要操作哪个仓库:默认 '.' 是工作区根仓库;操作克隆进来的外部仓库时填 'clones/仓库名'",
                    },
                },
                "required": ["command"],
            },
)
def git(command: str, confirm: bool = False, repo: str = ".") -> str:
    """在某个 git 仓库里执行 git 子命令。默认仓库是 workspace 根(自管版本);
    操作克隆进来的外部仓库时,把 repo 设成如 "clones/adminservice-collaboration"。

    不给 shell,只做 argv 级调用,所以不存在命令注入。"命令"是你要执行的 git
    子命令,例如 "status" 或 "commit -am '更新配置'"。
    """
    target = (ROOT / repo).resolve()
    if target == ROOT:
        # 工作区根仓库:确保已初始化,再走固定的 --git-dir/--work-tree
        _git_ensure_repo()
        is_root_repo = True
    else:
        # 操作克隆进来的外部仓库:必须是 clones/ 下的、且确认是有效仓库
        if not target.is_relative_to(CLONES_DIR):
            raise PermissionError(f"git 只能操作工作区根或 clones/ 下的仓库,收到:{target}")
        if not target.is_dir():
            raise FileNotFoundError(f"仓库 {target} 不存在;若是要克隆外部仓库,请用 git clone <url> <clones/下的目录>")
        check = _git_run_in(target, "rev-parse", "--git-dir")
        if check.returncode != 0:
            raise ValueError(f"{target} 不是一个有效的 git 仓库(可能还未完成克隆)。")
        target = target.resolve()  # 记录真实路径供后续 -C 使用
        is_root_repo = False

    parts = shlex.split(command)  # 转义交给 shlex 处理,别手写 split
    if not parts:
        raise ValueError("git 命令不能为空")
    sub = parts[0]

    if sub in GIT_FORBIDDEN:
        raise PermissionError(
            f"git {sub} 被禁止:它对纯本地的工作区版本管理无用,或不可逆。"
            f"需要的版本操作优先用 status/add/commit/log/branch/stash。"
        )
    if sub == "clone":
        # 克隆只在工作区根做(它往 clones/ 写新仓库),不能在克隆出来的仓库里再克隆
        if not is_root_repo:
            raise PermissionError("只能在工作区根仓库里执行 clone,不能嵌套克隆。")
        return _git_clone(target, *parts)  # 不调用 _git_ensure_repo,避免抢占目标目录

    if sub not in GIT_SAFE and sub not in GIT_RISKY:
        # 白名单之外的一律不接受 —— 宁可不放行,也不靠黑名单逐个封
        raise PermissionError(
            f"git {sub} 不在允许的指令里。可用:{'、'.join(sorted(GIT_SAFE))}。"
        )
    if sub in GIT_RISKY and not confirm:
        return (
            f"git {sub} 会改动 git 状态或历史,属于较危险的操作。"
            f"请先征求用户同意,确认后再带 confirm=true 重新调用。"
        )

    result = _git_run_in(target, *parts) if not is_root_repo else _git_run(*parts)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        return f"git {sub} 失败(exit {result.returncode}):\n{output}"

    truncated = ""
    if len(output) > GIT_MAX_OUTPUT:
        output, truncated = output[:GIT_MAX_OUTPUT], "\n(输出过长,已截断)"
    return output + truncated if output else "(git 没有输出)"
