#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""关键路径快照与比对 —— 观察样本运行时对文件系统的改动。

用法:
    python3 fs_snapshot.py snap /root/before.json [--paths /etc,/root,/tmp]
    python3 fs_snapshot.py diff /root/before.json /root/after.json

只读:不修改、不删除任何文件;用于"跑样本前拍一张、跑完再拍一张"。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

DEFAULT_PATHS = [
    "/etc", "/root", "/home", "/tmp", "/var/spool/cron", "/var/spool/at",
    "/usr/local/bin", "/usr/local/sbin", "/opt", "/srv", "/boot",
    "/etc/init.d", "/etc/systemd", "/etc/cron.d", "/var/log",
]
MAX_HASH_BYTES = 4 * 1024 * 1024
SKIP_DIRS = {"/proc", "/sys", "/dev", "/run"}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 16)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def record(path, st):
    if os.path.islink(path):
        kind = "symlink"
        try:
            target = os.readlink(path)
        except OSError:
            target = "?"
        return {"type": kind, "target": target, "mtime": int(st.st_mtime)}
    if os.path.isdir(path):
        return {"type": "dir", "mtime": int(st.st_mtime)}
    item = {"type": "file", "size": st.st_size, "mtime": int(st.st_mtime), "mode": oct(st.st_mode & 0o7777)}
    if st.st_size <= MAX_HASH_BYTES:
        try:
            item["sha256"] = sha256(path)
        except OSError:
            item["sha256"] = None
    return item


def snapshot(paths):
    out = {}
    for root in paths:
        if not os.path.exists(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            if any(dirpath == s or dirpath.startswith(s + "/") for s in SKIP_DIRS):
                dirnames[:] = []
                continue
            for name in dirnames + filenames:
                full = os.path.join(dirpath, name)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                try:
                    out[full] = record(full, st)
                except OSError:
                    continue
    return out


def compare(before, after):
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = []
    for p in sorted(set(before) & set(after)):
        b, a = before[p], after[p]
        if b == a:
            continue
        if b.get("type") == "dir" and a.get("type") == "dir":
            continue  # 目录 mtime 变动太频繁,忽略
        detail = []
        if b.get("sha256") != a.get("sha256") and a.get("sha256") is not None:
            detail.append("内容变化")
        elif b.get("size") != a.get("size"):
            detail.append("大小 %s→%s" % (b.get("size"), a.get("size")))
        if b.get("mode") != a.get("mode"):
            detail.append("权限 %s→%s" % (b.get("mode"), a.get("mode")))
        if b.get("type") != a.get("type"):
            detail.append("类型 %s→%s" % (b.get("type"), a.get("type")))
        changed.append((p, ", ".join(detail) or "元数据变化", a.get("type")))
    return added, removed, changed


def print_report(path, added, removed, changed):
    print("=" * 72)
    print("文件系统改动: %s" % path)
    print("=" * 72)
    print("\n[新增] %d 项" % len(added))
    for p in added[:200]:
        print("  + %s" % p)
    if len(added) > 200:
        print("  ... 其余 %d 项省略" % (len(added) - 200))
    print("\n[删除] %d 项" % len(removed))
    for p in removed[:200]:
        print("  - %s" % p)
    if len(removed) > 200:
        print("  ... 其余 %d 项省略" % (len(removed) - 200))
    print("\n[修改] %d 项" % len(changed))
    for p, why, _ in changed[:200]:
        print("  ~ %s  (%s)" % (p, why))
    if len(changed) > 200:
        print("  ... 其余 %d 项省略" % (len(changed) - 200))
    print("\n提示:/tmp 与日志目录的波动是常态,按路径与内容判断相关性;")


def cmd_snap(args):
    paths = [p.strip() for p in args.paths.split(",") if p.strip()] if args.paths else DEFAULT_PATHS
    data = snapshot(paths)
    doc = {"created_at": time.time(), "paths": paths, "entries": data}
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)
    print("快照已写入 %s: %d 项(路径:%s)" % (args.output, len(data), ", ".join(paths)))
    return 0


def cmd_diff(args):
    with open(args.before, encoding="utf-8") as f:
        before = json.load(f)["entries"]
    with open(args.after, encoding="utf-8") as f:
        after = json.load(f)["entries"]
    added, removed, changed = compare(before, after)
    print_report("%s → %s" % (args.before, args.after), added, removed, changed)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({
                "added": added, "removed": removed,
                "changed": [{"path": p, "why": w} for p, w, _ in changed],
            }, f, ensure_ascii=False, indent=2)
        print("JSON 已写入: %s" % args.json_out)
    return 0


def main():
    ap = argparse.ArgumentParser(description="文件系统快照与比对(只读)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("snap", help="拍一份快照")
    s.add_argument("output")
    s.add_argument("--paths", help="逗号分隔的观察路径,默认覆盖常见系统位置")
    s.set_defaults(func=cmd_snap)

    d = sub.add_parser("diff", help="比对两份快照")
    d.add_argument("before")
    d.add_argument("after")
    d.add_argument("--json", dest="json_out")
    d.set_defaults(func=cmd_diff)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
