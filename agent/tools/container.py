from __future__ import annotations

from .registry import tool

import os

import shutil
import subprocess
import uuid
from datetime import datetime

from ..core import (
    ROOT,
    clip_text,
)
from .. import ctx
from ..ctx import FS_ANY
from ..skills import CONTAINER_SKILLS_DIR, SKILLS_DIR


# ---- container 工具专属配置(环境变量名不变,仍可在 .env 覆盖)----
# ---- 容器执行(Docker 沙箱)相关 ----
# 安全项全部硬编码在 _docker_run 里,不给 agent 配置入口。
CONTAINER_IMAGE = os.environ.get("DOCKER_IMAGE", "python:3.11-slim")
# 容器里 pip 的安装目标(相对工作区)。它对**所有** agent 可写,理由见 _workspace_mounts。
PYLIBS_REL = ".pylibs"
# 容器内命令的硬超时:用 GNU timeout 在容器内部自我了断。
# 即使 agent 进程崩溃,容器也能到点自毁并被 --rm 清掉,不会无限残留。
CONTAINER_CMD_TIMEOUT = int(os.environ.get("DOCKER_CMD_TIMEOUT", "60"))
# agent 侧再留一个稍长的兜底(subprocess 超时),万一容器内的 timeout 没生效仍能按名 kill。
CONTAINER_TIMEOUT = CONTAINER_CMD_TIMEOUT + 8
CONTAINER_OUTPUT_MAX = int(os.environ.get("DOCKER_OUTPUT_MAX", "10000"))  # 输出进上下文的字符上限
CONTAINER_MEMORY = os.environ.get("DOCKER_MEMORY", "1g")
CONTAINER_CPUS = os.environ.get("DOCKER_CPUS", "2.0")
CONTAINER_PIDS = int(os.environ.get("DOCKER_PIDS", "200"))


# ---------------- 容器执行(Docker 沙箱) ----------------
# 让 agent 运行任意 Python/命令,但全程关在容器里:只挂 workspace,非 root,
# 无特权,cap-drop,资源封顶,超时强杀。网络默认开启(用户接受,便于装包干活)。


def docker_health() -> tuple[bool, str]:
    """检查 Docker 是否可用:CLI 在不在、守护进程通不通。返回 (可用, 说明)。"""
    if shutil.which("docker") is None:
        return False, "未找到 docker 命令(请确认 Docker 已安装并在 PATH 中)"
    try:
        r = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"Docker 守护进程不可达:{exc}"
    if r.returncode != 0:
        return False, f"Docker 守护进程不可达(cmn:{r.stderr.strip()[:120]})"
    return True, f"Docker 就绪(server {r.stdout.strip()})"


# 残留清理只动"够老"的容器:容器内 timeout 到 CONTAINER_CMD_TIMEOUT 就自我了断,
# 比它两倍还老还没死的,必然是崩溃残留。按创建时间判断而不是无脑按名字前缀清 ——
# 否则多开 agent 时,后启动的实例会把前一个实例正在跑的容器一起杀掉。
CONTAINER_STALE_AFTER = CONTAINER_CMD_TIMEOUT * 2


def docker_cleanup_stale() -> int:
    """清理上次会话残留的 agent-exec-* 容器。

    agent 进程一旦崩溃,容器内的 timeout 仍会在 CONTAINER_CMD_TIMEOUT 后自我了断,
    但若崩溃发生在运行中、或 timeout 因故没生效,容器可能残留。启动时兜底清一把。
    只清"创建超过 CONTAINER_STALE_AFTER 秒"的,避免误杀其它 agent 实例正在跑的容器。
    返回清掉的容器数量。
    """
    try:
        r = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=agent-exec-",
             "--format", "{{.ID}}\t{{.CreatedAt}}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
    except Exception:  # noqa: BLE001
        return 0

    now = datetime.now().astimezone()
    ids = []
    for line in r.stdout.splitlines():
        cid, _, created = line.partition("\t")
        cid = cid.strip()
        if not cid:
            continue
        try:
            # Docker 给的是 "2026-09-13 01:41:37 +0800 CST",取到 %z 为止即可
            born = datetime.strptime(created.strip()[:25], "%Y-%m-%d %H:%M:%S %z")
        except ValueError:
            continue                     # 时间解析不出就保守跳过,宁可不删也不误杀
        if (now - born).total_seconds() >= CONTAINER_STALE_AFTER:
            ids.append(cid)

    for cid in ids:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=15)
    return len(ids)


def _docker_image_present() -> bool:
    """镜像是否已在本地。没在的话 docker run 第一次会自动拉(需网络)。"""
    r = subprocess.run(
        ["docker", "image", "inspect", CONTAINER_IMAGE],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
    )
    return r.returncode == 0


def _scope_kind(rel: str) -> str:
    """这个写范围**还不存在**时,按目录还是按文件建。

    权限判断那边根本不需要问这个问题(见 ctx.FsGrant.allows —— 纯文本前缀匹配,是目录
    还是文件都判得对)。**只有挂载需要**:docker 的 bind mount 要求宿主路径存在,不存在
    就得造一个出来,而造错了是实打实的坏事 —— 想要文件却建出目录,容器里写它就失败,
    工作区还多一个没人要的空目录。

    能拿来判断的只有名字本身,所以规则必须简单到能用一句话说清:

    - 结尾带 `/` → 目录(显式声明,最可靠)
    - 最后一段带 `.` 且不在开头 → 文件(`notes/plan.md`)
    - 其余 → 目录(`reports`、`Makefile` 这种没后缀的当目录,毕竟范围**多半**是目录)

    猜错不要紧 —— **要紧的是别说都不说**(见 prepare_scopes 的返回)。
    """
    if rel.endswith("/"):
        return "dir"
    last = rel.rstrip("/").rsplit("/", 1)[-1]
    return "file" if "." in last.lstrip(".") else "dir"


def prepare_scopes(write: tuple) -> list[str]:
    """把写范围准备好(不存在就按 _scope_kind 建),返回**给人看的说明**。

    在派活时就调用一次:范围是主 agent 定的,建错了该当场告诉它,而不是等子 agent
    在容器里撞上 Read-only 再说 —— 那时候主 agent 已经不在等了,中间隔着一层报告。
    """
    notes: list[str] = []
    for rel in write:
        if rel == FS_ANY:
            continue
        p = (ROOT / rel).resolve()
        if not p.is_relative_to(ROOT):
            notes.append(f"范围 `{rel}` 跑出工作区了,已忽略")
            continue
        if p.exists():
            continue
        kind = _scope_kind(rel)
        try:
            if kind == "file":
                p.parent.mkdir(parents=True, exist_ok=True)
                p.touch()
                notes.append(f"范围 `{rel}` 原来不存在,按**文件**建了一个空的")
            else:
                p.mkdir(parents=True, exist_ok=True)
                notes.append(f"范围 `{rel}` 原来不存在,按**目录**建了"
                             f"(要的是文件的话,写成 `{rel}/文件名` 或在末尾加个文件后缀)")
        except OSError as exc:
            notes.append(f"范围 `{rel}` 建不出来:{exc}")
    return notes


def _workspace_mounts() -> list[str]:
    """按当前 agent 的写权限,决定工作区**怎么挂进容器**。

    **为什么非这么做不可**:文件工具的读写检查(`safe_path`)只管得住走它的那些调用。
    容器里跑的是**任意代码**,而工作区是挂进去的 —— 只要挂的是可写的,一句
    `open("/workspace/x","w")` 就绕过去了。那不是"检查被绕过",是**那条路上根本没有检查**。
    而要说一句"请你不要这么做"就能拦住模型,这套东西的其余部分也就不用做了。

    所以把授权做进**挂载**里:底**只读**,允许写的那些地方再用更长的路径盖上去
    (重叠挂载时 Docker 取最具体的那条)。容器在操作系统层面就写不动别处 ——
    不依赖模型配合,也没有"换个法子绕过去"的余地。

    副作用要说清楚:受限的子 agent 在容器里**创建不了新文件到未授权的地方**,
    `/tmp` 是 tmpfs 所以临时文件照常。`.pylibs` 单独开一个可写口子 —— 那是 pip 的
    安装目标,skills 里到处写着 `pip install --target /workspace/.pylibs`,堵了就没法装包了。
    它是个包目录,不是用户的工作成果,拿它当例外是划算的。
    """
    fs = ctx.current().fs
    if FS_ANY in fs.write:
        return ["-v", f"{ROOT}:/workspace"]
    out = ["-v", f"{ROOT}:/workspace:ro"]
    writable = list(fs.write)
    if PYLIBS_REL not in writable:
        writable.append(PYLIBS_REL)
    prepare_scopes(tuple(writable))       # 不该由挂载来"顺手建",但漏了也得兜住
    for rel in writable:
        p = (ROOT / rel).resolve()
        if not p.is_relative_to(ROOT) or not p.exists():
            continue                      # 越界的、建不出来的:直接跳过,绝不放宽
        out += ["-v", f"{p}:/workspace/{rel}"]
    return out


def _docker_run(inner: list[str]) -> str:
    """在容器里执行,安全项硬编码。inner 是镜像之后的命令(如 ['python','-c','...'])。"""
    ok, err = docker_health()
    if not ok:
        return f"错误:Docker 不可用 —— {err}(启动 Docker Desktop 后重试)"

    name = f"agent-exec-{uuid.uuid4().hex[:10]}"
    cmd = [
        "docker", "run", "--rm",
        "--name", name,
        "--cap-drop", "ALL",                      # 去掉内核能力,减少逃逸面
        "--security-opt", "no-new-privileges",
        "--user", "1000:1000",                    # 非 root
        "--memory", CONTAINER_MEMORY,
        "--cpus", CONTAINER_CPUS,
        "--pids-limit", str(CONTAINER_PIDS),
        "--tmpfs", "/tmp",
        "-e", "HOME=/tmp",
        # PYTHONPATH 指向 workspace/.pylibs:agent 用 `pip install --target /workspace/.pylibs`
        # 装一次就永久保留(workspace 是宿主盘,不随容器销毁),之后每次 run_python 都能 import。
        "-e", "PYTHONPATH=/workspace/.pylibs",
        *_workspace_mounts(), "-w", "/workspace",   # 挂法跟着授权走,见 _workspace_mounts
        # 技能目录**只读**挂进来:`:ro` 保证容器改不了它 —— 技能是项目里的事实来源,
        # 不该被跑在里面的代码篡改。技能自带的脚本因此能在容器里跑:
        #   skills/<名>/scripts/x.py  ->  /skills/<名>/scripts/x.py
        # (技能放在工作区之外,不挂的话容器根本看不到它。)
        "-v", f"{SKILLS_DIR}:{CONTAINER_SKILLS_DIR}:ro",
        CONTAINER_IMAGE,
        # 容器内自毁:到 CONTAINER_CMD_TIMEOUT 由 GNU timeout 终止,不依赖 agent 进程活着。
        # 这样 agent 崩溃也不会留下收不掉的容器(否则 --rm 只会等容器自己退出)。
        # -k 5:若进程忽略了 SIGTERM,5 秒后强制 SIGKILL,避免 timeout 自己也挂住。
        "timeout", "-k", "5", str(CONTAINER_CMD_TIMEOUT),
        *inner,
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=CONTAINER_TIMEOUT,
            stdin=subprocess.DEVNULL,  # 容器 stdin 直接给 EOF:交互式命令(input()、无参 cat)立即失败,不傻等
        )
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", name], capture_output=True, timeout=10)
        return f"执行超时(>{CONTAINER_TIMEOUT}s),已终止容器。"

    out, err = r.stdout.strip(), r.stderr.strip()
    # 成功只返回 stdout(真正的结果);Docker 拉镜像的进度在 stderr,不该混进结果。
    # 失败才带上 stderr(真正的报错)。
    if r.returncode != 0:
        body = err or out
        return f"执行失败(exit {r.returncode}):\n{(clip_text(body, CONTAINER_OUTPUT_MAX) or '(无输出)')}"
    return (clip_text(out, CONTAINER_OUTPUT_MAX) if out else "(容器无输出)")


@tool(
    agents=("main", "sub"),
    description="在安全的 Docker 隔离容器里执行一段 Python 代码。适合数据分析、计算、"
                "处理工作区文件 —— 你已有的工具做不到的运算用这个。"
                "容器只能访问工作区、非 root、无内核特权、资源封顶、超时强杀;"
                "执行完容器即销毁,不会留下任何东西。"
                "输出会截断到 1 万字符。需要第三方库时,可以先用 pip 装(容器断网则装不了,"
                "但网络默认开启,可 pip install --user 所需库)。"
                "注意路径:容器里的工作区在 /workspace,代码里用相对文件名或 /workspace/... 路径,"
                "别用文件工具返回的宿主路径(如 D:\\...),那在容器里不存在。",
    parameters={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "要执行的 Python 代码,可以多行",
                    }
                },
                "required": ["code"],
            },
)
def run_python(code: str) -> str:
    """在隔离容器里执行一段 Python 代码,只能访问工作区,不碰宿主其他内容。"""
    if not code.strip():
        raise ValueError("Python 代码不能为空")
    return _docker_run(["python", "-c", code])


@tool(
    agents=("main", "sub"),
    description="在安全的 Docker 隔离容器里执行一条 shell 命令。适合在环境里跑工具、"
                "装包、查看容器内情况。容器只能访问工作区、非 root、无内核特权、资源封顶、"
                "超时强杀,执行完即销毁。"
                "输出截断到 1 万字符。注意:命令里访问的工作区之外的路径,是容器自己的文件系统,"
                "不是你宿主的 —— 它动不了宿主。容器里的工作区在 /workspace,用相对名或 /workspace/... 路径,"
                "别用宿主路径(如 D:\\...)。",
    parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的 shell 命令,例如 'ls -la' 或 'pip install --user pandas'",
                    }
                },
                "required": ["command"],
            },
)
def run_command(command: str) -> str:
    """在隔离容器里执行一条 shell 命令,同样只访问工作区.给 agent 装包、跑工具。"""
    if not command.strip():
        raise ValueError("命令不能为空")
    return _docker_run(["/bin/sh", "-c", command])
