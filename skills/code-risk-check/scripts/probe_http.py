#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记录型 HTTP 服务 —— 把待检样本的出站请求引到观察点。

它假装成目标服务器:收下请求(方法、路径、头、体)写进日志,
然后回一个空的 200,**不把数据转发到任何地方**。

用法(在 VM 内,配合 hosts 劫持):
    echo "127.0.0.1 evil.example.com" >> /etc/hosts
    python3 probe_http.py --port 80 --log /root/http.log &

日志是 JSON Lines,一行一个请求。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG_PATH = "/root/http.log"
MAX_BODY = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "probe/1.0"

    def _log(self, body):
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "client": self.client_address[0],
            "method": self.command,
            "path": self.path,
            "headers": {k: v for k, v in self.headers.items()},
            "body": body[:MAX_BODY],
            "body_len": len(body),
        }
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            sys.stderr.write("写日志失败: %s\n" % exc)
        sys.stderr.write("[probe] %s %s %s (%d bytes)\n" % (self.command, self.path, self.client_address[0], len(body)))
        sys.stderr.flush()

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        self._log(body)
        payload = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _handle

    def log_message(self, *a):
        pass


def main():
    global LOG_PATH
    ap = argparse.ArgumentParser(description="记录型 HTTP 服务(不转发数据)")
    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--log", default=LOG_PATH)
    args = ap.parse_args()
    LOG_PATH = args.log

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    sys.stderr.write("[probe] 监听 %s:%d,日志 %s\n" % (args.bind, args.port, LOG_PATH))
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
