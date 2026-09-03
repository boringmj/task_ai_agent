#!/usr/bin/env python3
"""Alpine 沙箱管理服务端:单入口(带 token)+ 执行命令 + 按需代理 guest 端口。

协议:每个连接先读一行 JSON 请求(换行结尾)。
  {"token":"...", "cmd":"exec",  "command":"...", "timeout":30}
  {"token":"...", "cmd":"proxy", "host":"127.0.0.1", "port":8080}

exec  响应一行 JSON: {"ok":bool, "output":str, "rc":int, "timed_out":bool}
proxy 先回 {"ok":true,"proxy":true},之后该连接转为到 guest:host:port 的原始双向透传。

安全:
- token 每次请求都读 /root/.vm_token;无 token 文件 => 全部拒绝(镜像刚起时锁死)
- token 不对 => 拒绝
- 命令用**无 TTY 的 subprocess** 跑,自带超时,超时就 kill ——
  交互程序(vim/top)因为没有 TTY 会自动失败,不会接管终端卡会话
"""
import json
import os
import socket
import subprocess
import sys
import threading

PORT = int(os.environ.get("VMSERVER_PORT", "40000"))
TOKEN_FILE = os.environ.get("VMSERVER_TOKEN_FILE", "/root/.vm_token")
MAX_OUTPUT = 200_000       # 单次命令输出上限,防撑爆内存
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600


def current_token() -> str:
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def respond(conn, obj) -> None:
    conn.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))


def handle_exec(conn, req) -> None:
    command = req.get("command", "").strip()
    if not command:
        respond(conn, {"ok": False, "error": "empty command"})
        return
    timeout = max(1, min(int(req.get("timeout", DEFAULT_TIMEOUT)), MAX_TIMEOUT))
    # 无 TTY:交互程序(vim/top)拿不到终端,直接快速失败
    proc = subprocess.Popen(command, shell=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            out, _ = proc.communicate(timeout=3)
        except Exception:
            out = b""
        timed_out = True
    output = out[:MAX_OUTPUT].decode("utf-8", "replace")
    respond(conn, {"ok": True, "output": output, "rc": proc.returncode,
                   "timed_out": timed_out})


def _pump(src, dst) -> None:
    """把一个 socket 的数据透传到另一个。"""
    try:
        while True:
            data = src.recv(4096)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    try:
        dst.shutdown(socket.SHUT_WR)
    except Exception:
        pass


def handle_proxy(conn, req) -> None:
    host = req.get("host", "127.0.0.1")
    try:
        port = int(req.get("port", 0))
    except (TypeError, ValueError):
        respond(conn, {"ok": False, "error": "bad port"})
        return
    try:
        upstream = socket.create_connection((host, port), timeout=10)
    except OSError as exc:
        respond(conn, {"ok": False, "error": f"connect {host}:{port} failed: {exc}"})
        return
    respond(conn, {"ok": True, "proxy": True})
    conn.settimeout(30)
    upstream.settimeout(30)
    t1 = threading.Thread(target=_pump, args=(conn, upstream), daemon=True)
    t2 = threading.Thread(target=_pump, args=(upstream, conn), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    try:
        conn.close()
        upstream.close()
    except Exception:
        pass


def handle(conn) -> None:
    conn.settimeout(20)
    try:
        line = conn.makefile("rb").readline()
        if not line:
            return
        req = json.loads(line.decode("utf-8", "replace"))
    except Exception:
        try:
            respond(conn, {"ok": False, "error": "bad request"})
        except Exception:
            pass
        return
    # 每次请求都读 token(改 token 立即生效,无需重启服务)
    stored = current_token()
    if not stored:
        respond(conn, {"ok": False, "error": "locked: no token set"})
        return
    if req.get("token") != stored:
        respond(conn, {"ok": False, "error": "auth failed"})
        return
    cmd = req.get("cmd")
    if cmd == "exec":
        handle_exec(conn, req)
    elif cmd == "proxy":
        handle_proxy(conn, req)
    else:
        respond(conn, {"ok": False, "error": f"unknown cmd {cmd!r}"})


def main() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PORT))
    srv.listen(32)
    sys.stdout.write(f"vmserver listening on :{PORT}\n")
    sys.stdout.flush()
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
