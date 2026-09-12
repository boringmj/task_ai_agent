from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from ..core import (
    CLONES_DIR,
    GIT_DIR,
    MEMORY_FILE,
    ROOT,
    TRASH_DIR,
    TRASH_INDEX_FILE,
    safe_path,
)

def delete_file(path: str) -> str:
    target = safe_path(path)
    if not target.exists():
        raise FileNotFoundError(f"{target} 不存在")
    if target.is_dir():
        raise IsADirectoryError(f"{target} 是目录,本工具只删除单个文件")
    if TRASH_DIR == target.parent:
        raise PermissionError("该文件已在回收站中,不能重复删除")

    # 软删除:移进回收站而不是真删,给用户留后悔的余地
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = TRASH_DIR / f"{target.stem}.{stamp}{target.suffix}"
    seq = 1
    while dest.exists():  # 同一秒内删了同名文件
        dest = TRASH_DIR / f"{target.stem}.{stamp}-{seq}{target.suffix}"
        seq += 1

    target.rename(dest)
    _trash_record(dest, target)  # 记下原路径,否则没法还原到原位置
    return f"已把 {target.name} 移入回收站:{dest}(可用 restore_file 还原)"


# agent 赖以运作、绝不能删/挪的目录。比"只在递归时检查"严格 —— 任何时候都不许碰
PROTECTED_DIRS = (ROOT, TRASH_DIR, GIT_DIR, MEMORY_FILE.parent, CLONES_DIR)


def _is_system_dir(path: Path) -> bool:
    """判断一个路径是否是(或位于)agent 的系统目录内。

    注意不能拿 is_relative_to(ROOT) 来判 —— 工作区里每个普通文件都在 ROOT 下,
    那样会误挡。所以 ROOT 单独用相等判断,其余系统目录用"等于或位于其内"判断。
    """
    if path == ROOT:
        return True
    return any(path == p or path.is_relative_to(p) for p in PROTECTED_DIRS if p != ROOT)


def _trash_dest(name: str) -> Path:
    """生成回收站里的唯一目标名,时间戳冲突时加序号。delete_file / delete_dir 共用。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = TRASH_DIR / f"{name}.{stamp}"
    seq = 1
    while dest.exists():
        dest = TRASH_DIR / f"{name}.{stamp}-{seq}"
        seq += 1
    return dest


def delete_dir(path: str, recursive: bool = False) -> str:
    """删除工作区里的一个目录(软删除,移入 .trash/)。

    目录必须为空,才能直接删;非空目录需要 recursive=true 显式确认。
    工作区本身、agent 的系统目录(.trash/.git/.agent)一律拒绝,防止自毁。
    """
    target = safe_path(path)
    if not target.exists():
        raise FileNotFoundError(f"{target} 不存在")
    if not target.is_dir():
        raise IsADirectoryError(f"{target} 不是目录,删单个文件请用 delete_file")

    # 用 resolve() 展开后比较,防止 .. 拼出假保护路径。系统目录一律拒绝(含 clones/)
    resolved = (ROOT / path).resolve()
    if resolved == ROOT:
        raise PermissionError("不允许删除工作区根目录")
    if _is_system_dir(resolved):
        raise PermissionError(f"{target} 是 agent 的系统目录(.trash/.git/.agent/clones),不允许删除。")

    if not recursive:
        if any(target.iterdir()):
            return (
                f"{target} 不是空目录,直接删除会连带删除里面的一切。"
                f"确认要删除就带 recursive=true 重新调用。"
            )

    dest = _trash_dest(target.name)
    target.rename(dest)
    _trash_record(dest, target)
    depth = "递归" if recursive else "空"
    return f"已把{depth}目录 {target.name} 移入回收站:{dest}(可用 restore_file 还原)"


# ---------------- 回收站:还原 / 清空 / 自动清理 ----------------

# 回收站条目的命名是 {原名}.{YYYYMMDD-HHMMSS}[-NN][后缀]。目录没有后缀,
# 所以把 .{后缀} 设成可选的 —— 否则目录的时间戳解析不出来,进了回收站就出不来。
_TRASH_TIME_RE = re.compile(r"\.(\d{8}-\d{6})(?:-\d+)?(?:\.[^.]*)?$")


def _trash_index() -> dict[str, str]:
    """读回 <回收站文件名> -> <原相对路径> 的映射。索引坏了就当作没有,别因此崩掉。"""
    index: dict[str, str] = {}
    if TRASH_INDEX_FILE.exists():
        for line in TRASH_INDEX_FILE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                index[entry["trash"]] = entry["original"]
            except (json.JSONDecodeError, KeyError):
                continue  # 单条损坏不影响整体
    return index


def _trash_record(trash_path: Path, original: Path) -> None:
    """把一次删除记进索引:回收站文件名 -> 原路径(相对工作区)。"""
    index = _trash_index()
    index.pop(trash_path.name, None)  # 同一名可能被删过多次,后进为准
    try:
        index[trash_path.name] = str(original.relative_to(ROOT))
    except ValueError:
        index[trash_path.name] = str(original)  # 理论上不该发生,兜底存绝对路径
    _trash_save(index)


def _trash_save(index: dict[str, str]) -> None:
    lines = [json.dumps({"trash": k, "original": v}, ensure_ascii=False) for k, v in index.items()]
    TRASH_INDEX_FILE.write_text("\n".join(lines), encoding="utf-8")


def _trash_original_of(trash_path: Path) -> Path:
    """按索引还原原路径;索引缺失时从文件名挽回(stem 是原文件名)。"""
    original_rel = _trash_index().get(trash_path.name)
    if original_rel:
        return (ROOT / original_rel).resolve()
    match = _TRASH_TIME_RE.search(trash_path.name)
    if match:
        stem = trash_path.name[: match.start()]
        return (ROOT / stem).resolve()
    return ROOT / trash_path.stem


def _trash_timestamp(trash_path: Path) -> datetime | None:
    """从回收站文件名解析删除时刻,用于判断是否过期。解析不出来返回 None。"""
    match = _TRASH_TIME_RE.search(trash_path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def restore_file(trashed_name: str, overwrite: bool = False) -> str:
    """把回收站里的文件还原到它的原路径。

    还原前必须检查原位置是否已有文件 —— 直接覆盖会丢掉用户当前的内容,
    这是不可逆的,所以要像 write_file 覆盖那样要求显式声明意图。
    """
    trash_path = safe_path(TRASH_DIR / trashed_name)
    if not trash_path.exists():  # 目录和文件都能还原,不能只看 is_file()
        raise FileNotFoundError(f"回收站里没有 {trashed_name},可先 list_files(.trash, show_hidden=true) 看看")

    original = _trash_original_of(trash_path)
    if original.exists():
        if original.is_dir():
            return (
                f"原位置 {original} 现在是一个目录,不能覆盖。"
                f"如果确定旧文件不要了,请先删除或移走当前目录,再还原。"
            )
        if not overwrite:
            return (
                f"原位置 {original} 已有同名文件,直接覆盖会丢掉它当前的内容。"
                f"确认要覆盖,就带 overwrite=true 重新调用。"
            )

    # 先移出来再删索引:如果这一步异常了,索引还在,还能再试一次
    original.parent.mkdir(parents=True, exist_ok=True)
    trash_path.replace(original)
    index = _trash_index()
    index.pop(trashed_name, None)
    _trash_save(index)
    return f"已还原 {trashed_name} -> {original}"


def purge_trash(max_age_days: int | None = None, name: str | None = None) -> str:
    """永久删除回收站里的内容(**不可恢复**)。

    name 给定 -> 只永久删除回收站里这一项(名字取自 list_files(".trash", show_hidden=true));
    max_age_days 给定 -> 只删超过该天数的(启动自动清理用);
    两者都不给 -> 清空全部,不设保留规则。
    """
    # 只删指定的一项
    if name:
        target = safe_path(TRASH_DIR / name)
        if target == TRASH_DIR or not target.is_relative_to(TRASH_DIR):
            return f"错误:{name} 不在回收站里。"
        if target == TRASH_INDEX_FILE:
            return "错误:那是回收站的索引文件,不能删。"
        if not target.exists():
            return f'回收站里没有 {name};可先 list_files(".trash", show_hidden=true) 看看。'
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        index = _trash_index()
        index.pop(target.name, None)
        _trash_save(index)
        return f"已从回收站永久删除 {name}(不可恢复)。"

    now = datetime.now()
    index = _trash_index()
    # 先把孤儿条目清掉:索引里指向的回收站文件已不在磁盘(例如被手动删了、
    # 或上次清除时中断没来得及改索引)。这类条目永远不会被下面的循环碰到
    # (循环只遍历磁盘上的文件),会无限残留 —— 所以在这里主动剔除。
    index = {name: orig for name, orig in index.items() if (TRASH_DIR / name).exists()}
    removed, kept = 0, 0

    for item in TRASH_DIR.iterdir():
        if item.name == TRASH_INDEX_FILE.name:
            continue

        should_remove = False
        if max_age_days is None:
            should_remove = True  # 显式清空全部
        else:
            ts = _trash_timestamp(item)
            # 只在能确认过期时才删;解析不出时间的文件宁可保留,避免误删
            should_remove = ts is not None and (now - ts).days >= max_age_days

        if should_remove:
            # 目录和文件都要能删 —— delete_dir 会把目录放进回收站,不能只处理文件
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
            index.pop(item.name, None)
            removed += 1
        else:
            kept += 1
    _trash_save(index)

    note = "全部" if max_age_days is None else f"超过 {max_age_days} 天的"
    return f"已永久删除回收站{note}文件 {removed} 个,保留 {kept} 个。"
