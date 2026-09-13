from __future__ import annotations

from .registry import tool

import os

import atexit
import json
import socket
import secrets
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


def _qemu_pid_file(sid: str) -> Path:
    """记录"本会话正在跑的 QEMU 是哪个 pid"。

    这是启动时清扫孤儿的依据:光靠进程名去扫是不行的 —— 那会连别的 agent 实例
    甚至用户自己的 VM 一起杀掉。只认**我们自己写下过的 pid**,再逐条验证。
    """
    return session_dir(sid) / "qemu.pid"


VM_VMSERVER_PORT = 40000                            # vmserver 在 guest 内监听的端口(固定)
VM_CMD_TIMEOUT = int(os.environ.get("VM_CMD_TIMEOUT", "60"))     # vm_run 默认额度(秒)
# 长任务额度,给 3 小时。是折中而不是"永不超时":vmserver 每条连接一个线程,超时是回收
# 挂死命令的唯一机制 —— 一条等输入的 cat 会一直占着线程,而调用方早忘了它。
VM_LONG_TIMEOUT = int(os.environ.get("VM_LONG_TIMEOUT", "10800"))

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
# 启动代次:每逢"重置/切换会话"就 +1,让**还在跑的旧启动流程自己作废**。
# 光靠 kill + join 堵不住:线程可能正卡在串口登录的重试里,join 超时返回后它又接着
# spawn 一台 QEMU —— 那台就成了没人认领的孤儿,把盘锁住(实测 /vmreset 报 WinError 32)。
_vm_gen = 0
_vm_serial_port: int | None = None
# 线程安全的状态:{status, step, port, error}
_vm_state: dict = {"status": "idle", "step": "", "port": None, "error": ""}
_vm_lock = threading.Lock()


def _vm_state_set(status: str, step: str = "", port: int | None = None, error: str = "") -> None:
    with _vm_lock:
        _vm_state.update({"status": status, "step": step, "port": port, "error": error})


def vm_state_get() -> dict:
    with _vm_lock:
        return dict(_vm_state)


def _vm_free_port(base: int) -> int:
    """从 base 起找一个**真的绑得上**的端口。

    为什么用 bind 探测,而不是"connect 得上就算被占用":
    端口被占着有三种表现 —— 立即拒绝、接受连接、以及**超时**(端口被绑着,但对方
    accept 队列不响应;QEMU 的串口 `wait=off` 正是这样)。后两种都会让 connect 失败,
    而 `except OSError` 把它们和"没人监听"一视同仁地当成空闲。实测的后果是:
    我们兴冲冲把端口交给 QEMU → 它 bind 失败报 "Failed to find an available port"
    → 而我们的串口**连到了别人家那个进程上**,登录卡在"未进入 shell"。
    bind 是唯一和 QEMU 一致的判据:能绑,才是真的空。
    """
    import socket
    for port in range(base, base + 60):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port          # 绑得上 = 空闲(随即随 with 关闭,交给 QEMU 去绑)
            except OSError:
                continue             # 绑不上 = 真被占,试下一个
    raise RuntimeError(f"端口分配失败({base} 起 60 个都被占用)")


def _vm_kill_current(timeout: int = 5) -> None:
    """杀掉本进程起的这台 QEMU,并**等它真的退出**。

    等这一下不是讲究:kill 只负责发信号,进程没退干净时**磁盘句柄还开着** ——
    紧接着的删除或重建就会撞上 WinError 32("另一个程序正在使用此文件"),
    也就是 /vmreset 报的那个错。
    """
    global _vm_proc
    proc, _vm_proc = _vm_proc, None
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=timeout)
    except Exception:  # noqa: BLE001 - 杀不掉也只能继续,后面还有启动时的孤儿清扫兜底
        pass


def _vm_stop_thread(timeout: int = 10) -> None:
    """等后台启动线程收工,并让 vm_kickoff 可以重新拉起。

    不 join 的后果很实在:vm_kickoff 的幂等判断是"线程还活着就别起新的",而
    vm_reset / vm_switch_session 会先把 _vm_thread 置 None 绕过它。旧线程还没结束
    (比如卡在串口登录的重试循环里)时新线程就起来了 —— **两个 worker 各起一台
    QEMU**,其中一台当场失去引用变成孤儿,占着磁盘和串口端口不放(实测:那种情况下
    /vmreset 删不掉盘,而该会话的 VM 再也起不来)。
    """
    global _vm_thread
    t, _vm_thread = _vm_thread, None
    if t is not None and t.is_alive():
        t.join(timeout=timeout)


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
    def _expect(marker: str, timeout: int, what: str) -> None:
        """等一个标志串;等不到就**把 guest 实际说的话一并报出来**。

        以前这里直接丢掉 read_until 的返回值,失败时只有一句干巴巴的结论 ——
        guest 是卡在引导、内核 panic、还是压根没启动,全无线索(实测排查一次
        "进不去 shell"为此绕了大圈)。串口在这一刻收到的那段文本就是唯一现场,
        没有理由扔掉。
        """
        got = ser.read_until(marker, timeout)
        if marker not in got:
            shown = got.strip() or "(这段时间串口一个字节都没有)"
            raise RuntimeError(f"{what}:等了 {timeout}s 没等到 {marker!r}。"
                               f"虚拟机串口里是这些内容:\n{shown[-600:]}")

    _expect("login:", 30, "等不到登录提示符")
    ser.send("root\n")
    _expect("Password:", 15, "等不到密码提示符")
    ser.send("123456\n")
    _expect("#", 25, "登录后没进到 shell")
    ser.send("stty -echo\n")  # 关回显:命令输出与输入回声分离,避免误判
    ser.drain()
    return ser


_vm_ser: "_VmSerial | None" = None        # 保持登录态的串口会话
_vm_serial_lock = threading.Lock()          # 串口只用于启动时注入 token,加锁避免冲突


def _vm_spawn_and_login(serial_port: int, vmserver_host_port: int, gen: int) -> None:
    """boot QEMU(映射 guest:40000 的 vmserver)+ 串口登录,设置 _vm_proc/_vm_ser。"""
    global _vm_proc, _vm_ser
    # 起新的之前先收掉上一台 —— _vm_proc 是唯一句柄,直接赋值覆盖就等于把旧 QEMU
    # 扔在后台当孤儿:它继续占着磁盘和串口端口,谁都杀不掉(下次启动的孤儿清扫也
    # 未必认得它)。这是最后一道保险,前面几处 kill 各有各的竞态。
    _vm_kill_current()
    cmd = [
        str(VM_QEMU_SYSTEM),
        "-drive", f"file={_vm_disk()},if=virtio",
        # 只把 vmserver 的 40000 口转发到宿主;其余 guest 口不暴露(vmserver 可代理)。
        # 主机地址必须显式写 127.0.0.1:留空的话 QEMU 会绑到 0.0.0.0(实测如此),
        # 等于把"能在 guest 里执行任意命令"的服务摆到局域网上 —— 虽然还有 token 挡着,
        # 但没理由开这么大,本机用就只绑本机。
        "-netdev", f"user,id=n0,hostfwd=tcp:127.0.0.1:{vmserver_host_port}-:{VM_VMSERVER_PORT}",
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
    # 记下 pid:万一本进程被强杀(atexit 不跑),下次启动靠它把这台孤儿认出来
    try:
        _qemu_pid_file(current_session_id()).write_text(str(_vm_proc.pid), encoding="utf-8")
    except OSError:
        pass
    time.sleep(1)
    if gen != _vm_gen:
        # 刚拉起来就被作废了(重置/切换会话正好插在这一瞬)—— 当场收掉。
        # 留着它就是孤儿:占着磁盘和串口端口,连 /vmreset 都会栽在 WinError 32 上。
        _vm_kill_current()
        return
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


def _vm_worker(gen: int) -> None:
    """后台线程:boot → 串口登录 → 注入每启动随机 token → READY。

    之后 vm_run 走 socket 到 vmserver,不再用裸串口;串口只在启动时注入 token。

    gen 是本次启动的代次。流程中途被"重置/切换会话"作废(见 _vm_gen)时,**每个关键
    步骤前**都自行退出 —— 尤其是拉起 QEMU 之前那次:旧流程要是已经没人管了还把 QEMU
    硬拉起来,那台就是谁也杀不掉的孤儿。
    """
    global _vm_token, _vm_vmserver_host_port
    try:
        if gen != _vm_gen:
            return
        if not VM_BASE.exists():
            raise FileNotFoundError(
                f"缺少 vmserver 基础盘 {VM_BASE}。请先用 qemu-img convert 生成 vm/alpine-vmserver.qcow2。")
        _vm_ensure()
        if gen != _vm_gen:
            return
        serial_port = _vm_free_port(3000)
        vmserver_host_port = _vm_free_port(5000)
        global _vm_serial_port
        _vm_serial_port = serial_port
        _vm_vmserver_host_port = vmserver_host_port
        _vm_state_set("booting", "启动 QEMU…", port=vmserver_host_port)
        _vm_spawn_and_login(serial_port, vmserver_host_port, gen)
        if gen != _vm_gen:
            return                      # 启动期间被作废,别再往下走

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


def vm_kickoff() -> None:
    """启动后台线程(幂等)。"""
    global _vm_thread, _vm_gen
    if _vm_thread is not None and _vm_thread.is_alive():
        return
    _vm_gen += 1                       # 新一代:顺带让任何还在跑的旧流程作废
    gen = _vm_gen
    # 起自己的 VM 之前先扫掉上次强杀留下的孤儿 —— 它们可能正锁着某个会话的磁盘,
    # 不先清掉的话那个会话恢复时会撞上"盘被占用"。放在这里而不是 cli 里,
    # 是为了让任何调用 kickoff 的入口都自动带上这道兜底。
    try:
        _vm_cleanup_stale()
    except Exception:  # noqa: BLE001 - 清扫失败不该挡启动
        pass
    _vm_thread = threading.Thread(target=_vm_worker, args=(gen,), daemon=True)
    _vm_thread.start()


def vm_is_running() -> bool:
    """当前这台 VM 的进程还在不在。

    只看进程活着没,**不看是否 ready** —— 正在启动的也算"它就在那儿"。
    """
    return _vm_proc is not None and _vm_proc.poll() is None


def session_reset_hint() -> str:
    """会话被重置之后,如果 VM 还开着,返回一句该讲给用户听的话(否则空串)。

    为什么要单独一个函数:"VM 不会跟着会话一起重置"这件事只有这里最清楚,所以话由
    这里出;但**说不说、什么时候说**交给调用方(cli)定 —— 会话命令不该反过来 import
    VM 模块,它只声明"会话被重置了"这个事实(见 commands/__init__.py 的 events)。

    返回值是**打印给用户看的纯文本**,别写 markdown 标记(星号、反引号会原样露出来)。
    """
    if not vm_is_running():
        # VM 压根没起来时,说"虚拟机没被重置"纯属噪音,让人去 /vmreset 一个不存在的
        # 虚拟机也很怪 —— 所以这种情况什么都不说。
        return ""
    return ("\n\n注意:虚拟机没有跟着重置 —— 它还开着,之前装的软件、跑的服务、"
            "写进去的文件都还在。想连它一起清掉,用 /vmreset。")


@tool(
    description="查看沙箱虚拟机的当前状态:进行到哪一步、是否已就绪、连接端口。"
                "虚拟机在后台启动,可能没就绪;用这个确认是否能用。",
    parameters={"type": "object", "properties": {}},
)
def vm_status() -> str:
    """查看沙箱虚拟机的当前状态(进行到哪一步、是否就绪)。"""
    st = vm_state_get()
    if st["status"] == "ready":
        return f"虚拟机就绪(实例 {VM_INSTANCE[:8]},串口 {st['port']})。"
    if st["status"] == "error":
        return f"虚拟机出错:{st['error']}"
    return "虚拟机在后台启动中(agent 退出时销毁)。"


@tool(
    description="在沙箱虚拟机里执行一条命令,通过 socket 连 guest 内的 vmserver 执行(JSON 协议,非 SSH)。"
                "适合在隔离的完整系统里装软件、跑服务、做重活。若虚拟机还没就绪,会返回当前进度并让你稍后再试。"
                "命令默认最长跑 60 秒;构建、测试、下载这类长任务传 long_lived=true(上限 3 小时)。"
                "**特别久的活更推荐丢后台**:`nohup 命令 > /tmp/x.log 2>&1 &`,再隔一会儿查一次 —— "
                "连接是短的,中途断了也不影响任务本身。"
                "别用交互式命令(vim/top 等,它们没有终端会直接失败)。"
                "路径注意:客户机是 Linux,路径风格与宿主不同(没有 D:\\ 那套)。",
    parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要在客户机里执行的 shell 命令",
                    },
                    "long_lived": {
                        "type": "boolean",
                        "description": "长任务模式:超时上限从 60 秒提到 3 小时。构建/测试/下载用",
                    },
                },
                "required": ["command"],
            },
)
def vm_run(command: str, long_lived: bool = False) -> str:
    """在沙箱虚拟机里执行一条命令 —— 连接 guest 里的 vmserver(socket, JSON 协议)。

    vmserver 用无 TTY 的 subprocess 跑命令、自带超时(超时就 kill,返回 timed_out),
    所以交互程序(vim/top)会直接秒失败、不会卡死会话;命令输出是结构化 JSON,无壳提示符。

    long_lived=True 把上限从 60 秒提到 3 小时,给构建/测试/下载这类长任务用。
    """
    st = vm_state_get()
    if st["status"] != "ready":
        return f"虚拟机还没就绪,当前:{st['step'] or st['status']}。请用 vm_status 或稍后再试。"
    if not _vm_vmserver_host_port or not _vm_token:
        return "虚拟机 vmserver 未就绪,请稍后再试。"

    import socket as sk, json
    timeout = VM_LONG_TIMEOUT if long_lived else VM_CMD_TIMEOUT
    req = {"token": _vm_token, "cmd": "exec", "command": command, "timeout": timeout}
    try:
        s = sk.create_connection(("127.0.0.1", _vm_vmserver_host_port), timeout=10)
        # 跟 timeout 联动,别硬编码 —— 原来是死值 90,长任务会被宿主先掐断,
        # 而 guest 那边还在跑(结果就没人接了)。
        s.settimeout(timeout + 30)
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
    st = vm_state_get()
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
    st = vm_state_get()
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
    st = vm_state_get()
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
        # 空闲超时给足(3 小时)。原来是 60 秒:HTTP 那种请求-响应太快、碰不到,但 SSH
        # 这类长连接空闲一分钟就会被关掉方向、会话卡死。对端**真的**断开时 recv 会返回
        # 空串正常收尾,不靠这个超时兜底,所以放大它是安全的。
        up.settimeout(VM_LONG_TIMEOUT)
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
    st = vm_state_get()
    if st["status"] != "ready":
        return f"虚拟机还没就绪,当前:{st['step'] or st['status']}。请用 vm_status 或稍后再试。"
    if not _vm_vmserver_host_port or not _vm_token:
        return "虚拟机 vmserver 未就绪,请稍后再试。"
    import socket as sk
    if int(host_port) in _vm_tunnels:
        vm_tunnel_stop(int(host_port))
    srv = sk.socket(sk.AF_INET, sk.SOCK_STREAM)
    # Windows 上 SO_REUSEADDR 的语义是"允许抢占":两个 socket 绑同一端口不报错,
    # 连接最终落到谁手里不确定。实测的表现是 —— 端口已被占用时这里照样"开通成功",
    # 用户 SSH 过来可能连到别的程序上。Windows 必须用 SO_EXCLUSIVEADDRUSE(独占);
    # 其它平台沿用 SO_REUSEADDR:那里它只为跨过 TIME_WAIT,不会抢占活跃的监听。
    if hasattr(sk, "SO_EXCLUSIVEADDRUSE"):
        srv.setsockopt(sk.SOL_SOCKET, sk.SO_EXCLUSIVEADDRUSE, 1)
    else:
        srv.setsockopt(sk.SOL_SOCKET, sk.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", int(host_port)))
    except OSError as exc:
        # 别把原始的 WinError 10048 抛出去 —— 模型看到"套接字地址只允许使用一次"
        # 根本不知道该干什么;而 vm_ssh_login 那头连"用户和 sshd 都已经弄好了、
        # 只是端口没开成"也说不清楚。这里给个人话的说法,顺带指出出路。
        srv.close()
        return (f"宿主导 {host_port} 开不了({exc.strerror or exc}),多半已经被占用 —— "
                f"换一个,或者不指定端口(工具会自动从 {_VM_SSH_PORT_BASE} 起挑一个空闲的)。")
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


_VM_SSH_USER = os.environ.get("VM_SSH_USER", "dev")             # 登录名固定,可用 .env 覆盖
_VM_SSH_PORT_BASE = int(os.environ.get("VM_SSH_PORT", "2222"))  # 宿主导从这儿开始找空闲的


@tool(
    description="为用户开一个「从宿主 SSH 登录虚拟机」的入口:重置登录用户的密码(每次都是"
                "新的随机密码)、确保 sshd 在运行且允许密码登录,再把 guest 的 22 端口映射到"
                "宿主导,最后给出**可直接复制给用户的连接命令**。"
                "用户说「我想 SSH 进去自己装/自己操作」「给我一个能连虚拟机的端口」时用它。",
    parameters={
                "type": "object",
                "properties": {
                    "host_port": {"type": "integer",
                                  "description": "宿主导口;不填会自动挑一个空闲的"},
                },
            },
)
def vm_ssh_login(host_port: int = 0) -> str:
    """开一个宿主 → guest 的 SSH 入口,返回给用户看的连接信息。

    为什么做成一个工具、而不是写几条命令让模型自己拼:这套动作里有一串必须做对的细节
    —— 密码要随机、配置改动要幂等(反复调用不能把 sshd 弄坏)、隧道要挑空闲端口、连接
    命令里的保活参数还不能漏。漏任何一步,用户拿到的就是连不上、或者连上就断的命令。
    顺带,密码只在这里生成并交给用户,不会随着命令回显长期停在对话上下文里。
    """
    st = vm_state_get()
    if st["status"] != "ready":
        return f"虚拟机还没就绪({st['step'] or st['status']}),稍后再试。"

    user = _VM_SSH_USER
    password = secrets.token_urlsafe(12)        # 每次调用都换一把新的

    # 一条脚本做完:确认 sshd 在(基础镜像预装的,不装它)、建用户(幂等)、重置密码、
    # 确保允许密码登录、没在跑就起。guest 里没有交互式终端,拆成多条跑没有意义。
    script = (
        "set -e\n"
        "[ -x /usr/sbin/sshd ] || { echo MISSING_SSHD; exit 3; }\n"
        f"id -u {_shq(user)} >/dev/null 2>&1 || adduser -D -s /bin/ash {_shq(user)}\n"
        "if command -v chpasswd >/dev/null 2>&1; then\n"
        f"    echo {_shq(f'{user}:{password}')} | chpasswd\n"
        "else\n"
        f"    printf '%s\\n%s\\n' {_shq(password)} {_shq(password)} | passwd {_shq(user)}\n"
        "fi\n"
        "grep -qE '^[[:space:]]*PasswordAuthentication[[:space:]]+yes'"
        " /etc/ssh/sshd_config 2>/dev/null"
        " || echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config\n"
        "pgrep -x sshd >/dev/null 2>&1 || /usr/sbin/sshd\n"
        "echo SSH_READY\n"
    )
    r = _vm_exec_raw(script, timeout=30)
    body = f"{r.get('error') or ''}\n{r.get('output') or ''}".strip()
    if not r.get("ok") or "SSH_READY" not in body:
        if "MISSING_SSHD" in body:
            return ("虚拟机里没有 /usr/sbin/sshd —— 这个基础镜像没预装 openssh。"
                    "换回预装了它的镜像,或在 guest 里装上再试。")
        return f"启动 sshd 失败:{body[:400] or '(无输出)'}"

    port = int(host_port) or _vm_free_port(_VM_SSH_PORT_BASE)
    tunnel = vm_tunnel(port, 22)                # guest:22 → 宿主:port(字节透明,SSH 能过)

    return (
        f"已开好 SSH 入口。把下面这行给用户(可直接复制):\n\n"
        f"    ssh -o ServerAliveInterval=30 ssh://{user}@127.0.0.1:{port}\n\n"
        f"用户名 {user},密码 {password}(每次开通都会换一把新的)。\n"
        f"保活参数建议留着:转发链路空闲 3 小时才会断,带着更稳,也能及早发现对面已经掉了。\n"
        f"({tunnel})"
    )


def _pid_is_our_qemu(pid: int) -> bool:
    """确认这个 pid 现在跑的**确实是我们那个 QEMU**。

    pid 会被系统回收,光凭一个记下来的数字就去杀,有杀错无关进程的风险 ——
    这里核一下它的可执行文件路径,对不上就不动。
    """
    if pid <= 0:
        return False
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k = ctypes.windll.kernel32
        handle = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_uint(1024)
            if not k.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return False
            return Path(buf.value).name.lower() == VM_QEMU_SYSTEM.name.lower()
        finally:
            k.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return False


def _vm_cleanup_stale() -> int:
    """启动时清扫上次异常退出留下的孤儿 QEMU,返回清掉的个数。

    为什么需要:QEMU 是普通子进程,Windows 上**父进程死不会连坐**,而被强杀
    (任务管理器、timeout、崩溃)时 atexit 根本不跑 —— 于是留下孤儿,还锁着
    某个会话的磁盘(删不掉、/vmreset 失效)。这里兜底清一次。

    **只清"三个条件同时满足"的**,一条不够都不动手:
      1. 会话目录里记着一个 pid(qemu.pid)—— 只认我们自己写下过的;
      2. 那个 pid 还活着,但它的**所属 agent 已经不在了** —— 有人正用着就不碰;
      3. 该 pid 现在跑的确实是我们的 qemu 可执行文件 —— 防 pid 被回收后误杀。
    绝不按进程名批量杀:那会误伤别的 agent 实例、甚至用户自己的虚拟机。
    """
    from ..session import SESSIONS_DIR, pid_alive   # 延迟导入,避免与 session 成环
    if not SESSIONS_DIR.exists():
        return 0

    killed = 0
    for sid_dir in SESSIONS_DIR.iterdir():
        if not sid_dir.is_dir():
            continue
        pid_file = sid_dir / "qemu.pid"
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip() or 0)
        except Exception:  # noqa: BLE001
            continue

        if not pid_alive(pid):
            pid_file.unlink(missing_ok=True)          # 进程早已不在,陈迹清掉
            continue

        # 是本进程此刻正在用的那台?那不能动
        if _vm_proc is not None and getattr(_vm_proc, "pid", None) == pid:
            continue

        # 有**别的**活着的 agent 在用这个会话?那这台 QEMU 是它的,别动。
        # 注意要排除"owner 就是自己"的情形:启动时是先 claim_owner(写下自己的 pid)
        # 再走到这里的,若不排除,就会把"本会话里上次残留的那台"当成自己的而被放过
        # —— 实测正是这样漏掉了孤儿。
        owner_pid = 0
        try:
            owner_pid = int(json.loads(
                (sid_dir / "owner.json").read_text(encoding="utf-8") or "{}").get("pid") or 0)
        except Exception:  # noqa: BLE001
            owner_pid = 0
        if owner_pid and owner_pid != os.getpid() and pid_alive(owner_pid):
            continue

        if not _pid_is_our_qemu(pid):
            pid_file.unlink(missing_ok=True)          # pid 被回收给别人了,只清记录
            continue

        try:
            # 按**具体 pid** 杀,不是按进程名批量清 —— 后者会误伤别的实例
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=15)
            killed += 1
        except Exception:  # noqa: BLE001
            pass
        pid_file.unlink(missing_ok=True)
    return killed


def vm_switch_session() -> None:
    """会话切换后重启虚拟机,让它挂到**新会话自己那块盘**上。

    每个会话一块磁盘,所以换会话就必须换虚拟机 —— 否则新会话的历史配着旧会话的
    机器接着用,两边对不上(你以为在 A 的环境里,实际操作的是 B 的文件系统)。
    收掉当前这台时会先让 guest 刷盘(见 _vm_cleanup),不会丢数据。
    """
    global _vm_gen
    _vm_gen += 1                       # 先作废正在跑的启动流程(见 _vm_worker 的说明)
    _vm_cleanup()                      # 刷盘 + 停掉旧会话的 VM + 关转发
    _vm_stop_thread()                  # 还要等线程真的结束 —— 原来只是置 None,线程仍在跑
    _vm_state_set("idle", "会话已切换,虚拟机将重新启动")
    vm_kickoff()


def vm_reset() -> str:
    """把本会话的虚拟机恢复成出厂状态:停掉它、删掉会话磁盘,再用基础镜像重建。

    磁盘现在跟着会话持久化(装过的软件、写过的文件都在里面),所以需要一个
    "推倒重来"的口子,否则那块盘只会越长越大。**不可恢复**:VM 里所有东西都会没。
    工作区文件与长期记忆不受影响。
    """
    global _vm_gen
    _vm_gen += 1                # 先作废正在跑的启动流程 —— 它会在下一个检查点自行退出
    _vm_kill_current()          # 再停掉已经起来的 QEMU:它占着盘就删不掉(实测 WinError 32)
    _vm_stop_thread()           # 等线程收工;它此时已知自己作废,不会再去拉新的
    time.sleep(0.3)             # 最后留一点时间让系统彻底放掉文件句柄

    try:
        _vm_disk().unlink(missing_ok=True)
    except OSError as exc:
        return (f"虚拟机磁盘删不掉({exc})—— 多半还有别的进程占着它。"
                f"确认没有其它 agent 实例在跑同一个会话后重试。")

    _vm_state_set("idle", "已重置,等待重新启动")
    vm_kickoff()
    return "虚拟机已重置,正在用基础镜像重启。"


def _vm_cleanup() -> None:
    """agent 退出时:杀掉本实例的 QEMU、关闭转发、清掉日志,**但保留磁盘**。

    磁盘现在属于会话(见 VM_DISK 的注释),会话恢复时要靠它把虚拟机里的东西带回来,
    所以这里**不能删** —— 想从干净状态重来要显式重置(见 vm_reset)。
    只动本进程的 _vm_proc,不 taskkill /IM,以免误杀其它 agent 或用户自己的 VM。

    **本进程没起过 VM 就直接返回**:这个函数是 atexit 注册的,而注册发生在**导入
    本模块**时 —— 任何只是 import 了 vm 的短命脚本(巡检、测试、一次性工具),
    退出时都会跑它,顺手删掉"当前会话"的 qemu.pid 和日志,把别人依赖的孤儿标记
    抹掉。没起过 VM 就没什么要清的,直接走人。
    """
    if _vm_proc is None:
        return
    # 退出前先让 guest 把页缓存刷到虚拟磁盘。磁盘是会话资产、下次要接着用,而 QEMU
    # 是被 kill 的 —— guest 自己内存里还没落盘的写入会随它一起消失(实测:不 sync 时
    # 刚写的文件重启就没了,sync 之后能留下)。best-effort,连不上就算了。
    try:
        if vm_state_get().get("status") == "ready":
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
    try:
        # 干净退出,把 pid 记录也清掉 —— 留着的话下次启动会把它当孤儿去查
        _qemu_pid_file(current_session_id()).unlink(missing_ok=True)
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
    vm_kickoff()
    return f"虚拟机正在后台启动({VM_INSTANCE[:8]}),可查 vm_status。"
