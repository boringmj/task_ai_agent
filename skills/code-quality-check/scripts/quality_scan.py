#!/usr/bin/env python3
"""quality_scan.py —— 代码质量候选扫描器。

只负责「圈候选」:函数规模、错误处理、类型滥用、近似重复,外加一个测试缺口信号。
它**不给出最终结论** —— 每条命中都要读上下文复核,判据见 references/dimensions.md。

用法:
    python3 quality_scan.py <目标路径> [--json out.json] [--min-level L2] [--window 6] [--quiet]
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict

# ------------------------------------------------------------------ 配置

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "vendor", "dist", "build", "target",
    "__pycache__", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache",
    "coverage", ".idea", ".vscode", "bower_components", ".next", ".nuxt",
    "out", "obj", ".gradle", "Pods", "site-packages", ".terraform",
}

LANG_EXT = {
    ".py": "py", ".pyi": "py",
    ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "js",
    ".ts": "ts", ".tsx": "ts",
    ".java": "java", ".kt": "java", ".kts": "java",
    ".go": "go",
    ".rb": "rb",
    ".php": "php",
    ".cs": "cs",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp", ".cxx": "cpp",
    ".rs": "rs",
    ".swift": "swift",
}

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_SNIPPET = 200

# 函数规模阈值(只用于圈候选,不是结论线)
FUNC_LINES = 50
FUNC_LINES_HARD = 120
NESTING = 4
NESTING_HARD = 6
BRANCHES = 10
BRANCHES_HARD = 25
ARGS = 5
ANY_OVERUSE = 5

# 重复检测
DUP_WINDOW = 6
DUP_MIN_LINES = 12

LEVEL_ORDER = {"L4": 0, "L3": 1, "L2": 2, "L1": 3, "L0": 4, "LX": 5}

TEST_RE = re.compile(
    r"(^|/)(tests?|specs?|__tests__)(/|$)"
    r"|(^|/)(test_[^/]+|[^/]+_test|[^/]+[._]test|[^/]+[._]spec|spec_[^/]+)\.[A-Za-z0-9]+$",
    re.I,
)

COMMENT_PREFIX = ("#", "//", "/*", "*/", "*", "--", "<!--")

NEST_TYPES = (
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith,
    ast.Try, ast.ExceptHandler,
)
BRANCH_TYPES = (
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler,
    ast.IfExp, ast.Assert, ast.comprehension,
)

# 跨语言的错误处理候选(只对非 Python 生效;Python 走 ast 精确分析)。
# 只留高信噪比的一条:空 catch。宽泛捕获(catch Exception / Throwable)在 PHP、Java
# 里太常见,脚本分不清「宽泛但已处理」与「宽泛且吞掉」,交给人工复核。
REGEX_RULES = [
    ("error-handling", "L3", "swallowed-exception",
     re.compile(r"catch\s*(\([^)\n]*\))?\s*\{\s*\}"),
     "catch 块为空,异常被吞"),
]


# ------------------------------------------------------------------ 工具


def iter_files(root):
    if os.path.isfile(root):
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        )
        for fn in sorted(filenames):
            if fn.startswith("."):
                continue
            yield os.path.join(dirpath, fn)


def lang_of(path):
    return LANG_EXT.get(os.path.splitext(path)[1].lower())


def is_test(rel):
    return bool(TEST_RE.search(rel.replace(os.sep, "/")))


def read_text(path):
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_FILE_BYTES)
    except OSError:
        return None
    if b"\x00" in data:
        return None
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def mk(dimension, level, rule, rel, line, note, *, end_line=None,
       snippet=None, measured=None, confidence="medium"):
    return {
        "dimension": dimension,
        "level": level,
        "rule": rule,
        "file": rel,
        "line": int(line),
        "end_line": int(end_line if end_line is not None else line),
        "snippet": (snippet or "")[:MAX_SNIPPET],
        "measured": measured or {},
        "note": note,
        "confidence": confidence,
    }


# ------------------------------------------------------------------ Python


def _is_elif(parent, child):
    return (isinstance(child, ast.If) and isinstance(parent, ast.If)
            and len(parent.orelse) == 1 and parent.orelse[0] is child)


def _max_nesting(node):
    best = 0
    for child in ast.iter_child_nodes(node):
        d = _max_nesting(child)
        if isinstance(child, NEST_TYPES) and not _is_elif(node, child):
            d += 1
        if d > best:
            best = d
    return best


def _count_branches(node):
    n = 0
    for child in ast.walk(node):
        if isinstance(child, BRANCH_TYPES):
            n += 1
        elif isinstance(child, ast.BoolOp):
            n += max(len(child.values) - 1, 0)
    return n


def analyze_python(rel, text, out):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return
    lines = text.splitlines()

    def snip(a, b):
        a, b = max(a, 1), min(b, len(lines))
        return "\n".join(lines[a - 1:b])

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            flen = end - node.lineno + 1
            nest = _max_nesting(node)
            br = _count_branches(node)
            a = node.args
            nargs = len(a.posonlyargs) + len(a.args) + len(a.kwonlyargs)
            nargs += 1 if a.vararg else 0
            nargs += 1 if a.kwarg else 0

            trig = []
            if flen > FUNC_LINES:
                trig.append("长 %d 行" % flen)
            if nest >= NESTING:
                trig.append("嵌套 %d 层" % nest)
            if br > BRANCHES:
                trig.append("分支 %d" % br)
            if nargs > ARGS:
                trig.append("参数 %d" % nargs)
            if trig:
                hard = (flen > FUNC_LINES_HARD or nest >= NESTING_HARD
                        or br > BRANCHES_HARD)
                out.append(mk(
                    "function-size", "L3" if hard else "L2", "large-function",
                    rel, node.lineno, "%s:%s" % (node.name, "、".join(trig)),
                    end_line=end, snippet=snip(node.lineno, node.lineno + 1),
                    measured={"lines": flen, "nesting": nest,
                              "branches": br, "args": nargs},
                ))

            defaults = list(a.defaults) + [x for x in a.kw_defaults if x is not None]
            for d in defaults:
                if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                    out.append(mk(
                        "error-handling", "L4", "mutable-default-arg", rel, node.lineno,
                        "%s 用可变对象作默认参数,调用之间共享同一份" % node.name,
                        confidence="high",
                    ))

        elif isinstance(node, ast.ExceptHandler):
            end = getattr(node, "end_lineno", node.lineno)
            body = node.body
            only_pass = len(body) == 1 and isinstance(body[0], ast.Pass)
            bare = node.type is None
            broad = (isinstance(node.type, ast.Name)
                     and node.type.id in ("Exception", "BaseException"))
            if only_pass and (bare or broad):
                out.append(mk(
                    "error-handling", "L3", "swallowed-exception", rel, node.lineno,
                    "裸 except 且静默吞掉" if bare else "捕获过宽且静默吞掉",
                    end_line=end, snippet=snip(node.lineno, end), confidence="high",
                ))
            elif bare:
                out.append(mk(
                    "error-handling", "L2", "bare-except", rel, node.lineno,
                    "裸 except,会连 KeyboardInterrupt / SystemExit 一起吞",
                    end_line=end, snippet=snip(node.lineno, end), confidence="high",
                ))

    any_uses = len(re.findall(r":\s*Any\b|->\s*Any\b|\[\s*Any\s*\]", text))
    if any_uses >= ANY_OVERUSE:
        out.append(mk(
            "typing", "L2", "any-overuse", rel, 1,
            "Any 出现 %d 次" % any_uses,
            measured={"any_uses": any_uses}, confidence="low",
        ))


def scan_regex(rel, lang, text, out):
    for dim, level, rule, pat, note in REGEX_RULES:
        for m in pat.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            snippet = text[m.start():m.end()].replace("\n", " ")
            out.append(mk(dim, level, rule, rel, line, note,
                          snippet=snippet, confidence="low"))


# ------------------------------------------------------------------ 重复


def normalize_line(raw):
    return re.sub(r"\s+", " ", raw.strip())


def meaningful_lines(text):
    out = []
    for i, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        if not s or s.startswith(COMMENT_PREFIX):
            continue
        out.append((i, normalize_line(raw)))
    return out


def find_duplicates(files_lines, window, min_lines):
    index = defaultdict(list)
    per_file = {}
    for rel, vlines in files_lines.items():
        wins = []
        for i in range(len(vlines) - window + 1):
            key = "\n".join(vlines[i + k][1] for k in range(window))
            h = hashlib.md5(key.encode("utf-8", "replace")).hexdigest()
            wins.append((i, vlines[i][0], h))
            index[h].append((rel, vlines[i][0]))
        per_file[rel] = wins

    dup_hashes = {h for h, pos in index.items() if len(set(pos)) >= 2}
    if not dup_hashes:
        return []

    runs = []
    for rel, wins in per_file.items():
        cur = []
        for item in wins:
            i, orig, h = item
            if h in dup_hashes:
                if cur and i == cur[-1][0] + 1:
                    cur.append(item)
                else:
                    if cur:
                        runs.append((rel, cur))
                    cur = [item]
            elif cur:
                runs.append((rel, cur))
                cur = []
        if cur:
            runs.append((rel, cur))

    blocks = []
    for rel, run in runs:
        block_lines = len(run) + window - 1
        if block_lines < min_lines:
            continue
        start = run[0][1]
        end = run[-1][1] + window - 1
        others = sorted({p for (_, o, h) in run for p in index[h] if p != (rel, o)})
        blocks.append({
            "file": rel,
            "line": start,
            "end_line": end,
            "lines": block_lines,
            "fingerprint": run[0][2][:8],
            "occurrences": [{"file": rel, "line": start}]
            + [{"file": f, "line": l} for (f, l) in others[:10]],
        })

    blocks.sort(key=lambda x: (x["file"], x["line"]))
    merged = []
    for b in blocks:
        if merged and b["file"] == merged[-1]["file"] and b["line"] <= merged[-1]["end_line"]:
            last = merged[-1]
            last["end_line"] = max(last["end_line"], b["end_line"])
            last["lines"] = last["end_line"] - last["line"] + 1
            for o in b["occurrences"]:
                if o not in last["occurrences"]:
                    last["occurrences"].append(o)
        else:
            merged.append(b)

    # 同一段内容(同一指纹)的块并成一条,列出全部出现位置
    grouped = {}
    order = []
    for b in merged:
        fp = b["fingerprint"]
        if fp not in grouped:
            grouped[fp] = {"fingerprint": fp, "lines": b["lines"],
                           "occurrences": [], "test": True}
            order.append(fp)
        g = grouped[fp]
        g["lines"] = max(g["lines"], b["lines"])
        loc = {"file": b["file"], "line": b["line"]}
        if loc not in g["occurrences"]:
            g["occurrences"].append(loc)
        g["test"] = g["test"] and is_test(b["file"])
    return [grouped[fp] for fp in order]


# ------------------------------------------------------------------ 主流程


def main(argv=None):
    ap = argparse.ArgumentParser(description="代码质量候选扫描器(只圈候选,不下结论)")
    ap.add_argument("target", help="目标路径(容器里用 /workspace/...)")
    ap.add_argument("--json", dest="json_out", help="把结果写成 JSON")
    ap.add_argument("--min-level", default="L1", choices=["L1", "L2", "L3", "L4"],
                    help="低于此等级的候选不输出")
    ap.add_argument("--window", type=int, default=DUP_WINDOW, help="重复检测窗口行数")
    ap.add_argument("--quiet", action="store_true", help="只打印汇总")
    args = ap.parse_args(argv)

    root = args.target
    if not os.path.exists(root):
        print("路径不存在:%s" % root, file=sys.stderr)
        return 2

    base = root if os.path.isdir(root) else (os.path.dirname(root) or ".")
    findings = []
    files_lines = {}
    scanned = 0
    langs = Counter()
    has_src = has_test = False

    for path in iter_files(root):
        lang = lang_of(path)
        if not lang:
            continue
        text = read_text(path)
        if text is None:
            continue
        rel = os.path.relpath(path, base)
        scanned += 1
        langs[lang] += 1
        if is_test(rel):
            has_test = True
        else:
            has_src = True
        if lang == "py":
            analyze_python(rel, text, findings)
        scan_regex(rel, lang, text, findings)
        files_lines[rel] = meaningful_lines(text)

    dup_blocks = find_duplicates(files_lines, max(3, args.window), DUP_MIN_LINES)

    if has_src and not has_test:
        findings.append(mk("testing", "L2", "no-tests", "(repo)", 1,
                           "未发现任何测试文件"))

    limit = LEVEL_ORDER[args.min_level]
    findings = [f for f in findings if LEVEL_ORDER.get(f["level"], 9) <= limit]
    findings.sort(key=lambda f: (LEVEL_ORDER.get(f["level"], 9),
                                 f["dimension"], f["file"], f["line"]))

    by_level = Counter(f["level"] for f in findings)
    by_dim = Counter(f["dimension"] for f in findings)

    print("代码质量扫描 —— %s" % root)
    print("扫描文件:%d  %s" % (scanned, dict(langs)))
    print("候选命中:%d 条" % len(findings))
    print("  按等级:" + "  ".join(
        "%s=%d" % (k, by_level[k]) for k in ("L4", "L3", "L2", "L1") if by_level[k]))
    print("  按维度:" + "  ".join(
        "%s=%d" % (k, v) for k, v in sorted(by_dim.items())))
    print("重复块:%d 组" % len(dup_blocks))

    if not args.quiet:
        print()
        for f in findings:
            print("[%s] %s  %s:%d  %s" % (f["level"], f["dimension"],
                                          f["file"], f["line"], f["note"]))
        for b in dup_blocks:
            locs = "  ".join("%s:%d" % (o["file"], o["line"])
                             for o in b["occurrences"][:5])
            tail = " ..." if len(b["occurrences"]) > 5 else ""
            tag = "(测试)" if b.get("test") else ""
            print("[dup]%s %d 行 × %d 处  %s%s"
                  % (tag, b["lines"], len(b["occurrences"]), locs, tail))
        print()
        print("以上为候选,必须读上下文复核后再写进报告。")

    if args.json_out:
        payload = {
            "target": root,
            "summary": {
                "files_scanned": scanned,
                "by_level": dict(by_level),
                "by_dimension": dict(by_dim),
                "duplicate_blocks": len(dup_blocks),
            },
            "findings": findings,
            "duplicates": dup_blocks,
        }
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        if not args.quiet:
            print("JSON 已写出:%s" % args.json_out)

    return 0


if __name__ == "__main__":
    sys.exit(main())
