#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描一个代码仓库,输出架构分析所需的全局图景(JSON)。

只做静态统计与 import 名称抽取,不执行被扫描仓库里的任何代码。
依赖关系是近似结果:动态导入、反射、字符串拼出来的模块名抓不到。

用法:
    python scan_repo.py <仓库路径> [--depth 2] [--top 15]

路径注意:在容器里跑时,工作区要写成 /workspace/... (如 /workspace/clones/foo),
不要用宿主路径。
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict

IGNORE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "bower_components", "vendor",
    "__pycache__", ".venv", "venv", "env", ".env", "dist", "build",
    "target", "out", "bin", "obj", ".idea", ".vscode", ".gradle",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".next", ".nuxt",
    "coverage", "htmlcov", ".tox", "Pods", ".terraform", ".cache",
}

LANG_EXT = {
    ".py": "Python", ".pyi": "Python",
    ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript",
    ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".scala": "Scala",
    ".c": "C", ".h": "C", ".cc": "C++", ".cpp": "C++", ".hpp": "C++", ".cxx": "C++",
    ".cs": "C#", ".rb": "Ruby", ".php": "PHP", ".swift": "Swift", ".m": "Objective-C",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell",
    ".html": "HTML", ".css": "CSS", ".scss": "SCSS", ".less": "Less",
    ".sql": "SQL", ".vue": "Vue", ".svelte": "Svelte", ".lua": "Lua", ".pl": "Perl",
    ".md": "Markdown", ".rst": "reStructuredText",
    ".yml": "YAML", ".yaml": "YAML", ".json": "JSON", ".toml": "TOML",
    ".xml": "XML", ".ini": "INI", ".cfg": "INI",
}

SRC_EXTS = {
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".go",
    ".java", ".kt", ".scala", ".rs", ".rb", ".php", ".cs", ".swift",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cxx", ".m",
}

BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tiff",
    ".pdf", ".zip", ".gz", ".tgz", ".tar", ".rar", ".7z", ".bz2", ".xz",
    ".exe", ".dll", ".so", ".dylib", ".o", ".a", ".class", ".jar", ".pyc",
    ".wasm", ".ttf", ".otf", ".woff", ".woff2", ".eot", ".mp3", ".mp4",
    ".avi", ".mov", ".mkv", ".wav", ".flac", ".db", ".sqlite", ".lock",
}

MANIFEST_NAMES = {
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "requirements-dev.txt", "Pipfile", "Pipfile.lock", "poetry.lock",
    "go.mod", "go.sum", "Cargo.toml", "Cargo.lock",
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
    "Gemfile", "Gemfile.lock", "composer.json", "mix.exs",
    "CMakeLists.txt", "Makefile", "Dockerfile", "docker-compose.yml",
    "docker-compose.yaml", "build.sbt", "MANIFEST.in", "tox.ini",
}

ENTRY_BASENAMES = {
    "main.py", "__main__.py", "app.py", "manage.py", "cli.py", "wsgi.py",
    "asgi.py", "run.py", "server.py",
    "main.go", "main.rs", "lib.rs",
    "index.js", "index.ts", "index.jsx", "index.tsx", "index.mjs",
    "server.js", "server.ts", "app.js", "app.ts", "main.js", "main.ts",
    "Application.java", "Program.cs", "main.c", "main.cpp",
}

ENTRY_DIRS = {"cmd", "bin", "entrypoints", "entrypoint"}

MAX_LINES_BYTES = 2 * 1024 * 1024   # 超过这个大小不算行数
MAX_READ_BYTES = 512 * 1024         # 超过这个大小不抽 import

PY_FROM = re.compile(r"^[ \t]*from[ \t]+([.\w]+)[ \t]+import\b", re.M)
PY_IMPORT = re.compile(r"^[ \t]*import[ \t]+([\w. \t,]+?)[ \t]*$", re.M)
JS_IMPORT_FROM = re.compile(r"""^\s*import\s+(?:[^\n'"]*?\sfrom\s+)?['"]([^'"]+)['"]""", re.M)
JS_REQUIRE = re.compile(r"""require\s*\(\s*['"]([^'"]+)['"]""")
JS_DYN = re.compile(r"""import\s*\(\s*['"]([^'"]+)['"]""")
GO_BLOCK = re.compile(r"import\s*\(([^)]*)\)", re.S)
GO_SINGLE = re.compile(r'^\s*import\s+(?:\w+\s+)?"([^"]+)"', re.M)
JAVA_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)", re.M)

JS_RESOLVE_SUFFIX = ["", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs",
                     ".vue", ".svelte",
                     "/index.js", "/index.ts", "/index.jsx", "/index.tsx"]


def iter_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        dirnames.sort()
        for fn in sorted(filenames):
            yield os.path.join(dirpath, fn)


def count_lines(path):
    try:
        if os.path.getsize(path) > MAX_LINES_BYTES:
            return None
        with open(path, "rb") as f:
            data = f.read()
        return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    except OSError:
        return None


def read_text(path):
    try:
        if os.path.getsize(path) > MAX_READ_BYTES:
            return None
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return None


def py_imports(text):
    names = list(PY_FROM.findall(text))
    for m in PY_IMPORT.finditer(text):
        for part in m.group(1).split(","):
            part = part.split(" as ")[0].strip()
            if part:
                names.append(part)
    return names


def js_imports(text):
    return JS_IMPORT_FROM.findall(text) + JS_REQUIRE.findall(text) + JS_DYN.findall(text)


def go_imports(text):
    names = []
    for block in GO_BLOCK.findall(text):
        names += re.findall(r'"([^"]+)"', block)
    names += GO_SINGLE.findall(text)
    return names


def build_py_modules(root):
    mods = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        for fn in filenames:
            if not (fn.endswith(".py") or fn.endswith(".pyi")):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), root).replace(os.sep, "/")
            parts = rel.rsplit(".", 1)[0].split("/")
            if parts and parts[-1] == "__init__":
                parts = parts[:-1]
            if parts:
                mods[".".join(parts)] = rel
    return mods


def in_repo_py(full, mods):
    parts = full.split(".")
    for i in range(len(parts), 0, -1):
        cand = ".".join(parts[:i])
        if cand in mods:
            return cand
    return None


def resolve_py_rel(name, cur_pkg_parts):
    """把相对导入 .x / ..x 解析成绝对模块名(近似)。"""
    level = len(name) - len(name.lstrip("."))
    rest = name.lstrip(".")
    keep = len(cur_pkg_parts) - (level - 1)
    base = cur_pkg_parts[:keep] if keep > 0 else []
    parts = [p for p in base if p] + ([rest] if rest else [])
    return ".".join(parts)


def resolve_js(spec, cur_dir_rel, rel_set):
    base = os.path.normpath(os.path.join(cur_dir_rel, spec)).replace(os.sep, "/")
    base = base.lstrip("./")
    for suffix in JS_RESOLVE_SUFFIX:
        cand = base + suffix
        if cand in rel_set:
            return cand
    return None


def top_node(rel):
    return rel.split("/")[0] if "/" in rel else "(root)"


def external_top(name):
    if name.startswith("@"):
        parts = name.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else name
    return name.split("/")[0].split(".")[0]


def build_tree(root, max_depth):
    lines = []
    root = os.path.abspath(root)

    def walk(d, depth, prefix):
        if depth > max_depth:
            return
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            return
        dirs = [e for e in entries if os.path.isdir(os.path.join(d, e)) and e not in IGNORE_DIRS]
        files = [e for e in entries if os.path.isfile(os.path.join(d, e))]
        for e in dirs:
            sub = os.path.join(d, e)
            n_files = sum(len(fs) for _, _, fs in os.walk(sub))
            lines.append("%s%s/  (%d files)" % (prefix, e, n_files))
            walk(sub, depth + 1, prefix + "  ")
        if depth >= max_depth - 1 and files:
            shown = ", ".join(files[:8])
            more = "" if len(files) <= 8 else ", …(+%d)" % (len(files) - 8)
            lines.append("%s[files] %s%s" % (prefix, shown, more))

    lines.append(os.path.basename(root.rstrip(os.sep)) + "/")
    walk(root, 1, "  ")
    return lines


def scan(root, depth, top_n):
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        sys.exit("错误:不是目录:%s" % root)

    total_lines = 0
    size_bytes = 0
    ext_counter = Counter()
    lang_files = Counter()
    lang_lines = Counter()
    top_dir_files = Counter()
    top_dir_lines = Counter()
    top_dir_lang = defaultdict(Counter)
    file_lines = {}
    manifests = []
    entries = []
    rel_set = set()
    abs_by_rel = {}
    file_imports = {}

    for path in iter_files(root):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        rel_set.add(rel)
        abs_by_rel[rel] = path
        try:
            size_bytes += os.path.getsize(path)
        except OSError:
            pass

        if rel.count("/") > 0:
            top = rel.split("/")[0]
        else:
            top = "(root)"
        top_dir_files[top] += 1

        base = os.path.basename(path)
        ext = os.path.splitext(base)[1].lower()
        ext_counter[ext or "(no ext)"] += 1
        if base in MANIFEST_NAMES or base in ENTRY_BASENAMES:
            pass

        lines = None
        if ext not in BINARY_EXT:
            lines = count_lines(path)
        if lines is not None:
            total_lines += lines
            file_lines[rel] = lines
            top_dir_lines[top] += lines
        if ext in LANG_EXT:
            lang_files[LANG_EXT[ext]] += 1
            if lines:
                lang_lines[LANG_EXT[ext]] += lines
            top_dir_lang[top][LANG_EXT[ext]] += 1

        if base in MANIFEST_NAMES:
            manifests.append(rel)
        if base in ENTRY_BASENAMES:
            entries.append(rel)
        elif rel.count("/") >= 1 and rel.split("/")[0] in ENTRY_DIRS and ext in SRC_EXTS:
            entries.append(rel)

        if ext in SRC_EXTS:
            text = read_text(path)
            if text is not None:
                if ext in (".py", ".pyi"):
                    file_imports[rel] = ("py", py_imports(text))
                elif ext in (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"):
                    file_imports[rel] = ("js", js_imports(text))
                elif ext == ".go":
                    file_imports[rel] = ("go", go_imports(text))
                elif ext in (".java", ".kt", ".scala"):
                    file_imports[rel] = ("java", JAVA_IMPORT.findall(text))

    py_mods = build_py_modules(root)
    go_mod_name = None
    go_mod_path = os.path.join(root, "go.mod")
    if os.path.isfile(go_mod_path):
        t = read_text(go_mod_path) or ""
        m = re.search(r"^\s*module\s+(\S+)", t, re.M)
        if m:
            go_mod_name = m.group(1)

    internal_edges = Counter()
    imported_targets = Counter()
    external_counter = Counter()
    unresolved = 0

    for rel, (kind, names) in file_imports.items():
        src_node = top_node(rel)
        cur_dir = os.path.dirname(rel).replace(os.sep, "/")
        cur_pkg_parts = [p for p in cur_dir.split("/") if p] if cur_dir else []
        for name in names:
            if not name:
                continue
            target_rel = None
            if kind == "py":
                if name.startswith("."):
                    full = resolve_py_rel(name, cur_pkg_parts)
                    hit = in_repo_py(full, py_mods) if full else None
                    if hit:
                        target_rel = py_mods[hit]
                        imported_targets[hit] += 1
                    else:
                        unresolved += 1
                else:
                    hit = in_repo_py(name, py_mods)
                    if hit:
                        target_rel = py_mods[hit]
                        imported_targets[hit] += 1
                    else:
                        external_counter[external_top(name)] += 1
            elif kind == "js":
                if name.startswith("."):
                    r = resolve_js(name, cur_dir, rel_set)
                    if r:
                        target_rel = r
                        imported_targets[r] += 1
                    else:
                        unresolved += 1
                else:
                    external_counter[external_top(name)] += 1
            elif kind == "go":
                if go_mod_name and (name == go_mod_name or name.startswith(go_mod_name + "/")):
                    imported_targets[name] += 1
                    # 顶层包近似:module 之后的第一段
                    tail = name[len(go_mod_name):].strip("/")
                    if tail:
                        cand = tail.split("/")[0]
                        for r in rel_set:
                            if r.startswith(cand + "/"):
                                target_rel = r
                                break
                else:
                    external_counter[external_top(name)] += 1
            else:
                # java 等:包名难以映射到文件,只统计外部依赖
                external_counter[external_top(name)] += 1

            if target_rel:
                dst_node = top_node(target_rel)
                if dst_node != src_node:
                    internal_edges[(src_node, dst_node)] += 1

    largest = sorted(file_lines.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    langs = [
        {"language": lang, "files": lang_files[lang], "lines": lang_lines[lang]}
        for lang in sorted(lang_files, key=lambda l: lang_lines[l], reverse=True)
    ]
    top_dirs = []
    for d in sorted(top_dir_files, key=lambda x: top_dir_lines[x], reverse=True):
        main_lang = top_dir_lang[d].most_common(1)
        top_dirs.append({
            "dir": d,
            "files": top_dir_files[d],
            "lines": top_dir_lines[d],
            "main_language": main_lang[0][0] if main_lang else None,
        })

    result = {
        "root": root,
        "totals": {"files": len(rel_set), "lines": total_lines, "size_bytes": size_bytes},
        "languages": langs,
        "top_level_dirs": top_dirs,
        "tree": build_tree(root, depth),
        "manifests": manifests,
        "entry_candidates": entries,
        "largest_files": [{"path": p, "lines": n} for p, n in largest],
        "top_extensions": ext_counter.most_common(top_n),
        "dependencies": {
            "internal_edges": [
                {"from": a, "to": b, "count": c}
                for (a, b), c in internal_edges.most_common(40)
            ],
            "top_imported": [
                {"module": m, "count": c} for m, c in imported_targets.most_common(top_n)
            ],
            "external_top": [
                {"name": n, "count": c} for n, c in external_counter.most_common(top_n)
            ],
            "unresolved_internal_refs": unresolved,
            "note": ("近似结果:静态抽取 import,动态导入/反射抓不到;"
                     "未解析的相对引用只计数(unresolved_internal_refs),"
                     "java 的 import 未做仓库内映射"),
        },
    }
    return result


def main():
    ap = argparse.ArgumentParser(description="扫描代码仓库,输出架构分析所需的全局图景")
    ap.add_argument("root", help="仓库根目录(容器里用 /workspace/...)")
    ap.add_argument("--depth", type=int, default=2, help="目录树展示层数,默认 2")
    ap.add_argument("--top", type=int, default=15, help="各项榜单取前 N 条,默认 15")
    args = ap.parse_args()

    result = scan(args.root, args.depth, args.top)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
