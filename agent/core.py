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
# `list_files` 单次返回的条目数上限(含文件与子目录),避免超大目录撑爆上下文
MAX_ENTRIES = int(os.environ.get("MAX_ENTRIES", "200"))
# 护栏:单次写入的字节上限,防止模型一口气写爆磁盘
MAX_WRITE_BYTES = int(os.environ.get("MAX_WRITE_BYTES", str(3 * 1024 * 1024)))
# 护栏:read_file 单次返回的字符上限。超出会明确提示(不再静默截断),让模型改用行区间读
MAX_READ_CHARS = int(os.environ.get("MAX_READ_CHARS", str(3 * 1024 * 1024)))
# 即使在沙箱内,这些文件也禁止读取 —— 纵深防御,防止沙箱里混入密钥文件
DENY_READ = {".env"}

# ---- 文件搜索(按名 / 按内容)护栏 ----
# 搜索很容易命中一大片,和列目录同理:必须设上限,否则把上下文撑爆。
MAX_FIND_RESULTS = int(os.environ.get("MAX_FIND_RESULTS", "300"))         # find_files 最多返回多少条命中
MAX_GREP_MATCHES = int(os.environ.get("MAX_GREP_MATCHES", "200"))         # grep_files 最多返回多少条命中行
MAX_GREP_FILE_BYTES = 200 * 1024 * 1024  # 单个文件超此大小才跳过(防极端超大文件);超了会在结果里说明,不静默跳过
MAX_GREP_LINE_CHARS = int(os.environ.get("MAX_GREP_LINE_CHARS", "200"))      # 每条命中行的内容字符上限
# 递归搜索时剪掉的目录:隐藏目录(.git/.trash/.agent/.venv…)与常见的依赖/缓存目录
SEARCH_SKIP_DIRS = {"__pycache__", "node_modules", ".pylibs"}

# ---- 联网相关的护栏 ----
FETCH_TIMEOUT = float(os.environ.get("FETCH_TIMEOUT", "10.0"))  # 单次请求超时(秒)
MAX_FETCH_BYTES = int(os.environ.get("MAX_FETCH_BYTES", str(3 * 1024 * 1024)))  # 最多下载多少字节,超出直接截断
MAX_FETCH_CHARS = int(os.environ.get("MAX_FETCH_CHARS", str(3 * 1024 * 1024)))  # 正文进上下文的字符上限,别把窗口撑爆
MAX_REDIRECTS = int(os.environ.get("MAX_REDIRECTS", "5"))  # 最多跟几次跳转,每一跳都要重新校验
USER_AGENT = os.environ.get("USER_AGENT", "task-ai-agent/0.1")
# 下载单个文件的字节上限。与 fetch_url 不同,下载是写盘、内容不进上下文,
# 所以上限按磁盘/带宽来设,不用迁就上下文窗口。
DOWNLOAD_MAX_BYTES = int(os.environ.get("DOWNLOAD_MAX_BYTES", str(100 * 1024 * 1024)))  # 100MB,可据需要调

# ---- 容器执行(Docker 沙箱)相关 ----
# 安全项全部硬编码在 _docker_run 里,不给 agent 配置入口。
DOCKER_IMAGE = os.environ.get("DOCKER_IMAGE", "python:3.11-slim")
# 容器内命令的硬超时:用 GNU timeout 在容器内部自我了断。
# 即使 agent 进程崩溃,容器也能到点自毁并被 --rm 清掉,不会无限残留。
DOCKER_CMD_TIMEOUT = int(os.environ.get("DOCKER_CMD_TIMEOUT", "60"))
# agent 侧再留一个稍长的兜底(subprocess 超时),万一容器内的 timeout 没生效仍能按名 kill。
DOCKER_TIMEOUT = DOCKER_CMD_TIMEOUT + 8
DOCKER_OUTPUT_MAX = int(os.environ.get("DOCKER_OUTPUT_MAX", "10000"))  # 输出进上下文的字符上限
DOCKER_MEMORY = os.environ.get("DOCKER_MEMORY", "1g")
DOCKER_CPUS = os.environ.get("DOCKER_CPUS", "2.0")
DOCKER_PIDS = int(os.environ.get("DOCKER_PIDS", "200"))

# ---- 搜索相关 ----
# 换搜索服务只改这两个环境变量,不用动代码
SEARCH_PROVIDER = os.environ.get("SEARCH_PROVIDER", "").strip().lower()
SEARCH_API_KEY = os.environ.get("SEARCH_API_KEY", "").strip()
MAX_SEARCH_RESULTS = int(os.environ.get("MAX_SEARCH_RESULTS", "10"))  # 单次搜索最多返回几条
MAX_SEARCHES_PER_TURN = int(os.environ.get("MAX_SEARCHES_PER_TURN", "5"))  # 单轮对话最多搜几次 —— agent 会自动循环,必须自带刹车
MAX_SNIPPET_CHARS = int(os.environ.get("MAX_SNIPPET_CHARS", "500"))  # 每条摘要的字符上限

# ---- 图像(多模态)相关 ----
IMG_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".webp": "image/webp", ".gif": "image/gif"}
IMG_MAX_BYTES = int(os.environ.get("IMG_MAX_BYTES", str(3 * 1024 * 1024)))  # 单张图片的字节上限,超出拒绝(避免 base64 后撑爆上下文)
_pending_images: list[str] = []  # 本轮待注入的图片 data URL,img 工具有效时会填一个

# 截屏:最长边会被压缩到这个像素数。视觉模型对大图会内部缩小,原样送 4K 截图
# 只会浪费图像 token 甚至被拒,所以发送前自己压成小数。
SCREEN_MAX_DIM = int(os.environ.get("SCREEN_MAX_DIM", "1280"))

# 剪贴板读进上下文的字符上限(读太长会白白占窗口)
MAX_CLIPBOARD_CHARS = int(os.environ.get("MAX_CLIPBOARD_CHARS", "10000"))

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
# 回收站:删除的文件移到这里,不做真删除,超过 MAX_AGE_DAYS 天后启动时自动清空
TRASH_DIR = ROOT / ".trash"
TRASH_INDEX_FILE = TRASH_DIR / "index.json"  # 记录 回收站文件名 -> 原路径,支撑还原
TRASH_MAX_AGE_DAYS = int(os.environ.get("TRASH_MAX_AGE_DAYS", "7"))
TRASH_DIR.mkdir(exist_ok=True)

# 长期记忆:agent 在这个文件里记录跨会话保留的关键事实,启动时注入系统提示词
# 放在 .agent/ 隐藏目录,避免出现在用户的日常浏览里(list_files 默认过滤点开头项)
MEMORY_FILE = ROOT / ".agent" / "memory.md"

# ---- git:工作区内容的版本管理 ----
# 仓库建在 workspace 内部(独立于项目根的 .git),元数据在 .git/ 里,整体不出沙箱
GIT_DIR = ROOT / ".git"
GIT_MAX_OUTPUT = int(os.environ.get("GIT_MAX_OUTPUT", "20000"))  # 单次命令输出进上下文的字符上限
# 克隆进来的外部仓库统一放这里,每个自成一体,不干扰 workspace 根仓库的自管版本
CLONES_DIR = ROOT / "clones"
CLONES_DIR.mkdir(exist_ok=True)
# 正常管理版本所需的安全指令;不在白名单里的指令一律拒绝(不管 confirm)
GIT_SAFE = {
    "status", "add", "commit", "log", "diff", "show", "rm", "mv",
    "branch", "switch", "checkout", "stash", "restore", "ls-files",
    "init", "rev-parse", "tag",
}
# 会改写工作区或历史的:威力中等,执行前必须 confirm=true
GIT_RISKY = {"reset", "revert", "merge", "pull", "push"}
# 彻底不可逆或对纯本地版本管理无用:即使用户确认也拒绝
GIT_FORBIDDEN = {"gc", "clean", "rebase", "filter-branch"}
# 工作区仓库要忽略的本地状态目录(含容器持久化的 .pylibs 包)
GIT_EXCLUDE = (".agent/", ".trash/", "clones/", "__pycache__/", ".pylibs/")

# ---- 虚拟机(QEMU)沙箱 ----
# QEMU 二进制目录、基础盘等均可在 .env 里配置;默认指向项目内 temp/(QEMU 完整安装)。
# 多开隔离:每个 agent 进程一个唯一实例 —— 独立 overlay 盘 + 独立 vmserver 口,
# 基础盘只读共享。这样同时跑多个 agent,各自的虚拟机互不干扰。
QEMU_DIR = Path(os.environ.get("VM_QEMU_DIR", str(PROJECT_DIR / "temp")))
QEMU_SYSTEM = Path(os.environ.get("VM_QEMU_SYSTEM", str(QEMU_DIR / "qemu-system-x86_64.exe")))
QEMU_IMG = Path(os.environ.get("VM_QEMU_IMG", str(QEMU_DIR / "qemu-img.exe")))
VM_DIR = Path(os.environ.get("VM_DIR", str(PROJECT_DIR / "vm")))
VM_BASE = Path(os.environ.get("VM_BASE", str(VM_DIR / "alpine-vmserver.qcow2")))  # 预装 vmserver 的新 base
VM_ACCEL = os.environ.get("VM_ACCEL", "whpx")      # whpx 快;没开就设 tcg(慢但通用)
VM_INSTANCE = uuid.uuid4().hex[:8]                 # 每进程唯一,决定盘和口的唯一性
VM_DISK = VM_DIR / f"work-{VM_INSTANCE}.qcow2"     # 本实例专属 overlay;重置=删它
VM_VMSERVER_PORT = 40000                            # vmserver 在 guest 内监听的端口(固定)


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
