#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""repo-security-audit —— 仓库安全快扫器

在容器里对工作区的目标仓库做一次静态快扫,产出 findings.json + 人读摘要。
规则是数据驱动的,放在 scripts/rules/*.yaml,不加代码就能扩规则。

用法(路径相对技能目录;在容器里的实际位置见加载技能时给的资源清单):
    python scripts/scan.py <仓库路径> [子命令 ...] [选项]

子命令(一个都不写 = all):
    secrets   硬编码凭据 / 密钥 / 高熵串
    patterns  危险 API 与漏洞模式(按语言 + 通用 Web)
    config    部署与工程配置风险(Docker / K8s / CI / nginx / .env)
    deps      依赖清单 → OSV 漏洞库(需联网,--offline 可跳过)
    bandit    调用 bandit 做 Python 深度扫描(需已安装,缺失自动跳过)
    all       以上全部

常用选项:
    --out FILE        findings.json 输出路径(默认 ./findings.json)
    --exclude GLOB    追加排除的 glob(可重复)
    --max-size BYTES  单文件大小上限(默认 1.5 MB)
    --offline         不联网(deps 跳过 OSV 查询)
    --json-only       只写 JSON,不打印摘要
    --top N           摘要里列出前 N 条(默认 25)

规则文件需要 pyyaml,缺失时:
    pip install --target .pylibs pyyaml \
        -i https://pypi.tuna.tsinghua.edu.cn/simple
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter

RULES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules")

SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

DEFAULT_EXCLUDE_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "bower_components", "vendor", "third_party",
    "dist", "build", "out", "target", "bin", "obj", "__pycache__", ".venv", "venv",
    "env", ".env.d", "site-packages", ".idea", ".vscode", ".next", ".nuxt", ".output",
    "coverage", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".terraform",
    ".gradle", ".mvn", ".yarn", ".pylibs", ".trash", ".agent", "clones",
}

DEFAULT_EXCLUDE_GLOBS = [
    "*.min.js", "*.min.css", "*.map", "*.lock", "package-lock.json", "yarn.lock",
    "pnpm-lock.yaml", "go.sum", "*.sum", "*.pb.go", "*_pb2.py", "*_pb2_grpc.py",
    "*.svg", "*.css", "*.scss", "*.less", "*.woff", "*.woff2", "*.ttf", "*.eot",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.ico", "*.bmp", "*.pdf",
    "*.zip", "*.tar", "*.tar.gz", "*.tgz", "*.gz", "*.bz2", "*.7z", "*.rar",
    "*.jar", "*.war", "*.ear", "*.class", "*.pyc", "*.pyo", "*.so", "*.dylib",
    "*.dll", "*.exe", "*.bin", "*.o", "*.a", "*.db", "*.sqlite", "*.sqlite3",
    "*.csv", "*.parquet", "*.ipynb", "*.snap", "*.patch", "*.diff", "*.material",
]

LANG_BY_EXT = {
    ".py": "python", ".pyw": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".ts": "javascript", ".tsx": "javascript",
    ".vue": "javascript", ".svelte": "javascript",
    ".php": "php", ".php3": "php", ".php4": "php", ".php5": "php",
    ".php7": "php", ".phtml": "php", ".inc": "php", ".module": "php",
    ".java": "java", ".jsp": "java", ".jspx": "java", ".jspf": "java",
    ".go": "go",
    ".rb": "ruby", ".erb": "ruby",
    ".cs": "csharp",
    ".xml": "xml", ".yml": "yaml", ".yaml": "yaml",
    ".json": "json", ".env": "env", ".ini": "ini", ".conf": "conf",
    ".cfg": "conf", ".cnf": "conf", ".properties": "properties",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".ps1": "powershell",
    ".tf": "terraform", ".hcl": "terraform",
    ".html": "html", ".htm": "html", ".tpl": "html", ".twig": "html",
    ".sql": "sql", ".md": "markdown",
}

IGNORE_RX = re.compile(
    r"(?i)(nosec|security-audit:\s*ignore|#\s*lgtm|pragma:\s*allowlist|"
    r"nosemgrep|checkov:skip|bandit_skip)"
)

PLACEHOLDER_RX = re.compile(
    r"(?i)(example|placeholder|change[_-]?me|changeme|your[_-]|xxx+|\*\*\*|"
    r"<[^>]{0,24}>|todo|fixme|dummy|sample|fake|redacted|"
    r"getenv|environ|process\.env|\$\{|\{\{|%[a-z_]+%|<%|\bnull\b|\bnone\b|"
    r"^\s*$|secret|password|token|key)(?![a-z0-9])"
)


# --------------------------------------------------------------------------- #
# 规则加载
# --------------------------------------------------------------------------- #

_RULE_CACHE: dict[str, list[dict]] = {}


def _load_yaml(path: str) -> dict:
    try:
        import yaml  # type: ignore
    except ImportError:
        eprint("[!] 缺少 pyyaml,规则读不了。先装:")
        eprint("    pip install --target .pylibs pyyaml "
               "-i https://pypi.tuna.tsinghua.edu.cn/simple")
        sys.exit(3)
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_rules(filename: str) -> list[dict]:
    """读取 rules/<filename>,预编译正则。"""
    if filename in _RULE_CACHE:
        return _RULE_CACHE[filename]
    path = os.path.join(RULES_DIR, filename)
    if not os.path.exists(path):
        _RULE_CACHE[filename] = []
        return []
    data = _load_yaml(path)
    compiled: list[dict] = []
    for item in data.get("rules", []):
        item = dict(item)
        try:
            item["_re"] = re.compile(item["pattern"])
        except re.error as exc:
            eprint(f"[!] 规则 {item.get('id', '?')} 正则非法,跳过: {exc}")
            continue
        item.setdefault("severity", "medium")
        item.setdefault("confidence", "medium")
        item.setdefault("category", "uncategorized")
        item.setdefault("languages", ["*"])
        item["_source"] = filename.split(".")[0]
        compiled.append(item)
    _RULE_CACHE[filename] = compiled
    return compiled


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #

def eprint(*args) -> None:
    print(*args, file=sys.stderr)


def shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    counts = Counter(text)
    total = len(text)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def lang_of(rel: str) -> str:
    base = os.path.basename(rel)
    low = base.lower()
    if low == "dockerfile" or low.startswith("dockerfile."):
        return "dockerfile"
    if low in ("makefile", "gnumakefile"):
        return "makefile"
    ext = os.path.splitext(base)[1].lower()
    return LANG_BY_EXT.get(ext, ext.lstrip(".") or "text")


def is_binary(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(8192)
    except OSError:
        return True


def read_text(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def iter_files(root, exclude_dirs, exclude_globs, max_size):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in exclude_dirs and d not in DEFAULT_EXCLUDE_DIRS
        )
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if os.path.islink(full):
                continue
            if any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(name, g)
                   for g in exclude_globs):
                continue
            try:
                if os.path.getsize(full) > max_size:
                    continue
            except OSError:
                continue
            yield full, rel


def mk(rule: dict, rel: str, lineno: int, snippet: str, **over) -> dict:
    item = {
        "id": rule.get("id", "?"),
        "category": rule.get("category", ""),
        "severity": rule.get("severity", "medium"),
        "confidence": rule.get("confidence", "medium"),
        "cwe": rule.get("cwe", ""),
        "file": rel,
        "line": lineno,
        "snippet": " ".join(str(snippet).split())[:240],
        "message": rule.get("message", ""),
        "remediation": rule.get("remediation", ""),
        "source": rule.get("_source", "rule"),
    }
    item.update(over)
    return item


# --------------------------------------------------------------------------- #
# 各子扫描
# --------------------------------------------------------------------------- #

def scan_secrets(rel: str, lines: list[str], secret_rules: list[dict]) -> list[dict]:
    out = []
    base = os.path.basename(rel)
    for rule in secret_rules:
        globs = rule.get("file_globs")
        if globs and not any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(base, g)
                             for g in globs):
            continue
        rx: re.Pattern = rule["_re"]
        value_group = rule.get("value_group")
        min_entropy = rule.get("entropy")
        for lineno, line in enumerate(lines, 1):
            if IGNORE_RX.search(line):
                continue
            for match in rx.finditer(line):
                if value_group:
                    value = match.group(value_group) if match.lastindex else None
                    if not value:
                        continue
                else:
                    value = match.group(0)
                if PLACEHOLDER_RX.search(value):
                    continue
                if min_entropy is not None and shannon_entropy(value) < min_entropy:
                    continue
                out.append(mk(rule, rel, lineno, line,
                              detail=f"命中值: {value[:40]}"))
    return out


def scan_patterns(rel: str, lines: list[str], lang: str,
                  pattern_rules: list[dict]) -> list[dict]:
    out = []
    text = "\n".join(lines)
    for rule in pattern_rules:
        langs = rule.get("languages") or ["*"]
        if "*" not in langs and lang not in langs:
            continue
        rx: re.Pattern = rule["_re"]
        if rule.get("multiline"):
            for match in rx.finditer(text):
                lineno = text.count("\n", 0, match.start()) + 1
                line = lines[lineno - 1] if lineno - 1 < len(lines) else ""
                if IGNORE_RX.search(line):
                    continue
                out.append(mk(rule, rel, lineno, line))
        else:
            for lineno, line in enumerate(lines, 1):
                if IGNORE_RX.search(line):
                    continue
                if rx.search(line):
                    out.append(mk(rule, rel, lineno, line))
    return out


def scan_config(rel: str, lines: list[str], config_rules: list[dict]) -> list[dict]:
    out = []
    base = os.path.basename(rel)
    text = "\n".join(lines)
    for rule in config_rules:
        globs = rule.get("file_globs") or ["*"]
        if not any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(base, g) for g in globs):
            continue
        rx: re.Pattern = rule["_re"]
        if rule.get("multiline"):
            for match in rx.finditer(text):
                lineno = text.count("\n", 0, match.start()) + 1
                line = lines[lineno - 1] if lineno - 1 < len(lines) else ""
                if IGNORE_RX.search(line):
                    continue
                out.append(mk(rule, rel, lineno, line))
        else:
            for lineno, line in enumerate(lines, 1):
                if IGNORE_RX.search(line):
                    continue
                if rx.search(line):
                    out.append(mk(rule, rel, lineno, line))
    return out


def scan_builtin_config(root: str) -> list[dict]:
    """少数需要跨文件判断的配置风险,正则表达不了,写死在这里。"""
    out = []
    fake_rule = {"id": "CFG-ENV-IGNORED", "severity": "high", "confidence": "medium",
                 "category": "A05:2021-Security-Misconfiguration", "cwe": "CWE-538",
                 "message": "仓库里存在 .env 且 .gitignore 未忽略它,凭据可能被提交",
                 "remediation": "把 .env 加入 .gitignore 并 git rm --cached;改用密钥管理服务",
                 "_source": "builtin"}
    env_files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_EXCLUDE_DIRS]
        for name in filenames:
            if name == ".env" or name.startswith(".env.") or name.endswith(".env"):
                env_files.append(os.path.relpath(os.path.join(dirpath, name), root))
    if env_files:
        gi = os.path.join(root, ".gitignore")
        content = read_text(gi) or "" if os.path.exists(gi) else ""
        if not re.search(r"(?m)^\s*\.?env", content):
            out.append(mk(fake_rule, env_files[0].replace(os.sep, "/"), 1,
                          "存在 env 文件: " + ", ".join(env_files[:3]),
                          detail=f"共 {len(env_files)} 个 env 文件"))
    key_rule = {"id": "CFG-PRIVATE-KEY-FILE", "severity": "high", "confidence": "medium",
                "category": "A02:2021-Cryptographic-Failures", "cwe": "CWE-321",
                "message": "仓库里存在私钥文件",
                "remediation": "立刻吊销并轮换该密钥,从版本库彻底移除",
                "_source": "builtin"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_EXCLUDE_DIRS]
        for name in filenames:
            if name in ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519") or \
               name.endswith((".pem", ".p12", ".pfx", ".jks", ".keystore")):
                rel = os.path.relpath(os.path.join(dirpath, name), root)
                out.append(mk(key_rule, rel.replace(os.sep, "/"), 1, name))
    return out


# --------------------------------------------------------------------------- #
# 依赖漏洞(OSV)
# --------------------------------------------------------------------------- #

def _ver_clean(ver: str) -> str:
    ver = ver.strip().strip('"\'')
    ver = re.sub(r"^[\^~>=<\s]+", "", ver)
    ver = ver.split(",")[0].split(" ")[0]
    return ver if re.match(r"^\d", ver) else ""


def parse_deps(root: str, exclude_dirs) -> list[dict]:
    pkgs: list[dict] = []

    def add(ecosystem, name, version, src, where):
        version = _ver_clean(version or "")
        name = (name or "").strip()
        if not name or not version or "$" in version or "{" in version:
            return
        pkgs.append({"ecosystem": ecosystem, "name": name,
                     "version": version, "src": src, "where": where})

    for full, rel in iter_files(root, exclude_dirs, DEFAULT_EXCLUDE_GLOBS,
                                5 * 1024 * 1024):
        base = os.path.basename(rel)
        text = read_text(full)
        if text is None:
            continue
        if base.startswith("requirements") and rel.endswith(".txt"):
            for i, line in enumerate(text.splitlines(), 1):
                line = line.split("#")[0].strip()
                m = re.match(r"^([A-Za-z0-9_.\-]+)\s*==\s*([^\s;]+)", line)
                if m:
                    add("PyPI", m.group(1), m.group(2), rel, i)
        elif base == "package.json":
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            for section in ("dependencies", "devDependencies", "peerDependencies"):
                for name, ver in (data.get(section) or {}).items():
                    if isinstance(ver, str):
                        add("npm", name, ver, rel, 0)
        elif base == "go.mod":
            for i, line in enumerate(text.splitlines(), 1):
                m = re.match(r"^\s*([a-z0-9.\-]+\.[a-z]{2,}/[^\s]+)\s+(v[^\s]+)", line)
                if m:
                    add("Go", m.group(1), m.group(2), rel, i)
        elif base == "composer.json":
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            for section in ("require", "require-dev"):
                for name, ver in (data.get(section) or {}).items():
                    if isinstance(ver, str) and "/" in name:
                        add("Packagist", name, ver, rel, 0)
        elif base == "pom.xml":
            for m in re.finditer(r"<dependency>(.*?)</dependency>", text, re.S):
                block = m.group(1)
                g = re.search(r"<groupId>\s*([^<]+?)\s*</groupId>", block)
                a = re.search(r"<artifactId>\s*([^<]+?)\s*</artifactId>", block)
                v = re.search(r"<version>\s*([^<]+?)\s*</version>", block)
                if g and a and v:
                    add("Maven", f"{g.group(1)}:{a.group(1)}", v.group(1), rel,
                        text.count("\n", 0, m.start()) + 1)
        elif base == "Gemfile":
            for i, line in enumerate(text.splitlines(), 1):
                m = re.match(r"""^\s*gem\s+['"]([^'"]+)['"]\s*,\s*['"]([^'"]+)['"]""", line)
                if m:
                    add("RubyGems", m.group(1), m.group(2), rel, i)

    uniq = {}
    for p in pkgs:
        uniq[(p["ecosystem"], p["name"], p["version"])] = p
    return list(uniq.values())


def cvss3_score(vector: str) -> float | None:
    """极简 CVSS 3.x base score 计算,够判级别用。"""
    if not vector or "CVSS:3" not in vector:
        return None
    m = dict(re.findall(r"/([A-Z]{1,2}):([A-Z])", vector))
    av = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}.get(m.get("AV", ""), None)
    ac = {"L": 0.77, "H": 0.44}.get(m.get("AC", ""), None)
    ui = {"N": 0.85, "R": 0.62}.get(m.get("UI", ""), None)
    scope_changed = m.get("S") == "C"
    pr_key = m.get("PR", "")
    pr_tbl = {"N": 0.85, "L": 0.62 if not scope_changed else 0.68,
              "H": 0.27 if not scope_changed else 0.5}
    pr = pr_tbl.get(pr_key)
    imp = {"N": 0.0, "L": 0.22, "H": 0.56}
    c, i, a = imp.get(m.get("C", "")), imp.get(m.get("I", "")), imp.get(m.get("A", ""))
    if None in (av, ac, pr, ui, c, i, a):
        return None
    iss = 1 - (1 - c) * (1 - i) * (1 - a)
    if scope_changed:
        impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15
    else:
        impact = 6.42 * iss
    exploitability = 8.22 * av * ac * pr * ui
    if impact <= 0:
        return 0.0
    base = min((1.08 if scope_changed else 1.0) * (impact + exploitability), 10)
    return round(math.ceil(base * 10) / 10, 1)


def score_to_sev(score: float | None) -> str:
    if score is None:
        return "medium"
    if score >= 9:
        return "critical"
    if score >= 7:
        return "high"
    if score >= 4:
        return "medium"
    return "low"


def osv_vuln_detail(vid: str) -> dict:
    """取单条漏洞详情(querybatch 只回 ID 和 modified,细节要单独查)。"""
    import urllib.request
    try:
        with urllib.request.urlopen(
                "https://api.osv.dev/v1/vulns/" + vid, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception:  # noqa: BLE001
        return {}


def enrich_vulns(ids: list, limit: int = 80) -> dict:
    """并发补齐漏洞详情;个别失败就退回只用 ID,不让整体挂掉。"""
    from concurrent.futures import ThreadPoolExecutor
    uniq = list(dict.fromkeys(i for i in ids if i))[:limit]
    if not uniq:
        return {}
    out = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for vid, detail in zip(uniq, pool.map(osv_vuln_detail, uniq)):
            if detail:
                out[vid] = detail
    return out


def osv_query(pkgs: list[dict], batch: int = 300) -> dict:
    import urllib.request

    results: list[dict] = []
    for start in range(0, len(pkgs), batch):
        chunk = pkgs[start:start + batch]
        queries = [{"package": {"name": p["name"], "ecosystem": p["ecosystem"]},
                    "version": p["version"]} for p in chunk]
        body = json.dumps({"queries": queries}).encode()
        req = urllib.request.Request(
            "https://api.osv.dev/v1/querybatch", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode())
        results.extend(data.get("results", []))
        for p, res in zip(chunk, data.get("results", [])):
            p["_vulns"] = res.get("vulns", [])
    return {"pkgs": pkgs}


def scan_deps(root: str, exclude_dirs, offline: bool) -> tuple[list[dict], str]:
    pkgs = parse_deps(root, exclude_dirs)
    if not pkgs:
        return [], "没找到可解析的依赖清单"
    if offline:
        return [], f"解析出 {len(pkgs)} 个依赖(--offline,未查 OSV)"
    try:
        osv_query(pkgs)
    except Exception as exc:  # noqa: BLE001
        return [], f"OSV 查询失败({type(exc).__name__}: {exc}),跳过依赖漏洞"
    out = []
    all_ids = [v.get("id") for p in pkgs for v in (p.get("_vulns") or [])]
    details = enrich_vulns(all_ids)
    for p in pkgs:
        for vuln in p.get("_vulns", []) or []:
            vid = vuln.get("id", "UNKNOWN")
            info = details.get(vid) or {}
            score = None
            for sev in info.get("severity", []) or []:
                score = cvss3_score(sev.get("score", "")) or score
            db_sev = ((info.get("database_specific") or {}).get("severity") or "").upper()
            sev = {"CRITICAL": "critical", "HIGH": "high", "MODERATE": "medium",
                   "MEDIUM": "medium", "LOW": "low"}.get(db_sev)
            if not sev:
                sev = score_to_sev(score)
            aliases = [a for a in (info.get("aliases") or []) if a.startswith("CVE-")]
            summary = (info.get("summary") or "").strip()
            msg = summary or f"{p['name']}@{p['version']} 存在已知漏洞"
            if aliases:
                msg = " ".join(aliases) + " — " + msg
            out.append({
                "id": f"DEP-{vid}",
                "category": "A06:2021-Vulnerable-and-Outdated-Components",
                "severity": sev,
                "confidence": "high",
                "cwe": "",
                "file": p["src"],
                "line": p["where"],
                "snippet": f"{p['ecosystem']}:{p['name']}@{p['version']}",
                "message": msg[:300],
                "remediation": "升级到已修复版本;详见 https://osv.dev/vulnerability/" + vid,
                "source": "osv",
                "detail": " ".join(aliases + ([f"CVSS {score}"] if score else [])),
            })
    return out, (f"解析出 {len(pkgs)} 个依赖,命中 {len(out)} 个已知漏洞")


def scan_bandit(root: str) -> tuple[list[dict], str]:
    cmd = [sys.executable, "-m", "bandit", "-r", root, "-f", "json", "-q"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        return [], "bandit 不可用,跳过"
    except subprocess.TimeoutExpired:
        return [], "bandit 超时,跳过"
    raw = proc.stdout.strip()
    if not raw:
        return [], "bandit 无输出(可能未安装),跳过"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return [], "bandit 输出无法解析,跳过"
    sev_map = {"HIGH": "high", "MEDIUM": "medium", "LOW": "low"}
    out = []
    for r in data.get("results", []):
        rel = os.path.relpath(r.get("filename", ""), root).replace(os.sep, "/")
        out.append({
            "id": f"BANDIT-{r.get('test_id', '?')}",
            "category": "bandit:" + str(r.get("test_name", "")),
            "severity": sev_map.get(r.get("issue_severity", ""), "medium"),
            "confidence": {"HIGH": "high", "MEDIUM": "medium",
                           "LOW": "low"}.get(r.get("issue_confidence", ""), "medium"),
            "cwe": (r.get("issue_cwe") or {}).get("id", "") and
                   f"CWE-{(r.get('issue_cwe') or {}).get('id')}" or "",
            "file": rel,
            "line": r.get("line_number", 0),
            "snippet": " ".join((r.get("code") or "").split())[:240],
            "message": r.get("issue_text", ""),
            "remediation": "见 bandit 规则 " + str(r.get("test_id", "")),
            "source": "bandit",
        })
    return out, f"bandit 命中 {len(out)} 条"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def summarize(findings: list[dict], stats: dict, notes: list[str],
              top: int, json_only: bool) -> None:
    if json_only:
        return
    sev_count = Counter(f["severity"] for f in findings)
    print("=" * 68)
    print(f"目标: {stats['target']}")
    print(f"扫描: {stats['files']} 个文件 / {stats['lines']} 行")
    print(f"候选: {len(findings)} 条   " + "  ".join(
        f"{s}={sev_count.get(s, 0)}" for s in
        ("critical", "high", "medium", "low", "info")) )
    for note in notes:
        print(f"  · {note}")
    if findings:
        print("-" * 68)
        by_cat = Counter(f["category"] for f in findings)
        print("按类别:")
        for cat, n in by_cat.most_common():
            print(f"  {n:4d}  {cat}")
        print("-" * 68)
        print(f"Top {min(top, len(findings))}(按严重度,仅供参考,必须逐条人工确认):")
        for f in findings[:top]:
            print(f"  [{f['severity'].upper():8s}] {f['id']}  {f['file']}:{f['line']}"
                  f"  (置信度 {f['confidence']})")
            print(f"             {f['message']}")
            if f["snippet"]:
                print(f"             > {f['snippet'][:120]}")
    print("=" * 68)
    print(f"完整结果已写入: {stats['out']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="repo-security-audit 快扫器(命中=待确认候选,不是确认漏洞)")
    ap.add_argument("root", help="目标仓库路径(相对工作区,如 clones/x/clones/foo)")
    ap.add_argument("commands", nargs="*", default=[],
                    help="secrets patterns config deps bandit all")
    ap.add_argument("--out", default="findings.json")
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--max-size", type=int, default=1500 * 1024)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--json-only", action="store_true")
    ap.add_argument("--top", type=int, default=25)
    opts = ap.parse_args(argv)

    root = os.path.abspath(opts.root)
    if not os.path.isdir(root):
        eprint(f"[!] 目标不是目录: {root}")
        return 2

    which = set(opts.commands) or {"all"}
    if "all" in which or not which:
        which = {"secrets", "patterns", "config", "deps", "bandit"}

    secret_rules = load_rules("secrets.yaml") if "secrets" in which else []
    pattern_rules = load_rules("sinks.yaml") if "patterns" in which else []
    config_rules = load_rules("config.yaml") if "config" in which else []

    exclude_globs = list(DEFAULT_EXCLUDE_GLOBS) + list(opts.exclude)

    findings: list[dict] = []
    notes: list[str] = []
    n_files = n_lines = 0

    for full, rel in iter_files(root, set(), exclude_globs, opts.max_size):
        if is_binary(full):
            continue
        text = read_text(full)
        if text is None:
            continue
        n_files += 1
        lines = text.splitlines()
        n_lines += len(lines)
        lang = lang_of(rel)
        try:
            if secret_rules:
                findings += scan_secrets(rel, lines, secret_rules)
            if pattern_rules:
                findings += scan_patterns(rel, lines, lang, pattern_rules)
            if config_rules:
                findings += scan_config(rel, lines, config_rules)
        except Exception as exc:  # noqa: BLE001
            eprint(f"[!] {rel} 扫描异常: {type(exc).__name__}: {exc}")

    if "config" in which:
        findings += scan_builtin_config(root)

    if "deps" in which:
        dep_findings, dep_note = scan_deps(root, set(), opts.offline)
        findings += dep_findings
        notes.append("依赖: " + dep_note)

    if "bandit" in which:
        bandit_findings, bandit_note = scan_bandit(root)
        findings += bandit_findings
        notes.append("bandit: " + bandit_note)

    # 去重 + 排序
    uniq = {}
    for f in findings:
        key = (f["id"], f["file"], f["line"])
        if key not in uniq:
            uniq[key] = f
    findings = sorted(uniq.values(),
                      key=lambda f: (SEV_ORDER.get(f["severity"], 9),
                                     f["file"], f["line"]))

    stats = {"target": root, "files": n_files, "lines": n_lines,
             "total": len(findings), "out": os.path.abspath(opts.out)}

    with open(opts.out, "w", encoding="utf-8") as fh:
        json.dump({"stats": stats, "findings": findings}, fh,
                  ensure_ascii=False, indent=2)

    summarize(findings, stats, notes, opts.top, opts.json_only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
