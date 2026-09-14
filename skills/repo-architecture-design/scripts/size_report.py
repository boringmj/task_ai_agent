#!/usr/bin/env python3
"""规模统计:为重构守门提供数字依据。

统计目标目录的文件数、行数、语言构成、最大文件、空文件、目录深度,
并对 Python 文件做类与函数的精确计数(ast)。输出 JSON 与一段摘要。

用法:
    python3 size_report.py <目标路径> [--out report.json] [--top 10]

纯标准库实现,不依赖任何第三方包。
"""

import argparse
import ast
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone

SKIP_DIRS = {
    ".git", ".hg", ".svn", ".trash", "__pycache__", "node_modules",
    "venv", ".venv", "env", ".env", "dist", "build", "target",
    ".idea", ".vscode", ".mypy_cache", ".pytest_cache", ".pylibs",
    "site-packages", ".tox", "vendor",
}

TEXT_EXT_LANG = {
    ".py": "Python", ".pyi": "Python", ".js": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".java": "Java", ".go": "Go",
    ".php": "PHP", ".rb": "Ruby", ".rs": "Rust", ".c": "C", ".h": "C",
    ".cpp": "C++", ".hpp": "C++", ".cs": "C#", ".kt": "Kotlin",
    ".swift": "Swift", ".scala": "Scala", ".sh": "Shell", ".bat": "Batch",
    ".ps1": "PowerShell", ".sql": "SQL", ".html": "HTML", ".css": "CSS",
    ".vue": "Vue", ".md": "Markdown", ".json": "JSON", ".yaml": "YAML",
    ".yml": "YAML", ".toml": "TOML", ".ini": "INI", ".xml": "XML",
}

CODE_LANGS = {
    "Python", "JavaScript", "TypeScript", "Java", "Go", "PHP", "Ruby",
    "Rust", "C", "C++", "C#", "Kotlin", "Swift", "Scala", "Shell",
    "Batch", "PowerShell", "SQL",
}

MAX_FILE_BYTES = 2 * 1024 * 1024


def is_text(path):
    try:
        with open(path, "rb") as fh:
            return b"\x00" not in fh.read(4096)
    except OSError:
        return False


def count_lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def scan_python(path):
    """返回 (类数, 函数数, 最大类名与行数) —— 解析失败时返回 None。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            tree = ast.parse(fh.read(), filename=path)
    except (SyntaxError, ValueError, OSError):
        return None

    classes, functions, biggest = 0, 0, ("", 0)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            classes += 1
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            size = end - node.lineno + 1
            if size > biggest[1]:
                biggest = (node.name, size)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions += 1
    return classes, functions, biggest


def collect(root):
    files, dirs = [], set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        rel_dir = os.path.relpath(dirpath, root)
        dirs.add("." if rel_dir == "." else rel_dir.replace(os.sep, "/"))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            files.append((full, size))
    return files, dirs


def main():
    ap = argparse.ArgumentParser(description="统计目录规模,供重构守门使用")
    ap.add_argument("target")
    ap.add_argument("--out", help="把 JSON 结果写入该路径")
    ap.add_argument("--top", type=int, default=10, help="最大文件取前 N 个(默认 10)")
    args = ap.parse_args()

    root = os.path.abspath(args.target)
    if not os.path.isdir(root):
        print(f"不是目录: {root}", file=sys.stderr)
        return 2

    files, dirs = collect(root)
    skipped_binary = skipped_large = 0
    by_ext = Counter()
    by_lang = Counter()
    lang_lines = Counter()
    entries = []
    empty_files = []
    py_classes = py_functions = 0
    py_files = 0
    py_broken = []
    biggest_class = ("", 0, "")

    for full, size in files:
        rel = os.path.relpath(full, root).replace(os.sep, "/")
        ext = os.path.splitext(full)[1].lower()
        if size > MAX_FILE_BYTES:
            skipped_large += 1
            continue
        if not is_text(full):
            skipped_binary += 1
            continue

        lines = count_lines(full)
        by_ext[ext or "(无后缀)"] += 1
        lang = TEXT_EXT_LANG.get(ext, "Other")
        by_lang[lang] += 1
        lang_lines[lang] += lines
        entries.append({"path": rel, "lines": lines, "bytes": size, "lang": lang})

        if lines == 0:
            empty_files.append(rel)

        if ext == ".py":
            py_files += 1
            parsed = scan_python(full)
            if parsed is None:
                py_broken.append(rel)
            else:
                cls, fun, big = parsed
                py_classes += cls
                py_functions += fun
                if big[1] > biggest_class[1]:
                    biggest_class = (big[0], big[1], rel)

    entries.sort(key=lambda e: e["lines"], reverse=True)
    code_lines = sum(e["lines"] for e in entries if e["lang"] in CODE_LANGS)

    result = {
        "target": root,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "totals": {
            "files": len(entries),
            "lines": sum(e["lines"] for e in entries),
            "code_lines": code_lines,
            "dirs": len(dirs),
        },
        "skipped": {"binary": skipped_binary, "too_large": skipped_large},
        "by_extension": dict(by_ext.most_common()),
        "by_language": dict(by_lang.most_common()),
        "lang_lines": {k: v for k, v in lang_lines.most_common() if k in CODE_LANGS},
        "largest_files": entries[: args.top],
        "empty_files": empty_files,
        "all_dirs": sorted(dirs),
        "max_dir_depth": max(
            (0 if d == "." else d.count("/") + 1) for d in dirs
        ) if dirs else 0,
        "python": {
            "files": py_files,
            "classes": py_classes,
            "functions": py_functions,
            "unparsable": py_broken,
            "biggest_class": {
                "name": biggest_class[0],
                "lines": biggest_class[1],
                "file": biggest_class[2],
            },
        },
    }

    print("=" * 68)
    print(f"目标: {root}")
    t = result["totals"]
    print(f"文件 {t['files']}  行 {t['lines']}(其中代码 {t['code_lines']})  目录 {t['dirs']}  最大深度 {result['max_dir_depth']}")
    if result["skipped"]:
        print(f"跳过: 二进制 {result['skipped']['binary']} 个,超大 {result['skipped']['too_large']} 个")
    print("-" * 68)
    print("语言构成(按行数):")
    for lang, lines in list(lang_lines.most_common())[:8]:
        if lang in CODE_LANGS:
            print(f"  {lang:<12} {lines:>8} 行")
    print("-" * 68)
    print(f"最大文件 top {min(args.top, len(entries))}:")
    for e in entries[: args.top]:
        print(f"  {e['lines']:>6} 行  {e['path']}")
    if empty_files:
        print("-" * 68)
        print(f"空文件 {len(empty_files)} 个:", ", ".join(empty_files[:8]) + (" …" if len(empty_files) > 8 else ""))
    print("-" * 68)
    py = result["python"]
    print(f"Python: {py['files']} 文件, {py['classes']} 个类, {py['functions']} 个函数")
    if py["biggest_class"]["name"]:
        b = py["biggest_class"]
        print(f"  最大类: {b['name']} ({b['lines']} 行, {b['file']})")
    if py["unparsable"]:
        print(f"  无法解析: {len(py['unparsable'])} 个(语法错误?): {', '.join(py['unparsable'][:5])}")
    print("-" * 68)
    print("提示: 行数只解决「规模档位」;背景档位与守门规则见 references/over-engineering.md")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print(f"JSON 已写入: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
