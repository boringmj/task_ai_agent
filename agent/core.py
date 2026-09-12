from __future__ import annotations

import ipaddress
import os
import socket
import uuid
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from openai import OpenAI
from rich.console import Console


# 加载环境变量
load_dotenv()

# 模型名称
MODEL = os.environ.get("MODEL", "deepseek-flash")
# 模型支持的上下文窗口大小
MAX_CONTEXT_TOKENS = int(os.environ.get("MAX_CONTEXT_TOKENS", "128000"))
# 自动压缩阈值大小百分比
AUTO_COMPACT_RATIO = float(os.environ.get("AUTO_COMPACT_RATIO", "0.9"))
# 单轮会话中模型与工具之间往返的最大轮数
MAX_STEPS = int(os.environ.get("MAX_STEPS", "50"))
# 护栏:单次写入的字节上限,防止模型一口气写爆磁盘
MAX_WRITE_BYTES = int(os.environ.get("MAX_WRITE_BYTES", str(3 * 1024 * 1024)))





_pending_images: list[str] = []  # 本轮待注入的图片 data URL,img 工具有效时会填一个



# 项目根 = 本包(agent/)的上一级。拆分成包之后 __file__ 在 agent/ 里,
# 不能再直接用 Path(__file__).parent(那样会指到 agent/ 自己,提示词/工作区全找错);
# 可用 AGENT_PROJECT_DIR 覆盖,方便把包挪到别处。
PROJECT_DIR = Path(
    os.environ.get("AGENT_PROJECT_DIR") or Path(__file__).resolve().parent.parent
).resolve()
# 系统提示词跟着代码走,不放进沙箱 —— 模型不能读自己的提示词,更不能改
PROMPT_FILE = PROJECT_DIR / "system_prompt.md"


def _resolve_workspace() -> Path:
    """确定沙箱目录:项目下的一个专用子目录,模型只能在里面活动。

    刻意不允许指向项目根或项目外 —— 否则 agent.py、.env 都会落进射程。
    """
    root = (PROJECT_DIR / os.environ.get("AGENT_WORKSPACE", "workspace")).resolve()
    if root == PROJECT_DIR or not root.is_relative_to(PROJECT_DIR):
        raise SystemExit(
            f"AGENT_WORKSPACE 必须是项目内的子目录,不能是项目根目录或外部路径。\n"
            f"  当前解析结果:{root}\n"
            f"  项目目录:    {PROJECT_DIR}"
        )
    root.mkdir(parents=True, exist_ok=True)  # 不存在就建一个,免得首次运行到处报错
    return root


# 沙箱:所有文件操作都被限制在这个目录内
ROOT = _resolve_workspace()
TRASH_MAX_AGE_DAYS = int(os.environ.get("TRASH_MAX_AGE_DAYS", "7"))

# 长期记忆:agent 在这个文件里记录跨会话保留的关键事实,启动时注入系统提示词
# 放在 .agent/ 隐藏目录,避免出现在用户的日常浏览里(list_files 默认过滤点开头项)
MEMORY_FILE = ROOT / ".agent" / "memory.md"

# ---- git:工作区内容的版本管理 ----
# 仓库建在 workspace 内部(独立于项目根的 .git),元数据在 .git/ 里,整体不出沙箱
GIT_DIR = ROOT / ".git"
# 克隆进来的外部仓库统一放这里,每个自成一体,不干扰 workspace 根仓库的自管版本
CLONES_DIR = ROOT / "clones"
CLONES_DIR.mkdir(exist_ok=True)



# DeepSeek 兼容 OpenAI 协议,只需要换 base_url
client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
)

# 负责终端渲染:Markdown 排版、代码高亮,以及 Windows 老控制台的 ANSI 支持
console = Console()


def safe_path(path: str) -> Path:
    """把模型给的路径解析为绝对路径,并确保它没有逃出 ROOT。

    相对路径基于 ROOT 解析;绝对路径经 resolve() 后一并校验。
    resolve() 会展开 .. 和符号链接,所以 "../../etc" 和指向外部的软链都会被挡下。
    """
    resolved = (ROOT / path).resolve()
    if not resolved.is_relative_to(ROOT):
        raise PermissionError(f"拒绝访问工作区 {ROOT} 之外的路径:{resolved}")
    return resolved


def _rel(path: Path) -> str:
    """统一用相对工作区的 posix 路径回显:短、可跨平台、能直接再喂给别的工具。"""
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def _assert_public_url(url: str) -> None:
    """校验 URL 能否安全访问:协议受限,且解析出的每个 IP 都必须是公网地址。

    只比对主机名字符串是不够的 —— localtest.me 这类域名会解析到 127.0.0.1。
    必须先做 DNS 解析拿到真实 IP 再判断,否则内网和云元数据接口都是敞开的。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise PermissionError(
            f"只允许 http/https,收到 {parsed.scheme or '(空)'}。"
            f"file:// 之类的协议能绕过工作区限制去读本地文件。"
        )
    host = parsed.hostname
    if not host:
        raise ValueError(f"URL 里解析不出主机名:{url}")

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ConnectionError(f"域名解析失败:{host}({exc})") from None

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        # is_global 一次覆盖回环、私有网段、链路本地(含云元数据 169.254.169.254)和保留地址
        if not ip.is_global:
            raise PermissionError(
                f"拒绝访问非公网地址:{host} 解析到 {ip}。"
                f"本机服务、内网主机和云元数据接口都不允许访问。"
            )
