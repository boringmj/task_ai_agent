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
from ..skills import CONTAINER_SKILLS_DIR, SKILLS_DIR


# ---- container 工具专属配置(环境变量名不变,仍可在 .env 覆盖)----
# ---- 容器执行(Docker 沙箱)相关 ----
# 安全项全部硬编码在 _docker_run 里,不给 agent 配置入口。
CONTAINER_IMAGE = os.environ.get("DOCKER_IMAGE", "python:3.11-slim")
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


def _docker_health() -> tuple[bool, str]:
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


def _docker_cleanup_stale() -> int:
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


def _docker_run(inner: list[str]) -> str:
    """在容器里执行,安全项硬编码。inner 是镜像之后的命令(如 ['python','-c','...'])。"""
    ok, err = _docker_health()
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
        "-v", f"{ROOT}:/workspace", "-w", "/workspace",  # 唯一可写的:只有 workspace
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
