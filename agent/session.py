"""会话持久化:一个会话一个目录,id 随机生成、全局唯一。

会话是**隔离的单元**:每个会话有自己的对话历史、自己的虚拟机磁盘。目录在项目根下
(不在工作区里 —— 会话记录与 VM 磁盘都不该让 agent 读改删):

    sessions/
      index.json            索引:哪个工作区有哪些会话、最后跑的是哪个
      <随机 id>/
        messages.jsonl      对话历史,一行一条消息(第一行是元信息)
        owner.json          占用者(现在只用来提醒冲突,将来可升级成锁)
        work-<id>.qcow2     该会话专属的虚拟机磁盘(装过的软件、写过的文件都在这)
        qemu-<id>.log       QEMU 的输出,排查启动失败用

索引把"工作区"和"会话"关联起来,于是:**同一个工作区可以有多段互不干扰的会话**,
启动时默认接回**最后跑过的那一个**;将来要切换活跃会话,改索引里的 `last` 再重新
加载即可(见 set_current_session / list_sessions)。

为什么用 `.jsonl` 而不是 `.json`:会话是**每轮追加**的,而崩溃/强杀是常态。
`.json` 整个文件是一个文档,追加要读全文再整体重写,且**写一半被打断就全废**
(JSON 不完整 = 一个字符都解析不出来);`.jsonl` 一行一个对象,追加就是写一行,
崩了最多丢最后一行,前面照读。

设计上刻意**不设体积上限、不截断旧消息**:`messages` 已经被模型上下文上限和
90% 自动压缩约束住,文件天然有界;人为截断反而会丢历史、并把前缀缓存整段打掉。
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from .core import PROJECT_DIR, ROOT

# 会话根目录。默认在项目根(不在工作区里):会话记录与虚拟机磁盘既不该被 agent
# 读写,也不该混进工作区文件列表。多实例要用不同位置时用 AGENT_SESSIONS_DIR 覆盖。
SESSIONS_DIR = Path(
    os.environ.get("AGENT_SESSIONS_DIR") or (PROJECT_DIR / "sessions")
).resolve()
INDEX_FILE = SESSIONS_DIR / "index.json"
_INDEX_VERSION = 1
_FORMAT_VERSION = 1

# 写入用 UTF-8;消息里可能有中文与 base64 图片,别让编码出岔子
_ENCODING = "utf-8"

_current_session: str | None = None      # 本进程的活跃会话,首次解析后缓存


def _persistable(messages: list[dict]) -> list[dict]:
    """落盘前过滤掉**开头连续的** system 消息。

    开头那几条(系统提示词、长期记忆、指令清单)每次启动都重新生成,存下来既是
    浪费(约 11KB)又会让过期版本一直跟着会话跑。**但中段的 system 消息要留下** ——
    那不是每次重生的模板,而是程序在某个时刻插进对话的真实内容(例如"本次会话
    是恢复的"这条提示)。这也和 load_session 只剥离开头 system 的做法对称。
    """
    start = 0
    while start < len(messages) and messages[start].get("role") == "system":
        start += 1
    return messages[start:]


def _safe_name(sid: str) -> str:
    """会话 id 只允许简单字符,免得拼出目录之外的路径。"""
    return "".join(c for c in sid if c.isalnum() or c in "-_") or "invalid"


def new_session_id() -> str:
    """生成一个新的会话 id。随机、足够长,正常使用不会撞(48 位)。"""
    return uuid.uuid4().hex[:12]


def session_dir(sid: str) -> Path:
    """某个会话的目录 —— 它的历史、占用者、虚拟机磁盘都在这下面。

    虚拟机工具的磁盘路径也取自这里,所以"一个会话 = 一个目录 + 一台自己的 VM"。
    """
    return SESSIONS_DIR / _safe_name(sid)


def session_file(sid: str) -> Path:
    """对话历史文件。放在会话目录里,目录名已表明是哪个会话,所以文件名不用重复。"""
    return session_dir(sid) / "messages.jsonl"


def owner_file(sid: str) -> Path:
    return session_dir(sid) / "owner.json"


# ---------------- 索引:工作区 ↔ 会话 ----------------


def _workspace_key() -> str:
    """标识"哪个工作区"。用工作区的绝对路径 —— 索引要回答的是
    「这个工作区里以前跑过哪几段会话」。"""
    return str(ROOT.resolve())


def _index_load() -> dict:
    """读索引。坏了或不存在都当成空索引,不能让一个坏文件挡住启动。"""
    try:
        data = json.loads(INDEX_FILE.read_text(encoding=_ENCODING) or "{}")
        if isinstance(data, dict) and isinstance(data.get("workspaces"), dict):
            return data
    except Exception:  # noqa: BLE001
        pass
    return {"version": _INDEX_VERSION, "workspaces": {}}


def _index_save(index: dict) -> None:
    """原子写索引(临时文件 + 替换),免得写一半崩掉把索引搞坏。"""
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = INDEX_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding=_ENCODING)
        os.replace(tmp, INDEX_FILE)
    except Exception:  # noqa: BLE001
        pass


def register_session(sid: str, set_last: bool = True) -> None:
    """把会话登记到当前工作区名下(幂等)。"""
    now = datetime.now().isoformat(timespec="seconds")
    index = _index_load()
    entry = index["workspaces"].setdefault(_workspace_key(), {"last": None, "sessions": {}})
    session = entry["sessions"].setdefault(sid, {"created": now})
    session["last_used"] = now
    if set_last:
        entry["last"] = sid
    _index_save(index)


def set_current_session(sid: str) -> None:
    """切换活跃会话(将来供指令使用)。只改内存与索引指向,不动历史内容。"""
    global _current_session
    _current_session = _safe_name(sid)
    register_session(_current_session)


def current_session_id() -> str:
    """本进程的活跃会话 id:接回本工作区**最后跑过的那一个**;没有就新建一个。

    首次调用时解析并缓存 —— 会话 id 一旦定下来,整个进程都用它(虚拟机磁盘路径等
    都由它推导),中途换意味着连 VM 都要换,那是"切换会话"指令才做的事。
    """
    global _current_session
    if _current_session is not None:
        return _current_session

    entry = _index_load()["workspaces"].get(_workspace_key()) or {}
    last = entry.get("last")
    if last and session_dir(last).exists():
        _current_session = _safe_name(last)
        register_session(_current_session)          # 顺手更新 last_used
    else:
        _current_session = new_session_id()
        register_session(_current_session)
    return _current_session


def list_sessions() -> list[dict]:
    """本工作区名下的会话,最近使用的排在前面。供列表/切换类指令使用。"""
    entry = _index_load()["workspaces"].get(_workspace_key()) or {}
    sessions = entry.get("sessions") or {}
    out = []
    for sid, meta in sessions.items():
        exists = session_dir(sid).exists()
        out.append({
            "id": sid,
            "created": (meta or {}).get("created", "?"),
            "last_used": (meta or {}).get("last_used", "?"),
            "active": sid == _current_session,
            "missing": not exists,      # 目录被手工删了,但索引还记着
        })
    out.sort(key=lambda s: s["last_used"], reverse=True)
    return out


def _valid_prefix(messages: list[dict]) -> list[dict]:
    """截出"可以安全发给 API"的最长前缀。

    带 tool_calls 的 assistant 必须被后续 tool 消息**完整覆盖** —— 崩溃或中断可能
    正好留下半截,那种历史发给 API 会直接 400。发现不满足就从那条起截断。
    """
    out: list[dict] = []
    i, n = 0, len(messages)
    while i < n:
        m = messages[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            need = {tc.get("id") for tc in m["tool_calls"]}
            j = i + 1
            got = set()
            while j < n and messages[j].get("role") == "tool":
                got.add(messages[j].get("tool_call_id"))
                j += 1
            if not need <= got:          # 结果不全(或压根没写) → 到此为止
                break
            out.append(m)
            out.extend(messages[i + 1:j])
            i = j
        else:
            out.append(m)
            i += 1
    return out


def load_session(sid: str) -> tuple[list[dict], str]:
    """读回上次的对话。返回 (消息列表, 说明)。

    开头的 system 消息**不返回** —— 系统提示词和长期记忆每次启动都重新生成,
    沿用旧的那份等于把过期提示词冻在会话里。调用方自己补上最新的。
    """
    path = session_file(sid)
    if not path.exists():
        return [], "无历史会话(这是第一次)"

    messages: list[dict] = []
    broken = 0
    try:
        with path.open("r", encoding=_ENCODING) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    broken += 1        # 崩溃留下的半行,跳过
                    continue
                if isinstance(obj, dict) and "_meta" in obj:
                    continue           # 元信息行
                if isinstance(obj, dict) and "role" in obj:
                    messages.append(obj)
    except OSError as exc:
        return [], f"读取会话失败({exc}),按新会话开始"

    # 去掉开头连续的 system 消息(由调用方用最新的提示词重新注入)
    start = 0
    while start < len(messages) and messages[start].get("role") == "system":
        start += 1
    convo = messages[start:]

    kept = _valid_prefix(convo)
    dropped = len(convo) - len(kept)
    note = f"已恢复 {len(kept)} 条消息"
    if dropped:
        note += f"(丢弃了 {dropped} 条不完整的历史)"
    if broken:
        note += f";另有 {broken} 行损坏已跳过"
    return kept, note


def append_messages(new_messages: list[dict], sid: str) -> None:
    """把新产生的消息追加到会话文件末尾。写失败不抛 —— 存不下不该打断对话。"""
    if not new_messages:
        return
    try:
        session_dir(sid).mkdir(parents=True, exist_ok=True)
        path = session_file(sid)
        fresh = not path.exists()
        with path.open("a", encoding=_ENCODING) as fh:
            if fresh:
                fh.write(json.dumps(
                    {"_meta": {"version": _FORMAT_VERSION,
                               "created": datetime.now().isoformat(timespec="seconds")}},
                    ensure_ascii=False) + "\n")
            for m in _persistable(new_messages):
                fh.write(json.dumps(m, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())      # 崩了也别丢这一轮
    except Exception:  # noqa: BLE001 - 持久化失败不该影响对话
        pass


def rewrite_session(all_messages: list[dict], sid: str) -> None:
    """整体重写会话(压缩把历史换掉之后用)。先写临时文件再原子替换,避免写一半崩掉。"""
    try:
        session_dir(sid).mkdir(parents=True, exist_ok=True)
        path = session_file(sid)
        tmp = path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding=_ENCODING) as fh:
            fh.write(json.dumps(
                {"_meta": {"version": _FORMAT_VERSION,
                           "rewritten": datetime.now().isoformat(timespec="seconds")}},
                ensure_ascii=False) + "\n")
            for m in _persistable(all_messages):
                fh.write(json.dumps(m, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


# ---------------- 占用者(现在只用于提醒,将来可升级成锁)----------------


def _pid_alive(pid: int) -> bool:
    """进程是否还活着。Windows 上**不能**用 os.kill(pid, 0) —— 那会真的去杀进程。"""
    if pid <= 0:
        return False
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k = ctypes.windll.kernel32
        handle = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        k.CloseHandle(handle)
        return True
    except Exception:  # noqa: BLE001
        return True          # 查不出来就当它活着,宁可多提醒一次


def claim_owner(sid: str) -> str:
    """登记本实例为占用者;若发现另一个活着的实例也在用,返回提醒文案(空串表示没冲突)。"""
    warning = ""
    path = owner_file(sid)
    try:
        if path.exists():
            info = json.loads(path.read_text(encoding=_ENCODING) or "{}")
            other = int(info.get("pid") or 0)
            if other and other != os.getpid() and _pid_alive(other):
                since = info.get("started", "?")
                warning = (
                    f"⚠ 这个会话正被另一个 agent 实例占用(pid {other},启动于 {since})。"
                    f"两边会往同一个会话文件里写、还会共用同一块虚拟机磁盘,"
                    f"历史可能互相覆盖、虚拟机也可能互相踩;"
                    f"建议给另一个实例设不同的 AGENT_SESSIONS_DIR。"
                )
    except Exception:  # noqa: BLE001 - 占用者文件坏了不该挡启动
        pass

    try:
        session_dir(sid).mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "pid": os.getpid(),
            "instance": uuid.uuid4().hex[:8],
            "started": datetime.now().isoformat(timespec="seconds"),
            "workspace": str(ROOT),
        }, ensure_ascii=False), encoding=_ENCODING)
    except Exception:  # noqa: BLE001
        pass
    return warning


def release_owner(sid: str, instance: str | None = None) -> None:
    """退出时摘掉占用者标记 —— 只摘自己那个,别把别人的顺手删了。"""
    try:
        path = owner_file(sid)
        if not path.exists():
            return
        info = json.loads(path.read_text(encoding=_ENCODING) or "{}")
        if int(info.get("pid") or 0) == os.getpid():
            path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def clear_session(sid: str) -> None:
    """丢掉这个会话的历史(用户要开新会话时用)。"""
    try:
        session_file(sid).unlink(missing_ok=True)
    except OSError:
        pass
