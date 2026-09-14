from __future__ import annotations

from .registry import tool

import json
import os
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
    is_system_dir,
    safe_path,
)


# ---- trash 工具专属配置(环境变量名不变,仍可在 .env 覆盖)----
# 回收站:删除的文件移到这里,不做真删除,超过 MAX_AGE_DAYS 天后启动时自动清空
TRASH_DIR.mkdir(exist_ok=True)  # 回收站目录由本模块自己保证存在
TRASH_INDEX_FILE = TRASH_DIR / "index.json"  # 记录 回收站文件名 -> 原路径,支撑还原
# 批量删的上限。删除是破坏性操作,一次几十个既难复核、也容易误伤 —— 20 个够用,超了就
# 该让模型分批、把每一批都摆给用户看清楚。
TRASH_MAX_BATCH = int(os.environ.get("TRASH_MAX_BATCH", "20"))

def _delete_one(path: str) -> str:
    """把一个文件移进回收站,返回 "原名 → 回收站名"。出错就抛(由调用方决定怎么处理)。"""
    target = safe_path(path, "delete")
    if not target.exists():
        raise FileNotFoundError(f"{target} 不存在")
    if target.is_dir():
        raise IsADirectoryError(f"{target} 是目录,本工具只删除单个文件")
    if TRASH_DIR == target.parent:
        raise PermissionError("该文件已在回收站中,不能重复删除")
    # 系统目录里的**文件**同样不能挪走 —— 否则 .git/config、.agent/memory.md
    # 会被"软删"进回收站,仓库和长期记忆当场失效。delete_dir / move_file 都挡了,
    # 这里之前是漏的。
    if is_system_dir(target):
        raise PermissionError(
            f"{target} 在 agent 的系统目录(.trash/.git/.agent/clones)内,不允许删除。"
        )

    # 软删除:移进回收站而不是真删,给用户留后悔的余地
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = TRASH_DIR / f"{target.stem}.{stamp}{target.suffix}"
    seq = 1
    while dest.exists():  # 同一秒内删了同名文件
        dest = TRASH_DIR / f"{target.stem}.{stamp}-{seq}{target.suffix}"
        seq += 1

    target.rename(dest)
    _trash_record(dest, target)  # 记下原路径,否则没法还原到原位置
    return f"{target.name} → {dest.name}"


@tool(
    description="删除工作区内的文件 —— 实际是移入 .trash/ 回收站(可用 restore_file 还原),不是物理删除。"
                "**path 可以给数组,一次删多个**(上限 20 个)。"
                "删除是破坏性操作:调用前必须取得用户明确同意,不要自作主张;批量删更是如此 —— "
                "把要删的列清楚给用户看过再动手。本工具不能删目录。",
    parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要删除的文件路径(相对工作区),可给数组一次删多个,"
                                       "如 [\"a.py\", \"b.py\"];单个也可以直接给字符串",
                    }
                },
                "required": ["path"],
            },
)
def delete_file(path) -> str:
    """删除工作区内的一个或多个文件(都走回收站,可还原)。

    path 接受数组,一次删一批。单个文件时行为与从前一致(出错直接抛,调用方拿得到
    异常类型);多个文件时改成"逐条收集" —— 一个失败不该让后面的全不删。
    """
    paths = [path] if isinstance(path, str) else list(path)
    if not paths:
        return "错误:没有给出要删除的文件"
    if len(paths) > TRASH_MAX_BATCH:
        return (f"错误:一次最多删 {TRASH_MAX_BATCH} 个(收到 {len(paths)} 个),分批删吧")

    if len(paths) == 1:
        done = _delete_one(paths[0])
        return f"已把 {done.split(' → ')[0]} 移入回收站(可用 restore_file 还原)"

    ok, bad = [], []
    for p in paths:
        try:
            ok.append(_delete_one(p))
        except Exception as exc:  # noqa: BLE001 - 一个删不掉不该拖垮整批
            bad.append(f"  {p}: {type(exc).__name__}: {exc}")
    lines = [f"已移入回收站 {len(ok)} 个(可用 restore_file 还原):"]
    lines += [f"  {x}" for x in ok]
    if bad:
        lines += [f"失败 {len(bad)} 个:"] + bad
    return "\n".join(lines)


def _trash_dest(name: str) -> Path:
    """生成回收站里的唯一目标名,时间戳冲突时加序号。delete_file / delete_dir 共用。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = TRASH_DIR / f"{name}.{stamp}"
    seq = 1
    while dest.exists():
        dest = TRASH_DIR / f"{name}.{stamp}-{seq}"
        seq += 1
    return dest


@tool(
    description="删除工作区里的一个目录,实际是移入 .trash/ 回收站而非物理删除。"
                "目录必须是空的才能直接删;非空目录要带 recursive=true 显式确认,表示同意连带删除里面的所有内容。"
                "工作区根目录以及 .trash/.git/.agent 这些系统目录一律拒绝,防止 agent 自毁。"
                "删除是破坏性操作:调用前必须先取得用户明确同意。只删单个文件请用 delete_file。",
    parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要删除的目录路径(相对工作区)",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "目录非空时,是否同意连带删除其中所有内容,默认 false",
                    },
                },
                "required": ["path"],
            },
)
def delete_dir(path: str, recursive: bool = False) -> str:
    """删除工作区里的一个目录(软删除,移入 .trash/)。

    目录必须为空,才能直接删;非空目录需要 recursive=true 显式确认。
    工作区本身、agent 的系统目录(.trash/.git/.agent)一律拒绝,防止自毁。
    """
    target = safe_path(path, "delete")
    if not target.exists():
        raise FileNotFoundError(f"{target} 不存在")
    if not target.is_dir():
        raise IsADirectoryError(f"{target} 不是目录,删单个文件请用 delete_file")

    # 用 resolve() 展开后比较,防止 .. 拼出假保护路径。系统目录一律拒绝(含 clones/)
    resolved = (ROOT / path).resolve()
    if resolved == ROOT:
        raise PermissionError("不允许删除工作区根目录")
    if is_system_dir(resolved):
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


@tool(
    description="把回收站里的文件还原到它原来的路径。**trashed_name 可以给数组,一次还原多个**"
                "(误删一批时就该一次全捞回来)。还原前会先检查原位置是否有同名文件:"
                "如果原位置已被占用,调用会失败并告诉你,此时应当先征求用户同意再带 overwrite=true 重试。"
                "先用 list_files(path=\".trash\", show_hidden=true) 找到回收站里的确切文件名再还原。",
    parameters={
                "type": "object",
                "properties": {
                    "trashed_name": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "回收站里那个文件的名字(含时间戳后缀),**不是原路径**;"
                                       "可给数组一次还原多个,如 [\"a.20260914-025003.txt\"];"
                                       "单个也可以直接给字符串",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "原位置已被同名文件占用时,是否允许覆盖。覆盖不可逆,务必先得到用户确认",
                    },
                },
                "required": ["trashed_name"],
            },
)
def restore_file(trashed_name, overwrite: bool = False) -> str:
    """把回收站里的文件还原到它的原路径。trashed_name 可以给数组,一次还原多个。

    还原是**补救**动作,批量做很自然 —— 误删了一批,本来就该一次全捞回来。单个时行为
    与从前一致(出错直接抛);多个时逐条收集,一个失败不挡其余。
    """
    names = [trashed_name] if isinstance(trashed_name, str) else list(trashed_name)
    if not names:
        return "错误:没有给出要还原的项"
    if len(names) > TRASH_MAX_BATCH:
        return f"错误:一次最多还原 {TRASH_MAX_BATCH} 个(收到 {len(names)} 个),分批来吧"
    if len(names) == 1:
        return _restore_one(names[0], overwrite)

    ok, bad = [], []
    for n in names:
        try:
            ok.append(_restore_one(n, overwrite))
        except Exception as exc:  # noqa: BLE001 - 一个还原不了不该拖垮整批
            bad.append(f"  {n}: {type(exc).__name__}: {exc}")
    lines = [f"已还原 {len(ok)} 个:"] + [f"  {x}" for x in ok]
    if bad:
        lines += [f"失败 {len(bad)} 个:"] + bad
    return "\n".join(lines)


def _restore_one(trashed_name: str, overwrite: bool) -> str:
    """把一个回收站项还原回原路径(内核)。出错就抛,由调用方决定怎么处理。

    还原前必须检查原位置是否已有文件 —— 直接覆盖会丢掉用户当前的内容,那是不可逆的,
    所以要像 write_file 覆盖那样要求显式声明意图。
    """
    trash_path = safe_path(TRASH_DIR / trashed_name, "read")
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

    # **还原也是往工作区里写东西**,得照样过一遍授权。这条路径是从回收站索引里读回来的
    # (`_trash_original_of`),没经过 safe_path,所以要单独补一次 —— 不补的话"还原"就是
    # 绕开写权限的后门:不能写?删掉再还原,一样能落地。
    try:
        rel = original.relative_to(ROOT).as_posix()
    except ValueError:
        raise PermissionError(
            f"回收站里记的原位置不在工作区内:{original} —— 拒绝还原。"
        ) from None
    safe_path(rel, "write")
    # 先移出来再删索引:如果这一步异常了,索引还在,还能再试一次
    original.parent.mkdir(parents=True, exist_ok=True)
    trash_path.replace(original)
    index = _trash_index()
    index.pop(trashed_name, None)
    _trash_save(index)
    return f"已还原 {trashed_name} -> {original}"


@tool(
    description="永久删除回收站里的内容(**不可恢复**)。给 name 就只删回收站里的那一项(不必清空全部);"
                "不给 name 则清空全部,过期的文件本来也会在启动时自动清理。"
                "回收站里有哪些项,用 list_files(\".trash\", show_hidden=true) 看。"
                "永久删除不可恢复,调用前务必先让用户确认不再需要,不要自作主张。",
    parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "要永久删除的回收站项名(只删这一项);不填则清空全部",
                    },
                    "max_age_days": {
                        "type": "integer",
                        "description": "只删除超过这个天数的文件(用于启动清理);不填则清空回收站",
                    },
                },
            },
)
def purge_trash(max_age_days: int | None = None, name: str | None = None) -> str:
    """永久删除回收站里的内容(**不可恢复**)。

    name 给定 -> 只永久删除回收站里这一项(名字取自 list_files(".trash", show_hidden=true));
    max_age_days 给定 -> 只删超过该天数的(启动自动清理用);
    两者都不给 -> 清空全部,不设保留规则。
    """
    # 只删指定的一项
    if name:
        target = safe_path(TRASH_DIR / name, "delete")
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
