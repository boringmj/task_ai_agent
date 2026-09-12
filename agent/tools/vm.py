from __future__ import annotations

from .registry import tool

import os

import atexit
import json
import socket
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from ..session import current_session_id, session_dir
from ..core import (
    PROJECT_DIR,
    clip_text,
    safe_path,
)


# ---- vm 工具专属配置(环境变量名不变,仍可在 .env 覆盖)----
# ---- 虚拟机(QEMU)沙箱 ----
# QEMU 二进制与基础盘在 temp/、vm/(可在 .env 配置),基础盘只读共享、从不被写。
# 每个**会话**用自己的一块 overlay 盘(放在该会话的目录里,见下面的 _vm_disk())
# —— 会话之间互不干扰,同一会话恢复时虚拟机状态也跟着回来。端口之类属于本进程的
# 资源仍按 VM_INSTANCE 区分,避免多开时抢占。
#
# 注意:磁盘路径要**按需算**而不是在导入时定死 —— 会话 id 是运行时从索引里解析
# 出来的(见 session.current_session_id),导入时还不知道是哪个会话。
VM_QEMU_DIR = Path(os.environ.get("VM_QEMU_DIR", str(PROJECT_DIR / "vm" / "qemu")))
VM_QEMU_SYSTEM = Path(os.environ.get("VM_QEMU_SYSTEM", str(VM_QEMU_DIR / "qemu-system-x86_64.exe")))
VM_QEMU_IMG = Path(os.environ.get("VM_QEMU_IMG", str(VM_QEMU_DIR / "qemu-img.exe")))
VM_DIR = Path(os.environ.get("VM_DIR", str(PROJECT_DIR / "vm")))
VM_BASE = Path(os.environ.get("VM_BASE", str(VM_DIR / "alpine-vmserver.qcow2")))  # 预装 vmserver 的新 base,只读共享
VM_ACCEL = os.environ.get("VM_ACCEL", "whpx")      # whpx 快;没开就设 tcg(慢但通用)
VM_INSTANCE = uuid.uuid4().hex[:8]                 # 每进程唯一,决定端口等本进程资源的唯一性

# 每个**会话**一台自己的虚拟机:磁盘放在该会话的目录里(见 agent/session.py),
# 而不是按进程生成、退出即删。这样会话恢复时,虚拟机里的东西(装过的软件、
# 写过的文件)也一并回来 —— "一个会话 = 一段对话 + 一块自己的盘"。
# 代价:盘只增不减,想从干净状态开始得自己删掉那个 .qcow2(见 vm_reset)。
def _vm_session_dir() -> Path:
    return session_dir(current_session_id())


def _vm_disk() -> Path:
    """本会话的虚拟机磁盘。随会话持久化 —— 会话恢复时它也跟着回来。"""
    sid = current_session_id()
    return session_dir(sid) / f"work-{sid}.qcow2"


def _vm_log() -> Path:
    sid = current_session_id()
    return session_dir(sid) / f"qemu-{sid}.log"


VM_VMSERVER_PORT = 40000                            # vmserver 在 guest 内监听的端口(固定)

_vm_port: int | None = None                        # 启动时动态分配,避免多开抢 2222
_vm_token = ""                                     # 每启动随机生成、经串口注入 guest,服务端每次请求读它
_vm_vmserver_host_port: int | None = None           # 宿主侧转发到 guest:40000 的端口
_vm_tunnels: dict = {}                              # host_port -> 常驻转发入口(宿主监听 + relay)


# ---------------- 虚拟机(QEMU)沙箱 ----------------
# 后台启动一个 Alpine 虚拟机作为更强隔离的沙箱。guest 内置 vmserver(socket),
# 每启动随机 token 经串口注入。状态机:BOOTING → LOGIN → READY。
# 执行命令走 vmserver(JSON);agent 只写本实例 overlay,基础盘只读共享。

_vm_proc = None
_vm_thread: "threading.Thread | None" = None
_vm_serial_port: int | None = None
# 线程安全的状态:{status, step, port, error}
_vm_state: dict = {"status": "idle", "step": "", "port": None, "error": ""}
_vm_lock = threading.Lock()


def _vm_state_set(status: str, step: str = "", port: int | None = None, error: str = "") -> None:
    with _vm_lock:
        _vm_state.update({"status": status, "step": step, "port": port, "error": error})


def _vm_state_get() -> dict:
    with _vm_lock:
        return dict(_vm_state)


def _vm_free_port(base: int) -> int:
    import socket
    for port in range(base, base + 60):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                continue
        except OSError:
            return port
    raise RuntimeError(f"端口分配失败({base} 起 60 个都被占用)")


def _vm_ensure() -> None:
    _vm_session_dir().mkdir(parents=True, exist_ok=True)   # 磁盘在会话目录里,不在镜像目录
    if not _vm_disk().exists():
        if not (VM_QEMU_IMG.exists() and VM_BASE.exists()):
            raise FileNotFoundError(f"缺少 QEMU 工具({VM_QEMU_IMG})或基础盘({VM_BASE})")
        subprocess.run(
            [str(VM_QEMU_IMG), "create", "-f", "qcow2", "-F", "qcow2",
             "-b", str(VM_BASE), str(_vm_disk())],
            check=True, capture_output=True,
        )


class _VmSerial:
    """QEMU 串口控制台通道(参考 sandbox_demo):连接 sentinel 读取、发送、排空。"""

    _KEEP = 64 * 1024   # 缓冲保留上限:超出就丢弃更早的内容,避免无限增长

    def __init__(self, port: int):
        import socket
        self.s = socket.create_connection(("127.0.0.1", port), timeout=15)
        self.s.settimeout(0.5)
        self.buf = b""

    def read_until(self, marker: str, timeout: float = 30.0) -> str:
        """读到 marker 出现(或超时)为止,返回到目前为止收到的文本。

        调用方只用它做子串判断,所以缓冲区只保留最近的 _KEEP 字节 —— 否则长时间
        刷屏(如启动日志)会让 buf 无限增长;每次也只解码一遍,不再逐块重复解码整段。
        """
        import socket
        end = time.time() + timeout
        while time.time() < end:
            try:
                d = self.s.recv(4096)
            except socket.timeout:
                continue
            if not d:
                break
            self.buf += d
            if len(self.buf) > self._KEEP * 2:
                self.buf = self.buf[-self._KEEP:]
            text = self.buf.decode("utf-8", "replace")
            if marker in text:
                return text
        return self.buf.decode("utf-8", "replace")

    def send(self, text: str) -> None:
        self.s.sendall(text.encode())

    def drain(self) -> None:
        self.s.settimeout(0.2)
        try:
            while True:
                d = self.s.recv(4096)
                if not d:
                    break
                self.buf += d
        except socket.timeout:
            pass

    def reset_buf(self) -> None:
        self.buf = b""


def _vm_serial_login(port: int) -> _VmSerial:
    """连串口,root/123456 登录,关回显。返回保持登录态的会话(不配置 sshd)。

    参考 sandbox_demo:全程串口控制台执行,不用 sshd — 这是正确的通道。
    """
    _vm_state_set("login", "登录 root/123456…")
    ser = None
    for _ in range(40):
        try:
            ser = _VmSerial(port)
            break
        except OSError:
            time.sleep(0.5)
    if ser is None:
        raise ConnectionError("连不上虚拟机串口")
    ser.read_until("login:", 30)
    ser.send("root\n")
    ser.read_until("Password:", 15)
    ser.send("123456\n")
    if "#" not in ser.read_until("#", 25):  # 等 shell 提示符,确认真进入 shell
        raise RuntimeError("串口登录未进入 shell")
    ser.send("stty -echo\n")  # 关回显:命令输出与输入回声分离,避免误判
    ser.drain()
    return ser


_vm_ser: "_VmSerial | None" = None        # 保持登录态的串口会话
_vm_serial_lock = threading.Lock()          # 串口只用于启动时注入 token,加锁避免冲突


def _vm_spawn_and_login(serial_port: int, vmserver_host_port: int) -> None:
    """boot QEMU(映射 guest:40000 的 vmserver)+ 串口登录,设置 _vm_proc/_vm_ser。"""
    global _vm_proc, _vm_ser
    cmd = [
        str(VM_QEMU_SYSTEM),
        "-drive", f"file={_vm_disk()},if=virtio",
        # 只把 vmserver 的 40000 口转发到宿主;其余 guest 口不暴露(vmserver 可代理)
        "-netdev", f"user,id=n0,hostfwd=tcp::{vmserver_host_port}-:{VM_VMSERVER_PORT}",
        "-device", "virtio-net-pci,netdev=n0",
        "-m", "1024", "-smp", "2", "-display", "none",
        "-serial", f"tcp:127.0.0.1:{serial_port},server=on,wait=off",
        "-accel", VM_ACCEL,
    ]
    import subprocess as sp
    # stderr 落到日志文件(不接管道:没人读的管道写满会把 QEMU 堵死)。
    # 启动失败时它的内容就是真正的原因(如 "virtfs support is disabled"),
    # 只报一句"请检查 VM_ACCEL"会让人反复猜。
    log = _vm_log()
    with log.open("wb") as log_fp:
        _vm_proc = sp.Popen(cmd, creationflags=sp.CREATE_NO_WINDOW,
                            stdout=sp.DEVNULL, stderr=log_fp)
    time.sleep(1)
    if _vm_proc.poll() is not None and _vm_proc.returncode != 0:
        detail = ""
        try:
            detail = log.read_text(encoding="utf-8", errors="replace").strip()[-800:]
        except OSError:
            pass
        raise RuntimeError(
            f"QEMU 启动即退出(rc={_vm_proc.returncode})。"
            + (f"QEMU 的报错:\n{detail}" if detail
               else "未拿到 QEMU 的报错,请检查 VM_ACCEL(whpx/tcg)或镜像是否可用。")
        )
    _vm_ser = _vm_serial_login(serial_port)


def _vm_worker() -> None:
    """后台线程:boot → 串口登录 → 注入每启动随机 token → READY。

    之后 vm_run 走 socket 到 vmserver,不再用裸串口;串口只在启动时注入 token。
    """
    global _vm_token, _vm_vmserver_host_port
    try:
        if not VM_BASE.exists():
            raise FileNotFoundError(
                f"缺少 vmserver 基础盘 {VM_BASE}。请先用 qemu-img convert 生成 vm/alpine-vmserver.qcow2。")
        _vm_ensure()
        serial_port = _vm_free_port(3000)
        vmserver_host_port = _vm_free_port(5000)
        global _vm_serial_port
        _vm_serial_port = serial_port
        _vm_vmserver_host_port = vmserver_host_port
        _vm_state_set("booting", "启动 QEMU…", port=vmserver_host_port)
        _vm_spawn_and_login(serial_port, vmserver_host_port)

        # 生成并注入每启动随机 token(vmserver 每次请求读 /root/.vm_token,无需重启服务)
        _vm_token = uuid.uuid4().hex + uuid.uuid4().hex
        with _vm_serial_lock:
            _vm_ser.send(f"echo '{_vm_token}' > /root/.vm_token\n")
            time.sleep(0.3)
            # 回读校验:写失败会变成"状态已就绪但 vmserver 锁死",之后每个 vm_run
            # 都报 locked / auth failed,很难查 —— 在源头就确认写进去了。
            _vm_ser.reset_buf()
            _vm_ser.send("cat /root/.vm_token\n")
            echoed = _vm_ser.read_until(_vm_token, 5)
        if _vm_token not in echoed:
            raise RuntimeError("token 注入校验失败:串口回读不到刚写入的 token,虚拟机不可安全使用")
        try:
            _vm_ser.s.close()   # 串口只在注入 token 用,之后 vm_run 走 socket
        except Exception:
            pass
        _vm_state_set("ready", "就绪,可连接 vmserver", port=vmserver_host_port)
    except Exception as exc:  # noqa: BLE001
        _vm_state_set("error", "", error=str(exc))


def _vm_kickoff() -> None:
    """启动后台线程(幂等)。"""
    global _vm_thread
    if _vm_thread is not None and _vm_thread.is_alive():
        return
    _vm_thread = threading.Thread(target=_vm_worker, daemon=True)
    _vm_thread.start()


@tool(
    description="查看沙箱虚拟机的当前状态:进行到哪一步、是否已就绪、连接端口。"
                "虚拟机在后台启动,可能没就绪;用这个确认是否能用。",
    parameters={"type": "object", "properties": {}},
)
def vm_status() -> str:
    """查看沙箱虚拟机的当前状态(进行到哪一步、是否就绪)。"""
    st = _vm_state_get()
    if st["status"] == "ready":
        return f"虚拟机就绪(实例 {VM_INSTANCE[:8]},串口 {st['port']})。"
    if st["status"] == "error":
        return f"虚拟机出错:{st['error']}"
    return "虚拟机会在后台启动,agent 退出后会自动销毁。"


@tool(
    description="在沙箱虚拟机里执行一条命令,通过 socket 连 guest 内的 vmserver 执行(JSON 协议,非 SSH)。"
                "适合在隔离的完整系统里装软件、跑服务、做重活。若虚拟机还没就绪,会返回当前进度并让你稍后再试。"
                "命令自带超时;别用交互式命令(vim/top 等,它们没有终端会直接失败)。"
                "路径注意:客户机是 Linux,路径风格与宿主不同(没有 D:\\ 那套)。",
    parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要在客户机里执行的 shell 命令",
                    }
                },
                "required": ["command"],
            },
)
def vm_run(command: str) -> str:
    """在沙箱虚拟机里执行一条命令 —— 连接 guest 里的 vmserver(socket, JSON 协议)。

    vmserver 用无 TTY 的 subprocess 跑命令、自带超时(超时就 kill,返回 timed_out),
    所以交互程序(vim/top)会直接秒失败、不会卡死会话;命令输出是结构化 JSON,无壳提示符。
    """
    st = _vm_state_get()
    if st["status"] != "ready":
        return f"虚拟机还没就绪,当前:{st['step'] or st['status']}。请用 vm_status 或稍后再试。"
    if not _vm_vmserver_host_port or not _vm_token:
        return "虚拟机 vmserver 未就绪,请稍后再试。"

    import socket as sk, json
    req = {"token": _vm_token, "cmd": "exec", "command": command, "timeout": 60}
    try:
        s = sk.create_connection(("127.0.0.1", _vm_vmserver_host_port), timeout=10)
        s.settimeout(90)
        s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        line = s.makefile("rb").readline()
        s.close()
        resp = json.loads(line.decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 - 服务端可能挂了,给提示并可尝试重启
        return f"连接 vmserver 失败:{exc}(vmserver 可能未就绪或已退出,可稍后重试或看 vm_status)。"

    if not resp.get("ok"):
        return f"vmserver 错误:{resp.get('error', 'unknown')}"
    out = resp.get("output", "")
    timed = resp.get("timed_out", False)
    return (clip_text(out, 10000) if out else "(无输出)") + ("\n[命令超时,已中断]" if timed else "")


def _shq(s: str) -> str:
    """POSIX 单引号转义:把 guest 侧字符串安全地放进单引号里。"""
    return "'" + s.replace("'", "'\\''") + "'"


def _vm_exec_raw(command: str, timeout: int = 60) -> dict:
    """发一条 exec 到 guest 里的 vmserver,返回**原始响应 dict**(不截断、不拼摘要)。

    与 vm_run 的区别:这里拿的是完整响应(含任意大 output),由调用方决定怎么用;
    vm_run 面向模型、输出会截断后回显;文件传输(读回 base64)要的是完整字节,故走这里,
    这样文件内容只存在于工具函数内部,不会填进对话上下文。
    """
    st = _vm_state_get()
    if st["status"] != "ready":
        return {"ok": False, "error": f"虚拟机还没就绪({st['step'] or st['status']})"}
    if not _vm_vmserver_host_port or not _vm_token:
        return {"ok": False, "error": "vmserver 未就绪"}
    import socket as sk, json
    req = {"token": _vm_token, "cmd": "exec", "command": command, "timeout": timeout}
    try:
        s = sk.create_connection(("127.0.0.1", _vm_vmserver_host_port), timeout=10)
        s.settimeout(timeout + 30)
        s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        line = s.makefile("rb").readline()
        s.close()
        return json.loads(line.decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"连接 vmserver 失败:{exc}"}


@tool(
    description="把工作区里的一个文件上传到虚拟机(宿主 → guest)。"
                "文件字节走 vmserver 全程在工具内部处理,只回「已上传 (n 字节)」摘要,不会把文件内容塞进上下文。"
                "local_path 是工作区路径;guest_path 是 guest 里的绝对路径(如 /root/a.txt)。"
                "当 guest 里要做的事需要工作区的文件(脚本、配置、素材)时用它,比 base64 手拼省事。",
    parameters={
                "type": "object",
                "properties": {
                    "local_path": {"type": "string", "description": "工作区内的文件路径(相对或绝对)"},
                    "guest_path": {"type": "string", "description": "guest 里要写入的绝对路径,如 /root/a.txt"},
                },
                "required": ["local_path", "guest_path"],
            },
)
def vm_push(local_path: str, guest_path: str) -> str:
    """把工作区里的一个文件上传到虚拟机(宿主 → guest)。

    文件字节经 base64 分块走 vmserver 写入 guest,**全程在工具内部完成**,
    只回「已上传 (n 字节)」这类摘要,不会把文件内容塞进对话上下文。
    local_path 必须是工作区内路径(相对或绝对);guest_path 是 guest 里的绝对路径(如 /root/a.txt)。
    """
    import base64
    local = safe_path(local_path)
    if not local.is_file():
        return f"错误:工作区里没有 {local_path}(解析为 {local})"
    gp = _shq(guest_path)
    data = local.read_bytes()
    base = base64.b64encode(data).decode("ascii")  # base64 字符不含单引号,可安全放进单引号
    if not base:
        _vm_exec_raw(f"rm -f {gp}; mkdir -p \"$(dirname {gp})\"; : > {gp}", 30)
        return f"已把工作区文件上传到 guest:{guest_path}(空文件,0 字节)。"
    _vm_exec_raw(f"rm -f {gp}; mkdir -p \"$(dirname {gp})\"", 30)
    CHUNK = 64 * 1024  # 每块 64KB 源,base64 后 ~87KB,低于 guest 参数限制
    for i in range(0, len(base), CHUNK):
        piece = base[i:i + CHUNK]
        redir = ">" if i == 0 else ">>"  # 首块覆盖、其余追加
        resp = _vm_exec_raw(f"printf '%s' '{piece}' | base64 -d {redir} {gp}", 60)
        if not resp.get("ok"):
            return f"错误:第 {i // CHUNK + 1} 块写入失败:{resp.get('error', '')} — {resp.get('output', '')[:200]}"
    return f"已把工作区文件 {local_path} 上传到 guest:{guest_path}({len(data)} 字节)。"


@tool(
    description="把虚拟机里的一个文件下载到工作区(guest → 宿主)。"
                "文件字节走 vmserver 全程在工具内部处理,只回「已下载 (n 字节)」摘要,不会把文件内容塞进上下文。"
                "guest_path 是 guest 里的绝对路径;local_path 是工作区路径。"
                "当 guest 里产出了要拿回工作区的文件(结果、日志、下载物)时用它。",
    parameters={
                "type": "object",
                "properties": {
                    "guest_path": {"type": "string", "description": "guest 里的绝对路径,如 /root/out.txt"},
                    "local_path": {"type": "string", "description": "工作区内要写入的文件路径"},
                },
                "required": ["guest_path", "local_path"],
            },
)
def vm_pull(guest_path: str, local_path: str) -> str:
    """把虚拟机里的一个文件下载到工作区(guest → 宿主)。

    文件经 vmserver 分块 base64 读回,**在工具内部解码后写入工作区 local_path**,
    只回「已下载 (n 字节)」摘要,文件内容不进对话上下文。
    local_path 是工作区路径;guest_path 是 guest 里的绝对路径(如 /root/out.txt)。
    """
    import base64
    local = safe_path(local_path)
    gp = _shq(guest_path)
    size_resp = _vm_exec_raw(f"if [ -f {gp} ]; then stat -c %s {gp}; else echo MISSING; fi", 30)
    size_raw = (size_resp.get("output") or "").strip()
    if size_raw == "MISSING":
        return f"错误:虚拟机里没有 {guest_path}"
    try:
        total = int(size_raw.splitlines()[-1])
    except ValueError:
        return f"错误:无法读取 guest 文件大小({size_raw!r})"
    BS = 64 * 1024          # 与 vmserver 200KB 输出上限对齐:base64 后 ~87KB < 200KB
    out = bytearray()
    blocks = (total + BS - 1) // BS
    for b in range(blocks):
        cmd = f"dd if={gp} bs={BS} skip={b} count=1 2>/dev/null | base64 | tr -d '\n'"
        resp = _vm_exec_raw(cmd, 60)
        if not resp.get("ok"):
            return f"错误:读 guest {guest_path} 第 {b + 1}/{blocks} 块失败:{resp.get('error', '')}"
        try:
            out += base64.b64decode((resp.get("output") or "").strip())
        except Exception as exc:  # noqa: BLE001
            return f"错误:guest 第 {b + 1} 块 base64 解码失败:{exc}"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(bytes(out))
    return f"已把 guest {guest_path} 下载到工作区:{local_path}({len(out)} 字节)。"


@tool(
    description="转发访问 guest(沙箱虚拟机)内的 HTTP 服务 —— 通过 vmserver 的 proxy 把 guest 端口代理到本地。"
                "适合访问你在 guest 里起的 http 服务(如 web:8080)并拿到响应。"
                "只支持 http,不支持 https。URL 写 guest 视角:http://127.0.0.1:端口/路径。",
    parameters={
                "type": "object",
                "properties": {
                    "guest_url": {
                        "type": "string",
                        "description": "guest 内部的 http 地址,如 http://127.0.0.1:8080/status",
                    }
                },
                "required": ["guest_url"],
            },
)
def vm_fetch(guest_url: str) -> str:
    """转发访问 guest 内的 HTTP 服务:通过 vmserver 的 proxy,把 guest 端口代理到本地。

    只支持 http(geth;HTTPS 透传要 TLS,不支持)。适合访问 agent 在 guest 里起的服务。
    """
    st = _vm_state_get()
    if st["status"] != "ready":
        return f"虚拟机还没就绪,当前:{st['step'] or st['status']}。请用 vm_status 或稍后再试。"
    if not _vm_vmserver_host_port or not _vm_token:
        return "虚拟机 vmserver 未就绪,请稍后再试。"

    from urllib.parse import urlparse
    u = urlparse(guest_url)
    if u.scheme != "http" or not u.netloc:
        return "只支持 http://host:port/... 形式(HTTPS 透传暂不支持,请用 guest 内的 http 服务)。"
    host = u.hostname or "127.0.0.1"
    port = u.port or 80
    path = u.path or "/"
    if u.query:
        path += "?" + u.query

    import socket as sk
    try:
        s = sk.create_connection(("127.0.0.1", _vm_vmserver_host_port), timeout=10)
        s.settimeout(30)
        req = {"token": _vm_token, "cmd": "proxy", "host": host, "port": port}
        s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        ack = json.loads(s.makefile("rb").readline().decode("utf-8", "replace"))
        if not (ack.get("ok") and ack.get("proxy")):
            s.close()
            return f"vmserver proxy 失败:{ack.get('error', 'unknown')}"
        # 通过已建立的透传连接发 HTTP GET,读响应
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: {u.netloc}\r\nConnection: close\r\n\r\n".encode())
        data = b""
        while True:
            c = s.recv(8192)
            if not c:
                break
            data += c
            if len(data) > 2_000_000:
                break
        s.close()
    except Exception as exc:  # noqa: BLE001
        return f"vm_fetch 失败:{exc}"

    if not data:
        return "(guest 服务无响应)"
    status = data.split(b"\r\n", 1)[0].decode("utf-8", "replace")
    sep = data.find(b"\r\n\r\n")
    body = data[sep + 4:].decode("utf-8", "replace") if sep >= 0 else ""
    return f"{status}\n--- body ---\n{clip_text(body, 8000)}"


@tool(
    description="向 guest(沙箱虚拟机)内任意 TCP 服务发送一段字节并读回复 —— 经 vmserver 的 proxy 转发。"
                "通用 TCP,不限于 HTTP:适合 Redis、MySQL 查询、自定协议等请求/答型服务。"
                "是「发一次、收一次」的一问一答,持续会话类(SSH)不适合。",
    parameters={
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "guest 内的目标主机,通常是 127.0.0.1"},
                    "port": {"type": "integer", "description": "guest 内的目标端口"},
                    "data": {"type": "string", "description": "要发送的字节(把要发的请求编码成文本)"},
                },
                "required": ["host", "port", "data"],
            },
)
def vm_tcp(host: str, port: int, data: str) -> str:
    """向 guest 内任意 TCP 服务发一段字节并读回复(经 vmserver proxy)。

    通用 TCP(不仅 HTTP):适合请求/答一类协议(Redis、MySQL 查询、自定协议等)。
    注意是"发一次、收一次"的一问一答;持续会话类(SSH)不适合。
    """
    st = _vm_state_get()
    if st["status"] != "ready":
        return f"虚拟机还没就绪,当前:{st['step'] or st['status']}。请用 vm_status 或稍后再试。"
    if not _vm_vmserver_host_port or not _vm_token:
        return "虚拟机 vmserver 未就绪,请稍后再试。"

    import socket as sk
    try:
        s = sk.create_connection(("127.0.0.1", _vm_vmserver_host_port), timeout=10)
        s.settimeout(30)
        req = {"token": _vm_token, "cmd": "proxy", "host": host, "port": int(port)}
        s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        ack = json.loads(s.makefile("rb").readline().decode("utf-8", "replace"))
        if not (ack.get("ok") and ack.get("proxy")):
            s.close()
            return f"vmserver proxy 失败:{ack.get('error', 'unknown')}"
        s.sendall(data.encode("utf-8"))
        reply = b""
        while True:
            c = s.recv(8192)
            if not c:
                break
            reply += c
            if len(reply) > 2_000_000:
                break
        s.close()
    except Exception as exc:  # noqa: BLE001
        return f"vm_tcp 失败:{exc}"

    if not reply:
        return "(guest 服务无响应)"
    try:
        text = reply.decode("utf-8")
        if all(ord(ch) >= 32 or ch in "\r\n\t" for ch in text):
            return clip_text(text, 10000)
        raise ValueError
    except Exception:
        return f"(二进制 {len(reply)} 字节,前 200 字节 hex: {reply[:200].hex()})"


def _vm_proxy_relay(guest_port: int, client) -> None:
    """把一条宿主连接经 vmserver proxy 转发到 guest:guest_port,双向泵字节。"""
    import socket as sk, json
    try:
        up = sk.create_connection(("127.0.0.1", _vm_vmserver_host_port), timeout=10)
        up.settimeout(60)
        req = {"token": _vm_token, "cmd": "proxy", "host": "127.0.0.1", "port": guest_port}
        up.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        ack = json.loads(up.makefile("rb").readline().decode("utf-8", "replace"))
        if not (ack.get("ok") and ack.get("proxy")):
            up.close()
            client.close()
            return
    except Exception:
        try:
            client.close()
        except Exception:
            pass
        return

    def pump(a, b):
        try:
            while True:
                d = a.recv(8192)
                if not d:
                    break
                b.sendall(d)
        except Exception:
            pass
        try:
            b.shutdown(sk.SHUT_WR)
        except Exception:
            pass

    threading.Thread(target=pump, args=(client, up), daemon=True).start()
    threading.Thread(target=pump, args=(up, client), daemon=True).start()


@tool(
    description="把 guest(沙箱虚拟机)内某个端口**常驻转发**到宿主导,浏览器等程序可直接访问宿主口。"
                "真正实现「将 VM 端口映射到宿主」,例如 vm_tunnel(8080, 8080) 后访问 http://127.0.0.1:8080。"
                "每条连接经 vmserver proxy(带本进程 token)转发,不暴露原生 hostfwd。"
                "用完记得 vm_tunnel_stop。",
    parameters={
                "type": "object",
                "properties": {
                    "host_port": {"type": "integer", "description": "宿主导要监听的口,如 8080"},
                    "guest_port": {"type": "integer", "description": "guest 内的目标端口,如 8080"},
                },
                "required": ["host_port", "guest_port"],
            },
)
def vm_tunnel(host_port: int, guest_port: int) -> str:
    """把 guest 内某个端口常驻转发到宿主导 —— 浏览器等直接访问宿主口即可。

    每条进来的宿主连接,都会经 vmserver proxy(带本进程 token)转到 guest:guest_port。
    真正实现"把 VM 端口映射到宿主导、可浏览器访问"。
    """
    st = _vm_state_get()
    if st["status"] != "ready":
        return f"虚拟机还没就绪,当前:{st['step'] or st['status']}。请用 vm_status 或稍后再试。"
    if not _vm_vmserver_host_port or not _vm_token:
        return "虚拟机 vmserver 未就绪,请稍后再试。"
    import socket as sk
    if int(host_port) in _vm_tunnels:
        vm_tunnel_stop(int(host_port))
    srv = sk.socket(sk.AF_INET, sk.SOCK_STREAM)
    srv.setsockopt(sk.SOL_SOCKET, sk.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", int(host_port)))
    srv.listen(16)
    srv.settimeout(1.0)

    def accept_loop():
        while int(host_port) in _vm_tunnels:
            try:
                client, _ = srv.accept()
            except sk.timeout:
                continue
            except OSError:
                break
            if int(host_port) in _vm_tunnels:
                threading.Thread(target=_vm_proxy_relay, args=(int(guest_port), client), daemon=True).start()
            else:
                try:
                    client.close()
                except Exception:
                    pass

    _vm_tunnels[int(host_port)] = {"server": srv, "thread": threading.Thread(target=accept_loop, daemon=True)}
    _vm_tunnels[int(host_port)]["thread"].start()
    return f"已开通宿主 127.0.0.1:{host_port} → guest:{guest_port}。浏览器访问 http://127.0.0.1:{host_port}"


@tool(
    description="停止一个(给 host_port)或全部(不给)常驻端口转发。",
    parameters={
                "type": "object",
                "properties": {
                    "host_port": {"type": "integer", "description": "要停止的宿主导;不填则停全部"},
                },
            },
)
def vm_tunnel_stop(host_port: int | None = None) -> str:
    """停止一个(或全部)常驻转发。"""
    if host_port is not None and int(host_port) not in _vm_tunnels:
        return f"宿主 {host_port} 没有在转发。"
    targets = [int(host_port)] if host_port is not None else list(_vm_tunnels)
    for hp in targets:
        entry = _vm_tunnels.pop(hp, None)
        if not entry:
            continue
        try:
            entry["server"].close()  # 关闭监听,accept 循环退出
        except Exception:
            pass
    return "已停止指定转发。" if host_port is not None else "已停止所有转发。"


def vm_reset() -> str:
    """把本会话的虚拟机恢复成出厂状态:停掉它、删掉会话磁盘,再用基础镜像重建。

    磁盘现在跟着会话持久化(装过的软件、写过的文件都在里面),所以需要一个
    "推倒重来"的口子,否则那块盘只会越长越大。**不可恢复**:VM 里所有东西都会没。
    工作区文件与长期记忆不受影响。
    """
    global _vm_thread
    try:
        if _vm_proc is not None and _vm_proc.poll() is None:
            _vm_proc.kill()                    # 先停掉,QEMU 占着盘就删不掉
            try:
                _vm_proc.wait(timeout=5)
            except Exception:
                pass
    except Exception:
        pass
    time.sleep(0.3)                            # 等 QEMU 释放文件句柄

    try:
        _vm_disk().unlink(missing_ok=True)
    except OSError as exc:
        return (f"虚拟机磁盘删不掉({exc})—— 多半还有别的进程占着它。"
                f"确认没有其它 agent 实例在跑同一个会话后重试。")

    _vm_state_set("idle", "已重置,等待重新启动")
    _vm_thread = None                          # 让 _vm_kickoff 能重新起线程
    _vm_kickoff()
    return "已重置虚拟机:磁盘已删除,正在用基础镜像重新启动(稍后用 vm_status 看进度)。"


def _vm_cleanup() -> None:
    """agent 退出时:杀掉本实例的 QEMU、关闭转发、清掉日志,**但保留磁盘**。

    磁盘现在属于会话(见 VM_DISK 的注释),会话恢复时要靠它把虚拟机里的东西带回来,
    所以这里**不能删** —— 想从干净状态重来要显式重置(见 vm_reset)。
    只动本进程的 _vm_proc,不 taskkill /IM,以免误杀其它 agent 或用户自己的 VM。
    """
    # 退出前先让 guest 把页缓存刷到虚拟磁盘。磁盘是会话资产、下次要接着用,而 QEMU
    # 是被 kill 的 —— guest 自己内存里还没落盘的写入会随它一起消失(实测:不 sync 时
    # 刚写的文件重启就没了,sync 之后能留下)。best-effort,连不上就算了。
    try:
        if _vm_state_get().get("status") == "ready":
            _vm_exec_raw("sync", timeout=15)
    except Exception:
        pass
    try:
        if _vm_proc is not None and _vm_proc.poll() is None:
            _vm_proc.kill()          # 仅本进程起的 QEMU
            try:
                _vm_proc.wait(timeout=5)
            except Exception:
                pass
    except Exception:
        pass
    try:
        vm_tunnel_stop()  # 关闭所有常驻转发
    except Exception:
        pass
    try:
        _vm_log().unlink(missing_ok=True)
    except OSError:
        pass


atexit.register(_vm_cleanup)


@tool(
    description="确保内置的 Alpine 虚拟机(QEMU 沙箱)在后台启动与配置。已在配则返回当前状态。"
                "这个虚拟机比容器隔离更强(独立内核),适合让它做容器里放不开的完整系统操作。配置是后台进行的,可配合 vm_status 看进度。",
    parameters={"type": "object", "properties": {}},
)
def vm_start() -> str:
    """确保沙箱虚拟机在后台启动(已在配就返回当前状态)。"""
    _vm_kickoff()
    return f"虚拟机正在后台启动({VM_INSTANCE[:8]}),可查 vm_status。"
