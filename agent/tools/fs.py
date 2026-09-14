from __future__ import annotations

from .registry import tool

import os
import re
from datetime import datetime
from pathlib import Path

from .. import markdown as md
from ..core import (
    MAX_WRITE_BYTES,
    ROOT,
    is_system_dir,
    safe_path,
    _rel,
)


# ---- fs 工具专属配置(环境变量名不变,仍可在 .env 覆盖)----
# `list_files` 单次返回的条目数上限(含文件与子目录),避免超大目录撑爆上下文
FS_MAX_ENTRIES = int(os.environ.get("MAX_ENTRIES", "200"))
# 护栏:read_file 单次返回的字符上限。超出会明确提示(不再静默截断),让模型改用行区间读
FS_MAX_READ_CHARS = int(os.environ.get("MAX_READ_CHARS", str(3 * 1024 * 1024)))
# 一次 read_file 最多读几个文件。允许多文件是为了省调用次数(每多一次调用,模型就要重读
# 一遍整个历史),但一次塞太多同样撑上下文、模型也读不过来 —— 20 是够用又不失控的量级。
FS_MAX_READ_FILES = int(os.environ.get("MAX_READ_FILES", "20"))
# 一次 move_file 最多移几个。移动会改文件位置,比读危险 —— 上限和读一样,但返回里会逐条
# 列出来,方便用户核对到底动了什么。
FS_MAX_MOVE_FILES = int(os.environ.get("MAX_MOVE_FILES", "20"))
# 一次 list_files / find_files / grep_files 最多接受几个路径。和读文件同理:合并成一次
# 调用能省下好几轮"重发整个历史",但一次铺太开也没意义。
FS_MAX_LIST_DIRS = int(os.environ.get("MAX_LIST_DIRS", "20"))
# 即使在沙箱内,这些文件也禁止读取 —— 纵深防御,防止沙箱里混入密钥文件
FS_DENY_READ = {".env"}
# ---- 文件搜索(按名 / 按内容)护栏 ----
# 搜索很容易命中一大片,和列目录同理:必须设上限,否则把上下文撑爆。
FS_MAX_FIND_RESULTS = int(os.environ.get("MAX_FIND_RESULTS", "300"))         # find_files 最多返回多少条命中
FS_MAX_GREP_MATCHES = int(os.environ.get("MAX_GREP_MATCHES", "200"))         # grep_files 最多返回多少条命中行
FS_MAX_GREP_FILE_BYTES = 200 * 1024 * 1024  # 单个文件超此大小才跳过(防极端超大文件);超了会在结果里说明,不静默跳过
FS_MAX_GREP_LINE_CHARS = int(os.environ.get("MAX_GREP_LINE_CHARS", "200"))      # 每条命中行的内容字符上限
# 递归搜索时剪掉的目录:隐藏目录(.git/.trash/.agent/.venv…)与常见的依赖/缓存目录
FS_SEARCH_SKIP_DIRS = {"__pycache__", "node_modules", ".pylibs"}

@tool(
    agents=("main", "sub"),
    description="获取当前的日期、时间和星期。当用户询问「现在几点」「今天几号」「今天星期几」,"
                "或需要判断某个日期是工作日还是周末时使用。",
    parameters={"type": "object", "properties": {}},
)
def get_current_time() -> str:
    """当前时间。**带上星期几** —— 只给日期的话,模型要么让用户自己算,要么张口猜错。"""
    now = datetime.now()
    return f"{now:%Y-%m-%d %H:%M:%S} 星期{'一二三四五六日'[now.weekday()]}"


def _sniff_encoding(target: Path) -> str:
    """探测一个已存在文件的编码(读头部样本;UTF-16 看 BOM)。"""
    with target.open("rb") as fh:
        head = fh.read(65536)
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16"
    return _detect_text_encoding(head) if head else "utf-8"


def _read_text_with_encoding(target: Path) -> tuple[str, str]:
    """按探测到的编码读出文本,返回 (文本, 编码名)。

    GBK/GB18030 等中文编码的文件也能读。读时用 errors='replace':万一探测不准,
    也只是个别字符变乱码,不会整个读失败。(写回要用同一个编码,见 _save_lines / append_file。)
    """
    enc = _sniff_encoding(target)
    return target.read_text(encoding=enc, errors="replace"), enc


def _read_one(path: str, with_line_numbers: bool, start_line: int | None,
              end_line: int | None) -> str:
    """读单个文件,可选行区间。返回正文(或一句说明/错误)。

    这是 read_file 的内核 —— 多个文件时逐个调它,每个文件的上限、敏感文件拦截、
    行号规则都在这一个地方,不必在循环里重复。
    """
    target = safe_path(path)
    if target.name in FS_DENY_READ:
        raise PermissionError(f"{target.name} 属于敏感文件(如 .env),禁止读取")
    text, _enc = _read_text_with_encoding(target)
    lines = text.splitlines()
    total = len(lines)

    if start_line is None and end_line is None:
        lo, hi = 1, total
    else:
        if total == 0:
            return "(文件是空的)"
        lo = max(1, int(start_line or 1))
        hi = min(total, int(end_line or total))
        if lo > hi:
            return (f"错误:行区间无效(start_line={start_line}, end_line={end_line},"
                    f"文件共 {total} 行)")

    selected = lines[lo - 1:hi]
    if with_line_numbers:
        body = "\n".join(f"{i:>4} | {line}" for i, line in enumerate(selected, lo))
    else:
        body = "\n".join(selected)
    if len(body) > FS_MAX_READ_CHARS:
        body = body[:FS_MAX_READ_CHARS] + (
            f"\n\n…(内容过长,只返回前 {FS_MAX_READ_CHARS} 个字符;"
            f"请用 start_line/end_line 分段读)"
        )
    if (lo, hi) != (1, total):
        body += f"\n\n…(本次为第 {lo}-{hi} 行,文件共 {total} 行)"
    return body


@tool(
    agents=("main", "sub"),
    description="读取文本文件的内容,**一次可以读多个**(单次最多返回 3145728 个字符,超出会明确提示截断)。"
                "会自动识别编码,GBK 等中文编码的文件也能读。只能读取工作区内的文件。"
                "**path 可以给数组**,一次读几个相关文件 —— 每多一次工具调用,模型就要重读一遍"
                "整个历史,省一次调用就是省一份上下文。一次最多 20 个文件。"
                "用 start_line/end_line 可只读某个行区间(对数组里的**每个**文件都生效)—— "
                "grep_files 命中某行后想看附近上下文,就用它读那几行,不必把整个大文件读进来。"
                "准备用 edit_lines 或 insert_lines 按行修改文件前,先带 with_line_numbers=true 读一遍"
                "(或读目标区间)确认行号。",
    parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "文件路径(相对或绝对),可给数组一次读多个,如 [\"a.py\", \"b.py\"];"
                                       "只看一个文件时直接给字符串也行",
                    },
                    "with_line_numbers": {
                        "type": "boolean",
                        "description": "是否在每行前面加上行号,默认 false。按行编辑前应设为 true",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "起始行号(1 起始,含该行)。配合 end_line 只看某段;不填则从头",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "结束行号(含该行)。不填则读到末尾。带行号读区间时,行号仍是文件真实行号",
                    },
                },
                "required": ["path"],
            },
)
def read_file(path, with_line_numbers: bool = False,
              start_line: int | None = None, end_line: int | None = None) -> str:
    """读取工作区里的一个或多个文本文件。可只读某个行区间(1 起始,含两端)。

    **一次可以读多个**:path 传数组即可,看几个相关文件时不必来回调 —— 每次工具调用都会
    让模型重新读一遍整个历史,省一次调用就是省一份上下文。行区间对所有文件生效。
    带行号时,行号始终是**文件里的真实行号**,可直接喂给 edit_lines。
    """
    paths = [path] if isinstance(path, str) else list(path)
    if not paths:
        return "错误:没有给出要读的文件"
    if len(paths) > FS_MAX_READ_FILES:
        return (f"错误:一次最多读 {FS_MAX_READ_FILES} 个文件(收到 {len(paths)} 个)—— "
                f"分批读,或者先用 grep_files / find_files 缩小范围")

    single = len(paths) == 1
    parts = []
    for p in paths:
        try:
            body = _read_one(p, with_line_numbers, start_line, end_line)
        except Exception as exc:  # noqa: BLE001 - 一个文件读不了不该拖垮整批
            body = f"错误:{type(exc).__name__}: {exc}"
        # 多个文件必须各自带标题,否则读回来分不清哪段是谁的
        parts.append(body if single else f"===== {p} =====\n{body}")

    out = parts[0] if single else "\n\n".join(parts)
    if len(out) > FS_MAX_READ_CHARS:
        out = out[:FS_MAX_READ_CHARS] + (
            f"\n\n…(内容过长,只返回前 {FS_MAX_READ_CHARS} 个字符;"
            f"请减少文件数,或用 start_line/end_line 分段读)"
        )
    return out


@tool(
    agents=("main", "sub"),
    description="获取你的工作区目录(绝对路径)。所有相对路径都以它为基准,文件操作也不能超出它。当用户问「我在哪」「当前目录是什么」时使用。",
    parameters={"type": "object", "properties": {}},
)
def get_current_directory() -> str:
    return f"{ROOT}(你的工作区,所有文件操作都被限制在这个目录内)"


@tool(
    agents=("main", "sub"),
    description="列出某个目录下的文件和子目录。目录名以 / 结尾,文件会附带字节大小。"
                "当用户问「这里有什么文件」「列一下目录」,或者你需要先找到文件名再去读取它时使用。",
    parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要列出的目录路径(相对工作区),省略则为工作区根目录;"
                                       "可给数组一次列多个,如 [\"src\", \"tests\"]",
                    },
                    "show_hidden": {
                        "type": "boolean",
                        "description": "是否包含以点开头的隐藏文件,默认 false",
                    },
                },
            },
)
def list_files(path=".", show_hidden: bool = False) -> str:
    """列出目录内容。path 可以给数组,一次列多个目录。

    单个时保持原样(只有一行标题);多个时每块带 `===== 路径 =====`,一个目录出错不影响
    其余几个 —— 列目录本来就常是"顺便看看这几个地方"。
    """
    paths = [path] if isinstance(path, str) else list(path)
    if not paths:
        return "错误:没有给出要列出的目录"
    if len(paths) > FS_MAX_LIST_DIRS:
        return f"错误:一次最多列 {FS_MAX_LIST_DIRS} 个目录(收到 {len(paths)} 个),分批来吧"
    if len(paths) == 1:
        return _list_one(paths[0], show_hidden)

    parts = []
    for p in paths:
        try:
            parts.append(f"===== {p} =====\n{_list_one(p, show_hidden)}")
        except Exception as exc:  # noqa: BLE001 - 一个列不了不该拖垮整批
            parts.append(f"===== {p} =====\n错误:{type(exc).__name__}: {exc}")
    return "\n\n".join(parts)


def _list_one(path: str, show_hidden: bool) -> str:
    """列一个目录(内核)。"""
    target = safe_path(path)
    if not target.is_dir():
        return f"错误:{target} 不是一个目录"

    entries = [e for e in target.iterdir() if show_hidden or not e.name.startswith(".")]
    entries.sort(key=lambda e: (e.is_file(), e.name.lower()))  # 目录在前,再按名字排

    if not entries:
        return f"{target} 是空目录"

    lines = [
        f"{e.name}/" if e.is_dir() else f"{e.name}  ({e.stat().st_size} B)"
        for e in entries[:FS_MAX_ENTRIES]
    ]
    if len(entries) > FS_MAX_ENTRIES:
        lines.append(f"…(共 {len(entries)} 项,只显示前 {FS_MAX_ENTRIES} 项)")
    return "\n".join([f"{target}(共 {len(entries)} 项):", *lines])


def _iter_search_paths(root: Path):
    """递归产出 root 下的文件和子目录,剪掉隐藏目录与依赖/缓存目录。

    搜索不该翻进 .git/.trash/.agent/.venv 这些地方 —— 要么是本地状态、要么是海量噪音。
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and d not in FS_SEARCH_SKIP_DIRS
        ]
        base = Path(dirpath)
        for d in dirnames:
            yield base / d
        for f in filenames:
            yield base / f


@tool(
    agents=("main", "sub"),
    description="按文件名在工作区里递归查找(支持目录)。"
                "name 支持 * ? [ ] 通配符,不区分大小写;不含通配符时按「文件名包含该子串」匹配。"
                "不知道文件叫什么名字、或在某个目录树里找某类文件时用它。"
                "只返回路径(相对工作区),要看内容还要再用 read_file。",
    parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "文件名模式,如 *.py、test*、config.json;不含通配符时按子串匹配",
                    },
                    "path": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "在哪个目录下搜(相对工作区),省略则为工作区根目录;"
                                       "可给数组一次搜几处,如 [\"src\", \"tests\"],结果会合并去重",
                    },
                    "include_dirs": {
                        "type": "boolean",
                        "description": "是否把目录名也纳入匹配,默认 false(只找文件)",
                    },
                },
                "required": ["name"],
            },
)
def find_files(name: str, path=".", include_dirs: bool = False) -> str:
    """按文件名查找工作区里的文件(递归)。path 可以给数组,一次在几棵树里找。

    支持 * ? [ ] 通配符(不区分大小写);不含通配符时按子串匹配文件名。
    include_dirs=true 时把目录名也纳入匹配。只搜工作区,自动跳过隐藏目录与依赖目录。
    多个 path 的结果会**合并去重** —— 范围重叠时同一个文件只列一次。
    """
    paths = [path] if isinstance(path, str) else list(path)
    if not paths:
        return "错误:没有给出搜索路径"
    if len(paths) > FS_MAX_LIST_DIRS:
        return f"错误:一次最多搜 {FS_MAX_LIST_DIRS} 个路径(收到 {len(paths)} 个),分批来吧"

    hits: list[Path] = []
    missing: list[str] = []
    for one in paths:
        root = safe_path(one)
        if not root.exists():
            missing.append(str(root))       # 写错一个不该让其余几个白搜
            continue
        hits += _find_in(name, root, include_dirs)

    if missing and not hits:
        return f"错误:这些路径不存在:{'、'.join(missing)}"
    if not hits:
        what = "文件或目录" if include_dirs else "文件"
        return f"没有匹配「{name}」的{what}(搜索范围:{'、'.join(_rel(safe_path(p)) for p in paths)})"

    # 多个 path 范围可能重叠,同一个文件只列一次(保留首次出现的次序)
    uniq, seen = [], set()
    for h in hits:
        if h not in seen:
            seen.add(h)
            uniq.append(h)
    hits = uniq
    hits.sort(key=lambda p: _rel(p).lower())
    shown = hits[:FS_MAX_FIND_RESULTS]
    lines = [f"{_rel(p)}{'/' if p.is_dir() else ''}" for p in shown]
    tail = f"\n…(共 {len(hits)} 条,只显示前 {FS_MAX_FIND_RESULTS} 条)" if len(hits) > FS_MAX_FIND_RESULTS else ""
    skipped = f"(这些路径不存在,已跳过:{'、'.join(missing)})\n" if missing else ""
    return skipped + "\n".join([f"匹配「{name}」共 {len(hits)} 条:", *lines]) + tail


def _find_in(name: str, root: Path, include_dirs: bool) -> list[Path]:
    """在一个 root 下搜匹配项(内核,不做任何格式化)。"""
    import fnmatch
    pat = name.lower()
    has_glob = any(ch in name for ch in "*?[")
    hits: list[Path] = []
    for p in _iter_search_paths(root):
        if p.is_dir() and not include_dirs:
            continue
        low = p.name.lower()
        if fnmatch.fnmatch(low, pat) if has_glob else (pat in low):
            hits.append(p)
    return hits


def _detect_text_encoding(sample: bytes) -> str:
    """猜文本编码:优先 utf-8,其次中文常见的 gb18030/big5;都不行就按 utf-8 尽力解。"""
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            sample.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "utf-8"


@tool(
    agents=("main", "sub"),
    description="按文件内容在工作区里搜索,返回命中的文件路径、行号和行内容。"
                "pattern 是正则(不是通配符);ignore_case=true 可忽略大小写。"
                "只搜文本文件(二进制自动跳过),结果是「文件:行号: 内容」的形式。"
                "想知道某个函数/变量/字符串在哪些文件里出现过时用它,比一个个 read_file 高效得多。"
                "include 可按**文件名**收窄范围(逗号分隔的 glob,如 \"*.py\"、\"*.py,*.pyi\"),"
                "适合「我只看某类文件」这类诉求;但**别养成上来就限定类型的习惯** —— "
                "同一个名字常常也出现在配置(.json/.yaml/.toml)、文档(.md)、模板(.html)、脚本里,"
                "限死 *.py 就会漏掉。不确定时先不设 include 全搜一遍。"
                "命中数有上限,超出会提示截断。",
    parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "要匹配的正则表达式,如 def main、TODO|FIXME、requests\\.get",
                    },
                    "path": {
                        "type": "string",
                        "description": "搜索范围(相对工作区的文件或目录),省略则为整个工作区",
                    },
                    "ignore_case": {
                        "type": "boolean",
                        "description": "是否忽略大小写,默认 false",
                    },
                    "include": {
                        "type": "string",
                        "description": "只搜文件名匹配这些 glob 的文件,逗号分隔,如 \"*.py,*.pyi\";留空则所有文本文件都搜",
                    },
                },
                "required": ["pattern"],
            },
)
def grep_files(pattern: str, path: str = ".", ignore_case: bool = False,
               include: str = "") -> str:
    """按内容搜索工作区里的文本文件(正则),返回命中的文件、行号与行内容。

    逐行流式读取 —— 整本书、长日志这种大文件也照搜。按 utf-8 / gb18030 / big5 试解码,
    所以 GBK 编码的中文文件也能搜到。自动跳过隐藏目录与依赖目录,二进制文件(含空字节)跳过。
    include 可按文件名收窄范围(逗号分隔的 glob,如 "*.py" 或 "*.py,*.pyi"),留空则全搜。
    结果上限 FS_MAX_GREP_MATCHES 条,超出会提示截断。path 可以是文件或目录。
    """
    import fnmatch
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        return f"错误:正则表达式无效:{exc}"
    paths = [path] if isinstance(path, str) else list(path)
    if not paths:
        return "错误:没有给出搜索路径"
    if len(paths) > FS_MAX_LIST_DIRS:
        return f"错误:一次最多搜 {FS_MAX_LIST_DIRS} 个路径(收到 {len(paths)} 个),分批来吧"
    roots: list[Path] = []
    missing: list[str] = []
    for one in paths:
        r = safe_path(one)
        if r.exists():
            roots.append(r)
        else:
            missing.append(str(r))       # 写错一个不该让其余几个白搜
    if not roots:
        return f"错误:这些路径都不存在:{'、'.join(missing)}"

    # 多个路径合并成同一份 targets,并按路径去重 —— 范围重叠时同一个文件只搜一次
    targets: list[Path] = []
    seen_t: set[Path] = set()
    for r in roots:
        for p in ([r] if r.is_file() else _iter_search_paths(r)):
            if p.is_file() and p not in seen_t:
                seen_t.add(p)
                targets.append(p)

    if include.strip():
        pats = [s.strip().lower() for s in include.split(",") if s.strip()]
        targets = [p for p in targets if any(fnmatch.fnmatch(p.name.lower(), pat) for pat in pats)]
        if not targets:
            return f"没有文件名匹配「{include}」的文件(搜索范围:{'、'.join(_rel(r) for r in roots)})"

    results: list[tuple[Path, int, str]] = []
    files_hit: set[Path] = set()
    truncated = False
    too_big = 0  # 超过大小的文件数 —— 如实报出来,绝不静默跳过
    for fp in targets:
        if fp.name in FS_DENY_READ:
            continue
        try:
            if fp.stat().st_size > FS_MAX_GREP_FILE_BYTES:
                too_big += 1
                continue
            with fp.open("rb") as fh:
                head = fh.read(65536)
        except OSError:
            continue
        if not head:
            continue
        if head[:2] in (b"\xff\xfe", b"\xfe\xff"):   # UTF-16 BOM
            enc = "utf-16"
        elif b"\x00" in head[:8192]:                 # 含空字节 → 视为二进制,跳过
            continue
        else:
            enc = _detect_text_encoding(head)
        try:
            with fp.open("r", encoding=enc, errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    if rx.search(line):
                        results.append((fp, i, line.strip()[:FS_MAX_GREP_LINE_CHARS]))
                        files_hit.add(fp)
                        if len(results) >= FS_MAX_GREP_MATCHES:
                            truncated = True
                            break
        except OSError:
            continue
        if truncated:
            break

    skip_note = ""
    if too_big:
        skip_note = f";另有 {too_big} 个文件超过 {FS_MAX_GREP_FILE_BYTES // (1024 * 1024)}MB 未搜"
    if missing:
        skip_note += f";另有 {len(missing)} 个路径不存在,已跳过"
    if not results:
        return f"没有匹配「{pattern}」的内容(搜索范围:{'、'.join(_rel(r) for r in roots)})" + skip_note
    out: list[str] = []
    last: Path | None = None
    for fp, ln, txt in results:
        if fp != last:
            out.append(f"{_rel(fp)}:")
            last = fp
        out.append(f"  {ln}: {txt}")
    summary = f"共 {len(results)} 处命中,{len(files_hit)} 个文件"
    if truncated:
        summary += f"(已达上限 {FS_MAX_GREP_MATCHES},后续未继续)"
    return summary + skip_note + "\n" + "\n".join(out)


def _markdown_note(target: Path) -> str:
    """写完 .md 后自动查一遍格式,把问题附在结果里(没有就返回空串)。

    为什么放在写文件这条路上、而不是做成一个"想查就查"的工具:markdown 的格式问题
    (标题/列表/代码块前后少空行、代码块没标语言)是**一眼看不出**的 —— 写的人
    自我感觉良好,用户打开却满屏 lint 警告。**必须由系统在写的时候就拦一道**,指望
    模型记得主动去查是靠不住的(实测:提醒过也一样忘)。
    """
    if target.suffix.lower() != ".md":
        return ""
    issues = md.lint_file(target)
    if not issues:
        return "\n\nmarkdown 格式检查:通过。"
    shown = issues[:12]
    more = f"\n…(还有 {len(issues) - len(shown)} 处未列出)" if len(issues) > len(shown) else ""
    return ("\n\n! markdown 格式有问题,请**现在就改掉**(用户打开文件会看到 lint 警告):\n"
            + "\n".join(f"  {it}" for it in shown) + more)


def _check_writable(target: Path, content: str) -> int:
    """写入前的公共校验,返回内容的字节数。"""
    size = len(content.encode("utf-8"))
    if size > MAX_WRITE_BYTES:
        raise ValueError(f"内容过大({size} 字节),单次写入上限为 {MAX_WRITE_BYTES} 字节")
    if target.is_dir():
        raise IsADirectoryError(f"{target} 是一个目录,不能当作文件写入")
    return size


@tool(
    agents=("main", "sub"),
    description="把文本内容写入工作区内的文件,父目录不存在会自动创建。"
                "默认不允许覆盖已存在的文件:如果文件已存在,调用会失败并提示你,"
                "此时应当先征求用户同意,确认后再带 overwrite=true 重新调用。"
                "只想在文件末尾补充内容时,用 append_file 而不是本工具。",
    parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "目标文件路径(相对工作区),例如 notes/todo.md",
                    },
                    "content": {
                        "type": "string",
                        "description": "要写入的完整文本内容",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "是否允许覆盖已存在的文件,默认 false。覆盖不可撤销,务必先得到用户确认",
                    },
                },
                "required": ["path", "content"],
            },
)
def write_file(path: str, content: str, overwrite: bool = False) -> str:
    target = safe_path(path)
    size = _check_writable(target, content)

    # 覆盖是不可逆的,必须由模型显式声明意图,不能靠默认行为悄悄发生
    existed = target.is_file()
    if existed and not overwrite:
        return (
            f"错误:{target.name} 已存在({target.stat().st_size} 字节)。"
            f"确认要覆盖就带 overwrite=true 重新调用,否则换个文件名。"
        )

    target.parent.mkdir(parents=True, exist_ok=True)  # 子目录不存在就顺手建出来
    # newline="\n" 不能省:默认(None)在 Windows 上会把 \n 翻成 \r\n,于是 agent 写出来的
    # shell 脚本传进 VM 就以 CRLF 结尾,sh 会报 "illegal option -"(实测踩过 —— 当时还
    # 误以为是 vm_push 转换的,其实 vm_push 走 read_bytes+base64,字节透明)。
    # 三个写入点(这里、append_file、_save_lines)必须保持一致。
    target.write_text(content, encoding="utf-8", newline="\n")
    return f"已{'覆盖' if existed else '创建'} {target}({size} 字节)" + _markdown_note(target)


@tool(
    agents=("main", "sub"),
    description="在工作区内某个文件的末尾追加文本,文件不存在则自动创建。"
                "适合记日志、往清单里加条目这类场景,不会破坏已有内容。"
                "注意:本工具不会自动加换行,需要换行请在 content 里自己写 \\n。",
    parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "目标文件路径(相对工作区)",
                    },
                    "content": {
                        "type": "string",
                        "description": "要追加到文件末尾的文本",
                    },
                },
                "required": ["path", "content"],
            },
)
def append_file(path: str, content: str) -> str:
    target = safe_path(path)
    size = _check_writable(target, content)

    target.parent.mkdir(parents=True, exist_ok=True)
    # 追加要沿用原文件编码:否则会给 GBK 文件塞进 UTF-8 字节,把文件编码搞坏
    enc = _sniff_encoding(target) if target.is_file() else "utf-8"
    with target.open("a", encoding=enc, newline="\n") as fp:
        fp.write(content)
    return f"已向 {target} 追加 {size} 字节(当前共 {target.stat().st_size} 字节)" + _markdown_note(target)


def _load_lines(path: str) -> tuple[Path, list[str], str]:
    """按行读出文件,保留行尾换行符,供按行编辑的工具复用。返回 (路径, 行, 原编码)。"""
    target = safe_path(path)
    if target.name in FS_DENY_READ:
        raise PermissionError(f"{target.name} 属于敏感文件,禁止编辑")
    if not target.is_file():
        raise FileNotFoundError(f"{target} 不存在;要新建文件请用 write_file")
    text, enc = _read_text_with_encoding(target)
    return target, text.splitlines(keepends=True), enc


def _save_lines(target: Path, lines: list[str], summary: str, center: int,
                encoding: str = "utf-8") -> str:
    """写回并返回改动摘要 + 附近几行,让模型能自己确认改对没有。

    用文件**原来的编码**写回(不是一律 UTF-8),否则编辑一个 GBK 文件会把它悄悄转码。
    """
    text = "".join(lines)
    _check_writable(target, text)
    target.write_text(text, encoding=encoding, newline="\n")

    lo, hi = max(1, center - 3), min(len(lines), center + 3)
    preview = "\n".join(f"{i:>4} | {lines[i - 1].rstrip(chr(10))}" for i in range(lo, hi + 1))
    tail = f"\n改动附近的内容:\n{preview}" if preview else "\n(文件现在是空的)"
    return f"{summary},文件现共 {len(lines)} 行{tail}" + _markdown_note(target)


def _terminate(line: str) -> str:
    """补上缺失的行尾换行,否则插入的内容会和下一行粘成一行。"""
    return line if line.endswith("\n") else line + "\n"


@tool(
    agents=("main", "sub"),
    description="替换文件中指定行号区间的内容,只改这几行,文件其余部分原样保留。"
                "行号从 1 开始,start_line 和 end_line 都包含在内。"
                "把 content 留空('')就是删除这几行。"
                "改一两处内容时优先用本工具,不要用 write_file 整篇重写。"
                "调用前务必先用 read_file(with_line_numbers=true) 确认行号,不要凭记忆猜。",
    parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径(相对工作区)"},
                    "start_line": {
                        "type": "integer",
                        "description": "起始行号(从 1 开始,包含该行)",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "结束行号(包含该行);只改一行时填成和 start_line 相同",
                    },
                    "content": {
                        "type": "string",
                        "description": "替换进去的新内容,可以是多行;留空则删除这些行",
                    },
                },
                "required": ["path", "start_line", "end_line"],
            },
)
def edit_lines(path: str, start_line: int, end_line: int, content: str = "") -> str:
    target, lines, enc = _load_lines(path)
    total = len(lines)
    if not 1 <= start_line <= total or not start_line <= end_line <= total:
        raise ValueError(
            f"行号超出范围:文件共 {total} 行,收到 start_line={start_line}、end_line={end_line}。"
            f"请先用 read_file(with_line_numbers=true) 确认行号。"
        )

    new = content.splitlines(keepends=True) if content else []
    if new and end_line < total:  # 末行之外的替换必须以换行结尾,否则会粘住后一行
        new[-1] = _terminate(new[-1])

    replaced = end_line - start_line + 1
    updated = lines[:start_line - 1] + new + lines[end_line:]
    verb = "删除" if not new else "替换"
    summary = f"已{verb}第 {start_line}-{end_line} 行({replaced} 行 → {len(new)} 行)"
    return _save_lines(target, updated, summary, start_line, enc)


@tool(
    agents=("main", "sub"),
    description="在指定行之后插入新内容,不覆盖任何已有行。"
                "after_line=0 表示插入到文件最开头,after_line=5 表示插到第 5 行和第 6 行之间。"
                "只是往文件末尾补内容的话,用 append_file 更简单。",
    parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径(相对工作区)"},
                    "after_line": {
                        "type": "integer",
                        "description": "在这一行之后插入;0 表示插到文件最开头",
                    },
                    "content": {
                        "type": "string",
                        "description": "要插入的内容,可以是多行",
                    },
                },
                "required": ["path", "after_line", "content"],
            },
)
def insert_lines(path: str, after_line: int, content: str) -> str:
    target, lines, enc = _load_lines(path)
    total = len(lines)
    if not 0 <= after_line <= total:
        raise ValueError(
            f"after_line 必须在 0 到 {total} 之间(0 表示插入到文件最开头),收到 {after_line}。"
        )

    if after_line > 0:  # 插入点的前一行若缺换行,先补上
        lines[after_line - 1] = _terminate(lines[after_line - 1])
    new = content.splitlines(keepends=True)
    if new and after_line < total:
        new[-1] = _terminate(new[-1])

    updated = lines[:after_line] + new + lines[after_line:]
    summary = f"已在第 {after_line} 行之后插入 {len(new)} 行"
    return _save_lines(target, updated, summary, after_line + 1, enc)


def _move_one(source: str, destination: str, overwrite: bool) -> str:
    """移动/重命名单个文件或目录。返回一句结果说明;出错就抛(由调用方决定怎么处理)。"""
    src = safe_path(source)
    if not src.exists():
        raise FileNotFoundError(f"{src} 不存在")

    dest = safe_path(destination)
    # 目标是已存在的目录 -> 移动进去并沿用原名,与 shell 里 mv a.txt dir/ 的习惯一致
    if dest.is_dir() and dest != src:
        dest = dest / src.name

    # 源或目标都不能是 agent 的系统目录(.trash/.git/.agent/clones),否则能挪走
    # 回收站、版本库、记忆,把整个 agent 搞致残 —— move_file 之前漏了这个检查。
    if is_system_dir(src) or is_system_dir(dest):
        raise PermissionError("不允许移动或重命名 agent 的系统目录(.trash/.git/.agent/clones)。")

    if src == dest:
        return f"错误:源路径和目标路径相同({src}),无需移动"
    if src.is_dir() and dest.is_relative_to(src):
        raise ValueError(f"不能把目录 {src.name} 移动到它自己内部:{dest}")

    existed = dest.exists()
    if existed:
        if dest.is_dir():
            raise IsADirectoryError(f"目标 {dest} 是已存在的目录,不能覆盖")
        if not overwrite:
            return (
                f"错误:目标 {dest.name} 已存在({dest.stat().st_size} 字节)。"
                f"确认要覆盖就带 overwrite=true 重新调用,否则换个目标名。"
            )

    kind = "目录" if src.is_dir() else "文件"  # 移动之后 src 就不在了,先取
    dest.parent.mkdir(parents=True, exist_ok=True)
    src.replace(dest)  # 用 replace 不用 rename:Windows 上 rename 遇到已存在的目标会失败

    if existed:
        action = "覆盖并移动"
    elif src.parent == dest.parent:
        action = "重命名"
    else:
        action = "移动"
    return f"已{action}{kind}:{src} -> {dest}"


@tool(
    agents=("main", "sub"),
    description="移动或重命名工作区内的文件、目录。同一目录内换个名字就是重命名,换到别的目录就是移动。"
                "**source 可以给数组,一次移多个**(上限 20 个)—— 这时候 destination 要是个**目录**"
                "(它们各自沿用原名移进去),目录不存在会自动创建;目标若是个已存在的文件则报错,"
                "目标的父目录不存在会自动创建;目标是一个已存在的目录时,会把源移动进去并沿用原名。"
                "默认不允许覆盖已存在的目标文件,需要覆盖时先征求用户同意再带 overwrite=true 重试。",
    parameters={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "源文件或目录的路径(相对工作区);可给数组一次移多个,"
                                       "如 [\"a.py\", \"b.py\"];单个也可以直接给字符串",
                    },
                    "destination": {
                        "type": "string",
                        "description": "目标路径(相对工作区)。移多个时必须是已存在的目录;"
                                       "移单个时可以是新文件名,也可以是已存在的目录",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "是否允许覆盖已存在的目标文件,默认 false。覆盖不可撤销,务必先得到用户确认",
                    },
                },
                "required": ["source", "destination"],
            },
)
def move_file(source, destination: str, overwrite: bool = False) -> str:
    """移动或重命名工作区里的文件/目录。source 可以给数组,一次移多个。

    多个源的时候 destination 必须是**已存在的目录** —— 几个文件挤到同一个新名字上没有
    意义,而"移进某个目录"能推出来。单个源时保持原行为(目标可以是新名字,也可以是目录)。
    """
    sources = [source] if isinstance(source, str) else list(source)
    if not sources:
        return "错误:没有给出要移动的源"
    if len(sources) > FS_MAX_MOVE_FILES:
        return (f"错误:一次最多移动 {FS_MAX_MOVE_FILES} 个(收到 {len(sources)} 个),分批来吧")

    if len(sources) == 1:
        return _move_one(sources[0], destination, overwrite)

    dest = safe_path(destination)
    created = ""
    if not dest.is_dir():
        if dest.exists():
            return (f"错误:一次移多个时 destination 得是目录,而 {dest} 是个已存在的文件 —— "
                    f"它们没法共用一个文件名。换个目录名,或者分开移。")
        # 不存在就建出来。单文件时本来就会自动建父目录,多文件时建"目标目录"是同一个道理,
        # 不该反过来卡住。但返回里要明说建了什么 —— 万一模型把 out.txt 当目录传进来,
        # 它得看见自己凭空造了个目录,而不是以为文件被移进去了。
        try:
            dest.mkdir(parents=True)
            created = f"(目标目录 {dest} 原先不存在,已创建)\n"
        except OSError as exc:
            return f"错误:创建目标目录 {dest} 失败:{exc}"

    ok, bad = [], []
    for s in sources:
        try:
            ok.append(_move_one(s, destination, overwrite))
        except Exception as exc:  # noqa: BLE001 - 一个移不动不该拖垮整批
            bad.append(f"  {s}: {type(exc).__name__}: {exc}")
    lines = [f"已处理 {len(ok)} 个:"]
    lines += [f"  {x}" for x in ok]
    if bad:
        lines += [f"失败 {len(bad)} 个:"] + bad
    return created + "\n".join(lines)
