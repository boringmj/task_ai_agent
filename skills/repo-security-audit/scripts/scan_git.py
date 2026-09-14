#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""repo-security-audit —— git 历史扫描器

普通静态扫描只看工作区当前的文件。但密钥删过、配置清过、分支 rebase 过 ——
这些内容仍留在 .git 对象库里,而且往往**比当前代码更值钱**:
被删掉的 .env、旧版的硬编码口令、force-push 丢弃的提交,攻击者拿得到仓库就都能翻出来。

容器里没有 git 二进制,所以这里用纯 Python 的 dulwich 直接读对象库。

用法(路径相对技能目录;在容器里的实际位置见加载技能时给的资源清单):
    python scripts/scan_git.py <仓库路径> [选项]

选项:
    --out FILE        输出 JSON(默认 git-findings.json)
    --max-blobs N     最多扫描多少个历史 blob(默认 5000)
    --max-size BYTES  单个 blob 大小上限(默认 2MB)
    --patterns        额外用 sinks 规则扫历史代码(默认只扫密钥)
    --dangling        额外扫不可达对象(rebase / force-push 遗留,较慢)
    --top N           摘要里列前 N 条(默认 25)

依赖 dulwich(不是 git 命令):
    pip install --target .pylibs dulwich \
        -i https://pypi.tuna.tsinghua.edu.cn/simple
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scan as base  # noqa: E402  复用规则加载与密钥扫描

# 历史里出现过、且文件名本身就敏感的路径
SENSITIVE_NAME_RX = re.compile(
    r"(?i)(^|/)("
    r"\.env(\..+)?|\.git-credentials|\.netrc|\.npmrc|\.pypirc|\.htpasswd|"
    r"id_(rsa|dsa|ecdsa|ed25519)|.*\.(pem|key|p12|pfx|jks|keystore|ppk|asc|gpg)|"
    r".*\.(sql|sqlite|sqlite3|dump|bak|backup|old|orig|save)$|"
    r".*\.(tar|tar\.gz|tgz|zip|7z|rar)$|"
    r".*(secret|password|passwd|credential|token|apikey|api_key|private).*"
    r"\.(json|ya?ml|ini|conf|cfg|properties|txt|xml|env|php)$"
    r")$"
)

SKIP_BLOB_SIZE = 1024 * 1024  # 超过 1MB 记为"大对象"


def eprint(*args) -> None:
    print(*args, file=sys.stderr)


def hx(sha) -> str:
    """dulwich 1.2 的 sha 可能是 bytes 或 FixedSha 包装对象,统一取十六进制串。"""
    if isinstance(sha, bytes):
        return sha.decode()
    for attr in ("hexdigest", "hex"):
        meth = getattr(sha, attr, None)
        if callable(meth):
            try:
                return meth()
            except Exception:  # noqa: BLE001
                pass
    return str(sha)


def is_file_blob(mode) -> bool:
    """普通文件(排除目录 / 符号链接 / 子模块)。"""
    return (mode & 0o170000) == 0o100000


def decode_bytes(data: bytes) -> str | None:
    for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def collect_history(repo, max_commits: int = 300000):
    """遍历所有 ref 可达的提交,收集历史 blob 与作者。

    返回 (hist, deleted_paths, n_commits, authors):
      hist           blob sha(bytes) -> {path, commit, time}
      deleted_paths  曾在历史中被删掉的路径
      authors        作者 -> 提交数
    """
    from dulwich.object_store import iter_tree_contents
    from dulwich.diff_tree import tree_changes

    refs = {sha for sha in repo.get_refs().values() if sha}
    if not refs:
        return {}, set(), 0, Counter()

    hist: dict[bytes, dict] = {}
    deleted: set[str] = set()
    authors: Counter = Counter()
    n_commits = 0

    for walk in repo.get_walker(include=list(refs)):
        commit = walk.commit
        n_commits += 1
        if n_commits > max_commits:
            eprint(f"[!] 提交数超过 {max_commits},提前停止")
            break
        raw_author = commit.author or b""
        authors[raw_author.decode("utf-8", "replace")] += 1
        csha = hx(commit.id) if getattr(commit, "id", None) else hx(commit.sha())

        # 根提交没有父,整个 tree 都是"新增"
        if not commit.parents:
            for entry in iter_tree_contents(repo.object_store, commit.tree):
                if is_file_blob(entry.mode):
                    hist[bytes(entry.sha)] = {
                        "path": entry.path.decode("utf-8", "replace"),
                        "commit": csha, "time": commit.commit_time}
            continue

        try:
            parent = repo.object_store[bytes(commit.parents[0])]
            changes = list(tree_changes(repo.object_store, parent.tree, commit.tree))
        except Exception as exc:  # noqa: BLE001
            eprint(f"[!] 提交 {csha[:8]} 差异计算失败,跳过: {type(exc).__name__}")
            continue

        for change in changes:
            if change.new is not None and is_file_blob(change.new.mode):
                hist[bytes(change.new.sha)] = {
                    "path": change.new.path.decode("utf-8", "replace"),
                    "commit": csha, "time": commit.commit_time}
            if change.old is not None and change.new is None:
                deleted.add(change.old.path.decode("utf-8", "replace"))
    return hist, deleted, n_commits, authors


def head_snapshot(repo) -> dict[str, bytes]:
    """当前所有分支 tip 的文件快照:path -> blob sha。"""
    from dulwich.object_store import iter_tree_contents

    snap: dict[str, bytes] = {}
    for sha in set(repo.get_refs().values()):
        try:
            obj = repo.object_store[bytes(sha)]
            if obj.type_name == b"tag":
                obj = repo.object_store[bytes(obj.object[1])]
            if obj.type_name != b"commit":
                continue
            for entry in iter_tree_contents(repo.object_store, obj.tree):
                if is_file_blob(entry.mode):
                    snap[entry.path.decode("utf-8", "replace")] = bytes(entry.sha)
        except Exception:  # noqa: BLE001
            continue
    return snap


def scan_blob(repo, bsha: bytes, path: str, opts, secret_rules, pattern_rules):
    """扫一个历史 blob,返回 (findings, is_large)。"""
    try:
        blob = repo.object_store[bsha]
    except Exception:  # noqa: BLE001
        return [], False
    data = blob.data
    if len(data) > opts.max_size:
        return [], len(data) > SKIP_BLOB_SIZE
    if b"\x00" in data[:8192]:
        return [], False
    text = decode_bytes(data)
    if text is None:
        return [], False
    lines = text.splitlines()
    out = []
    try:
        out += base.scan_secrets(path, lines, secret_rules)
        if pattern_rules:
            out += base.scan_patterns(path, lines, base.lang_of(path), pattern_rules)
    except Exception as exc:  # noqa: BLE001
        eprint(f"[!] 扫描 {path} 出错: {type(exc).__name__}: {exc}")
    return out, len(data) > SKIP_BLOB_SIZE


def main(argv=None) -> int:
    try:
        from dulwich.repo import Repo
    except ImportError:
        eprint("[!] 缺少 dulwich(容器里没有 git 命令,靠它读对象库):")
        eprint("    pip install --target .pylibs dulwich "
               "-i https://pypi.tuna.tsinghua.edu.cn/simple")
        return 3

    ap = argparse.ArgumentParser(description="git 历史安全扫描")
    ap.add_argument("repo", help="仓库路径,如 clones/foo")
    ap.add_argument("--out", default="git-findings.json")
    ap.add_argument("--max-blobs", type=int, default=5000)
    ap.add_argument("--max-size", type=int, default=2 * 1024 * 1024)
    ap.add_argument("--patterns", action="store_true")
    ap.add_argument("--dangling", action="store_true")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--json-only", action="store_true",
                    help="只写 JSON,不打印摘要")
    opts = ap.parse_args(argv)

    root = os.path.abspath(opts.repo)
    if not os.path.isdir(os.path.join(root, ".git")) and \
       not os.path.isfile(os.path.join(root, ".git")):
        eprint(f"[!] 不是 git 仓库(找不到 .git): {root}")
        return 2

    repo = Repo(root)
    secret_rules = base.load_rules("secrets.yaml")
    pattern_rules = base.load_rules("sinks.yaml") if opts.patterns else []

    hist, deleted_paths, n_commits, authors = collect_history(repo)
    head = head_snapshot(repo)

    findings: list[dict] = []
    large: list[tuple[str, int, str]] = []
    scanned = 0
    hit_paths: set[str] = set()

    for bsha, meta in hist.items():
        if scanned >= opts.max_blobs:
            eprint(f"[!] 已达 --max-blobs={opts.max_blobs},剩余历史未扫")
            break
        path = meta["path"]
        cur = head.get(path)
        if cur is not None and cur == bsha:
            continue                      # 当前文件还是这份内容,scan.py 已经覆盖过
        status = "deleted" if path not in head else "changed"
        hits, is_large = scan_blob(repo, bsha, path, opts, secret_rules, pattern_rules)
        scanned += 1
        if is_large:
            large.append((path, len(repo.object_store[bsha].data), meta["commit"]))
        for hit in hits:
            hit["source"] = "git-history"
            hit["commit"] = meta["commit"][:10]
            hit["git_status"] = status
            if status == "deleted":
                hit["severity"] = _bump(hit["severity"])
                hit["detail"] = "该文件已从当前分支删除,但内容仍在 git 历史中"
            findings.append(hit)
        if hits:
            hit_paths.add(path)

    # 已删除的敏感文件名
    gone = (set(p for p in (m["path"] for m in hist.values())) | deleted_paths) - set(head)
    for path in sorted(gone):
        if not SENSITIVE_NAME_RX.search(path):
            continue
        sev = "high" if re.search(r"(?i)(\.env|id_rsa|\.pem|\.key|secret|password|credential)", path) else "medium"
        findings.append({
            "id": "GIT-DELETED-SENSITIVE-FILE",
            "category": "A05:2021-Security-Misconfiguration",
            "cwe": "CWE-538",
            "severity": sev,
            "confidence": "high",
            "file": path,
            "line": 0,
            "snippet": "该文件曾存在于仓库,现已不在当前分支",
            "message": "历史中存在敏感文件名,内容仍可从 .git 取出",
            "remediation": "用 git filter-repo / BFG 清除历史并强制轮换其中涉及的凭据",
            "source": "git-history",
            "detail": "即使当前分支看不到,clone 仓库即可用 git show 还原",
        })

    # 历史大对象
    for path, size, csha in large[:200]:
        findings.append({
            "id": "GIT-LARGE-BLOB",
            "category": "A05:2021-Security-Misconfiguration",
            "cwe": "CWE-1104",
            "severity": "low",
            "confidence": "high",
            "file": path,
            "line": 0,
            "snippet": f"{size // 1024} KB",
            "message": "历史里存在较大的对象(可能是数据导出 / 备份 / 打包产物)",
            "remediation": "确认内容是否含敏感数据;用 filter-repo 清理以缩减仓库",
            "source": "git-history",
            "commit": csha[:10],
        })

    # 作者信息(通常是内部域名 / 邮箱,信息级)
    emails = sorted({a.split("<")[-1].rstrip(">").strip()
                     for a in authors if "<" in a})
    if emails:
        findings.append({
            "id": "GIT-AUTHOR-EMAILS",
            "category": "A09:2021-Security-Logging-and-Monitoring-Failures",
            "cwe": "CWE-200",
            "severity": "info",
            "confidence": "high",
            "file": "(git 元数据)",
            "line": 0,
            "snippet": ", ".join(emails[:8]) + (" ..." if len(emails) > 8 else ""),
            "message": f"提交历史里包含 {len(emails)} 个作者邮箱(可用于钓鱼 / 撞库定向)",
            "remediation": "对外公开的仓库考虑清理历史中的邮箱,或统一用 noreply 地址",
            "source": "git-history",
        })

    # 不可达对象(rebase / force-push 遗留)
    if opts.dangling:
        extra = 0
        for sha in repo.object_store:
            if extra >= opts.max_blobs:
                break
            try:
                obj = repo.object_store[sha]
            except Exception:  # noqa: BLE001
                continue
            if obj.type_name != b"blob" or bytes(sha) in hist:
                continue
            data = obj.data
            if len(data) > opts.max_size or b"\x00" in data[:8192]:
                continue
            text = decode_bytes(data)
            if text is None:
                continue
            extra += 1
            for hit in base.scan_secrets("(dangling)", text.splitlines(), secret_rules):
                hit["source"] = "git-history"
                hit["git_status"] = "dangling"
                hit["detail"] = "对象未被任何提交引用(rebase / force-push 遗留)"
                findings.append(hit)
        eprint(f"[i] dangling 模式额外扫了 {extra} 个不可达 blob")

    seen = set()
    uniq = []
    for f in findings:
        key = (f["id"], f["file"], f["line"], f.get("commit", ""))
        if key not in seen:
            seen.add(key)
            uniq.append(f)
    findings = sorted(uniq, key=lambda f: (base.SEV_ORDER.get(f["severity"], 9), f["file"]))

    stats = {"target": root, "commits": n_commits, "history_blobs": len(hist),
             "blobs_scanned": scanned, "current_files": len(head),
             "deleted_paths": len(gone), "total": len(findings),
             "out": os.path.abspath(opts.out)}
    with open(opts.out, "w", encoding="utf-8") as fh:
        json.dump({"stats": stats, "findings": findings}, fh, ensure_ascii=False, indent=2)

    sev = Counter(f["severity"] for f in findings)
    if opts.json_only:
        return 0
    print("=" * 68)
    print(f"git 历史扫描: {root}")
    print(f"提交 {n_commits} 个 | 历史 blob {len(hist)} 个(扫了 {scanned}) | "
          f"当前文件 {len(head)} 个 | 历史中已消失的路径 {len(gone)} 个")
    print(f"候选 {len(findings)} 条   " + "  ".join(
        f"{s}={sev.get(s, 0)}" for s in ("critical", "high", "medium", "low", "info")))
    if findings:
        print("-" * 68)
        for f in findings[:opts.top]:
            tag = f.get("git_status", "")
            print(f"  [{f['severity'].upper():8s}] {f['id']}  {f['file']}"
                  + (f"  ({tag}, {f.get('commit', '')})" if tag else ""))
            print(f"             {f['message']}")
            if f["snippet"]:
                print(f"             > {f['snippet'][:110]}")
    print("=" * 68)
    print(f"完整结果已写入: {stats['out']}")
    return 0


def _bump(severity: str) -> str:
    """已删除但仍留在历史里 —— 定为"秘密没被清除",提一档。"""
    order = ["info", "low", "medium", "high", "critical"]
    if severity in order and order.index(severity) < len(order) - 1:
        return order[order.index(severity) + 1]
    return severity


if __name__ == "__main__":
    sys.exit(main())
