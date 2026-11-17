#!/usr/bin/env python3
"""多语言启发式性能热点扫描。

给的是候选清单,不是结论。输出:
  - 超长/高分支函数(复杂度热点)
  - 循环体内做 IO / 查询 / 反射 / 序列化(疑似 N+1)
  - 进程级可变状态(常驻模式下的隔离风险)
  - 缓存与预热的调用点分布(判断"有没有做")

用法:
    python scan_hotspots.py <repo> [--top 20] [--json out.json] [--lang php,go]

设计原则:宁可漏报,不要用正则强行断言"这里慢"。所有命中都要人读代码确认。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

LANGS = {
    ".php": "php", ".py": "python", ".pyi": "python",
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js",
    ".ts": "ts", ".tsx": "ts",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".go": "go", ".rs": "rust", ".cs": "csharp", ".rb": "ruby",
    ".swift": "swift", ".scala": "scala", ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp",
}

SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "vendor", "dist", "build",
    "target", ".venv", "venv", "__pycache__", ".pylibs", ".trash",
    "coverage", ".idea", ".vscode", "tmp", "cache",
}

BRACE_LANGS = {"php", "js", "ts", "java", "kotlin", "go", "rust", "csharp",
               "swift", "scala", "c", "cpp"}

FUNC_RE = {
    "php": re.compile(r"^\s*(?:(?:public|protected|private|static|final|abstract)\s+)*function\s+&?\s*([A-Za-z_]\w*)\s*\("),
    "js": re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(|^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\s*)?\(?[^=;]*?=>"),
    "ts": re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(|^\s*(?:export\s+)?(?:public|private|protected|static|async|readonly|\s)*([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*(?::\s*[^{]+)?\{"),
    "java": re.compile(r"^\s*(?:(?:public|private|protected|static|final|synchronized|abstract|default|native)\s+)+[\w<>\[\],\s?.]+?\s+([A-Za-z_]\w*)\s*\("),
    "kotlin": re.compile(r"^\s*(?:(?:public|private|protected|internal|suspend|inline|operator|override)\s+)*fun\s+(?:<[^>]+>\s*)?([A-Za-z_]\w*)\s*\("),
    "go": re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\("),
    "rust": re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+([A-Za-z_]\w*)"),
    "csharp": re.compile(r"^\s*(?:(?:public|private|protected|internal|static|async|virtual|override|sealed|partial)\s+)+[\w<>\[\],\s?.]+?\s+([A-Za-z_]\w*)\s*\("),
    "swift": re.compile(r"^\s*(?:public|private|internal|static|class|override|mutating)?\s*func\s+([A-Za-z_]\w*)\s*\("),
    "scala": re.compile(r"^\s*def\s+([A-Za-z_]\w*)\s*[\[(]"),
    "c": re.compile(r"^[A-Za-z_][\w \t*]*\s+\**([A-Za-z_]\w*)\s*\([^;]*$"),
    "cpp": re.compile(r"^[A-Za-z_][\w \t*:<>,]*\s+\**([A-Za-z_]\w*)\s*\([^;]*$"),
    "python": re.compile(r"^(\s*)(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\("),
    "ruby": re.compile(r"^(\s*)def\s+(?:self\.)?([A-Za-z_]\w*[?!=]?)"),
}

BRANCH_RE = re.compile(r"\b(?:if|elseif|elif|elsif|for|foreach|while|case|catch|when|switch|guard)\b|&&|\|\||\?[^.:]")
LOOP_RE = re.compile(r"\b(?:for|foreach|while|do)\b|\.forEach\s*\(|\.map\s*\(|\.filter\s*\(|\.each\b|\.times\b")

HEAVY = {
    "db-query": re.compile(r"\b(?:query|execute|prepare|fetchAll|fetchOne|fetch|find|findOne|findBy|select|insert|update|delete|where|createQuery|EntityManager)\s*\(", re.I),
    "http": re.compile(r"\b(?:curl_exec|file_get_contents|stream_context_create|requests?\.(?:get|post|put)|httpx\.|fetch\s*\(|axios\.|http\.Get|http\.Post|HttpClient|RestTemplate|WebClient|reqwest)\b", re.I),
    "file-io": re.compile(r"\b(?:file_get_contents|file_put_contents|fopen|fread|fwrite|fs\.(?:readFile|writeFile|readFileSync|writeFileSync)|open\s*\(|read_to_string|ioutil\.ReadFile|os\.ReadFile|File\.read)\b", re.I),
    "reflection": re.compile(r"\b(?:new\s+Reflection(?:Class|Method|Property|Parameter)|getDeclared|getMethod|getProperty|Class\.forName|Type\.GetType|getattr\s*\(|hasattr\s*\(|Invoke\s*\(|MethodInfo)\b", re.I),
    "serialize": re.compile(r"\b(?:json_encode|json_decode|serialize\s*\(|unserialize\s*\(|JSON\.parse|JSON\.stringify|pickle\.|yaml\.(?:load|dump)|marshal\.|msgpack)\b", re.I),
}

CACHE_RE = re.compile(r"\b(?:cache|Cache|cached|memoize|memo|lru_cache|opcache|APCu|apcu_|Redis|Memcached|sync\.Pool|ConcurrentHashMap|computeIfAbsent)\b")
STATIC_RE = [
    # 只认"可变容器/惰性单例"形态的静态状态;`static string $table='x'` 这类类常量不算
    re.compile(r"\bstatic\s+[^;=]*\$[A-Za-z_]\w*\s*=\s*(?:array\(|\[|new\s)"),
    re.compile(r"\bstatic\s+[^;=]*\$[A-Za-z_]\w*\s*=\s*null\b"),
    re.compile(r"^\s*(?:private|protected|public|internal)?\s*static\s+(?:final\s+)?(?:Map|Set|WeakMap|ConcurrentHashMap|sync\.Map|atomic\.Value)"),
    re.compile(r"^(?:var|let|const)\s+[A-Za-z_$][\w$]*\s*=\s*(?:new\s+)?(?:Map|Set|WeakMap|\[\]|\{\})"),
    re.compile(r"^[A-Za-z_]\w*\s*=\s*(?:\[\s*\]|\{\s*\}|dict\(\)|list\(\)|set\(\)|defaultdict|make\()"),
]

def strip_noise(line: str) -> str:
    """去掉行内字符串与注释,避免括号/关键字误计。粗粒度,够用。"""
    line = re.sub(r"//.*$", "", line)
    line = re.sub(r"#(?!\[).*$", "", line)
    line = re.sub(r"/\*.*?\*/", "", line)
    line = re.sub(r'"(?:\\.|[^"\\])*"', '""', line)
    line = re.sub(r"'(?:\\.|[^'\\])*'", "''", line)
    line = re.sub(r"`(?:\\.|[^`\\])*`", "``", line)
    return line


def iter_files(root: Path, langs: set[str] | None):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            p = Path(dirpath) / name
            lang = LANGS.get(p.suffix.lower())
            if not lang:
                continue
            if langs and lang not in langs:
                continue
            try:
                if p.stat().st_size > 1_500_000:
                    continue
            except OSError:
                continue
            yield p, lang


def read_lines(p: Path) -> list[str] | None:
    try:
        return p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None


def brace_block(lines: list[str], start: int) -> int:
    """从 start 行起按大括号配对找函数结束行(含)。找不到返回 start。"""
    depth = 0
    seen = False
    for i in range(start, min(len(lines), start + 600)):
        s = strip_noise(lines[i])
        for ch in s:
            if ch == "{":
                depth += 1
                seen = True
            elif ch == "}":
                depth -= 1
        if seen and depth <= 0:
            return i
    return start


def indent_block(lines: list[str], start: int, base_indent: int, lang: str) -> int:
    """Python/Ruby 的缩进块。Ruby 退化到下一个同缩进 end。"""
    if lang == "ruby":
        for i in range(start + 1, min(len(lines), start + 600)):
            if re.match(r"^\s{0,%d}end\b" % base_indent, lines[i]):
                return i
        return start
    for i in range(start + 1, len(lines)):
        raw = lines[i]
        if not raw.strip():
            continue
        ind = len(raw) - len(raw.lstrip())
        if ind <= base_indent and not raw.lstrip().startswith(("#", "@", ")")):
            return i - 1
    return len(lines) - 1


def analyze(path: Path, lang: str) -> dict | None:
    lines = read_lines(path)
    if lines is None or len(lines) < 5:
        return None
    funcs = []
    bodies = {}
    func_count = 0
    total_branches = 0
    loop_hits = []
    statics = []
    cache_sites = 0

    for idx, line in enumerate(lines):
        if CACHE_RE.search(line):
            cache_sites += 1
        if any(r.search(line) for r in STATIC_RE):
            statics.append({"line": idx + 1, "code": line.strip()[:120]})

        m = FUNC_RE[lang].search(line)
        if m:
            name = next((g for g in m.groups() if g), "")
            if not name:
                continue
            func_count += 1
            start_indent = len(line) - len(line.lstrip())
            end = indent_block(lines, idx, start_indent, lang) if lang in ("python", "ruby") else brace_block(lines, idx)
            body = lines[idx:end + 1]
            bodies[name] = "\n".join(body)
            size = end - idx + 1
            branches = sum(len(BRANCH_RE.findall(strip_noise(b))) for b in body)
            total_branches += branches
            if size >= 40 or branches >= 15:
                funcs.append({"name": name, "line": idx + 1, "end": end + 1,
                              "size": size, "branches": branches})

    # 独立的循环块扫描(不限于已识别函数,覆盖脚本式代码)
    for idx, line in enumerate(lines):
        code = strip_noise(line)
        if not LOOP_RE.search(code):
            continue
        end = brace_block(lines, idx) if lang in BRACE_LANGS else indent_block(lines, idx, len(line) - len(line.lstrip()), lang)
        if end <= idx:
            continue
        body = "\n".join(lines[idx:end + 1])
        kinds = [k for k, rx in HEAVY.items() if rx.search(body)]
        if not kinds:
            # 循环里调用本文件定义的方法,而该方法访问 DB/IO:间接 N+1,提示人工追
            via = [fn for fn, fb in bodies.items()
                   if re.search(r"\b%s\s*\(" % re.escape(fn), body)
                   and any(rx.search(fb) for rx in HEAVY.values())]
            if via:
                kinds = ["indirect:" + ",".join(via[:3])]
        if kinds:
            loop_hits.append({"line": idx + 1, "end": end + 1, "kinds": kinds,
                              "size": end - idx + 1, "code": line.strip()[:120]})

    return {
        "file": str(path), "lang": lang, "lines": len(lines),
        "funcs": funcs, "func_count": func_count, "branches": total_branches,
        "loop_heavy": loop_hits, "statics": statics, "cache_sites": cache_sites,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="多语言启发式性能热点扫描")
    ap.add_argument("repo")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--json")
    ap.add_argument("--lang", help="只看这些语言,逗号分隔")
    args = ap.parse_args()

    root = Path(args.repo)
    if not root.exists():
        print(f"路径不存在: {root}", file=sys.stderr)
        return 2
    langs = {x.strip() for x in args.lang.split(",")} if args.lang else None

    results = []
    lang_counter = Counter()
    for p, lang in iter_files(root, langs):
        lang_counter[lang] += 1
        r = analyze(p, lang)
        if r:
            results.append(r)

    if not results:
        print("没有找到可分析的源码文件(检查路径、扩展名或 --lang)")
        return 1

    all_funcs = [(r["file"], f, r["lang"]) for r in results for f in r["funcs"]]
    all_funcs.sort(key=lambda x: (x[1]["size"], x[1]["branches"]), reverse=True)
    all_loops = [(r["file"], h, r["lang"]) for r in results for h in r["loop_heavy"]]
    all_statics = [(r["file"], s, r["lang"]) for r in results for s in r["statics"]]

    print("=" * 72)
    print(f"扫描根目录: {root}")
    print(f"源码文件: {len(results)}  语言分布: {dict(lang_counter)}")
    print(f"函数总数: {sum(r['func_count'] for r in results)}  缓存相关调用点: {sum(r['cache_sites'] for r in results)}")
    print("=" * 72)

    print(f"\n[1] 复杂度热点 Top {args.top} (行数>=40 或 分支>=15)")
    print(f"{'行数':>5} {'分支':>5}  位置")
    for path, f, lang in all_funcs[:args.top]:
        rel = os.path.relpath(path, root)
        print(f"{f['size']:>5} {f['branches']:>5}  {rel}:{f['line']}-{f['end']}  {f['name']}")
    if not all_funcs:
        print("  (无命中)")

    print(f"\n[2] 循环体内的重操作 Top {args.top} (疑似 N+1,需人工确认)")
    for path, h, lang in sorted(all_loops, key=lambda x: x[1]["size"], reverse=True)[:args.top]:
        rel = os.path.relpath(path, root)
        print(f"  {rel}:{h['line']}-{h['end']}  [{','.join(h['kinds'])}]  {h['code']}")
    if not all_loops:
        print("  (无命中)")

    print(f"\n[3] 进程级可变状态 ({len(all_statics)} 处)")
    for path, s, lang in all_statics[:args.top]:
        rel = os.path.relpath(path, root)
        print(f"  {rel}:{s['line']}  {s['code']}")
    if len(all_statics) > args.top:
        print(f"  ... 其余 {len(all_statics) - args.top} 处见 JSON")
    if not all_statics:
        print("  (无命中)")

    print("\n[4] 提示")
    print("  - 命中 ≠ 问题:循环里的单次查询可能只跑 1 次,需读代码确认数据规模。")
    print("  - 复杂度热点集中在同一个文件时,优先看这个文件的分层是否合理。")
    print("  - 静态状态数量多,说明常驻模式下的隔离要靠手工维护,去查清理路径。")

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "root": str(root),
            "files": len(results),
            "languages": dict(lang_counter),
            "results": results,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n完整结果已写入: {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
