#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""code-risk-check 静态扫描器。

只读文本、不执行目标代码,输出"候选命中"供人工复核。
命中的 level_hint 是"假如这条行为成立"的等级上界,**不是结论**。

用法:
    python3 risk_scan.py <目标路径> [--json out.json] [--min-severity low] [--quiet]
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from collections import Counter, defaultdict

SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "vendor", "dist", "build",
    "__pycache__", ".venv", "venv", ".tox", "target", ".idea", ".vscode",
    "site-packages", "bower_components", ".mypy_cache", ".pytest_cache",
    "coverage", ".next", ".nuxt", "out",
}
SKIP_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".pdf", ".zip",
    ".gz", ".tar", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".jar", ".war",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".pyc", ".pyo", ".class",
    ".mp3", ".mp4", ".avi", ".mov", ".woff", ".woff2", ".ttf", ".eot",
    ".lock", ".map",
}
LANG_EXT = {
    ".py": "py", ".pyw": "py",
    ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "js",
    ".ts": "js", ".tsx": "js", ".vue": "js",
    ".php": "php", ".phtml": "php",
    ".lua": "lua",
    ".sh": "sh", ".bash": "sh", ".zsh": "sh", ".ksh": "sh",
    ".ps1": "ps1", ".psm1": "ps1",
    ".go": "go", ".java": "java", ".kt": "java", ".cs": "cs", ".rb": "rb",
    ".pl": "pl", ".pm": "pl",
}
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_LINE_MATCH = 4000
MAX_SNIPPET = 200
LONG_LINE = 600

SEV_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}

RULES = []


def rule(rid, cat, sev, level, langs, pat, note):
    RULES.append({
        "id": rid, "category": cat, "severity": sev, "level_hint": level,
        "langs": set(x.strip() for x in langs.split(",")),
        "re": re.compile(pat, re.I), "note": note,
    })


# ---------------------------------------------------------------- 读取 / 凭据
rule("read-ssh-key", "凭据访问", "high", "L2", "any",
     r"\.ssh/|id_rsa|id_ed25519|id_ecdsa|\bknown_hosts\b",
     "SSH 私钥/密钥目录,确认读取用途与是否外发")
rule("read-cloud-cred", "凭据访问", "high", "L2", "any",
     r"\.aws/(credentials|config)|\.kube/config|\.docker/config\.json|\.npmrc|\.pypirc|\.netrc|\.git-credentials|gcloud/(credentials|application_default)",
     "云与包管理器凭据文件")
rule("read-sys-cred", "凭据访问", "high", "L2", "any",
     r"/etc/(passwd|shadow|sudoers)|/proc/(self|\d+)/environ|SAM\b|System32\\\\config",
     "系统凭据/环境变量文件")
rule("read-browser-data", "凭据访问", "high", "L2", "any",
     r"Login Data|Cookies|cookies\.sqlite|Web Data|Local State|places\.sqlite|logins\.json|key[34]\.db|User Data[\\/]|Local Storage|leveldb",
     "浏览器凭据与隐私数据")
rule("read-wallet", "凭据访问", "high", "L2", "any",
     r"wallet\.dat|keystore|MetaMask|Exodus|Electrum|\.ethereum",
     "加密货币钱包数据")
rule("read-env-secret", "凭据访问", "medium", "L2", "py,js",
     r"(os\.environ|process\.env)[\[\.][^\]]{0,60}(KEY|TOKEN|SECRET|PASS|CRED|AUTH)",
     "从环境变量取密钥类值")
rule("hardcoded-secret", "凭据访问", "medium", None, "any",
     r"(api[_-]?key|access[_-]?token|client[_-]?secret|private[_-]?key|password|passwd|pwd)\s*[:=]\s*['\"][^'\"\s]{8,}['\"]",
     "疑似硬编码凭据,注意随代码泄露")

# ---------------------------------------------------------------- 隐私采集
rule("priv-keylog", "隐私采集", "high", "L2", "any",
     r"pynput|keyboard\.(on_press|Listener)|GetAsyncKeyState|SetWindowsHookEx|WH_KEYBOARD",
     "键盘输入采集")
rule("priv-clipboard", "隐私采集", "medium", "L2", "any",
     r"pyperclip|win32clipboard|pbpaste|xclip|clipboard\.readText|navigator\.clipboard|GetClipboardData",
     "剪贴板读取")
rule("priv-screen", "隐私采集", "medium", "L2", "any",
     r"ImageGrab|mss\.|\bpyautogui\b|screenshot|screencapture|scrot|gnome-screenshot|GetDC\(|BitBlt",
     "屏幕截取")
rule("priv-camera", "隐私采集", "high", "L2", "any",
     r"cv2\.VideoCapture|VideoCapture\(|pyaudio|sounddevice|MediaRecorder|getUserMedia|mediaDevices",
     "摄像头/麦克风采集")
rule("priv-location", "隐私采集", "medium", "L2", "any",
     r"navigator\.geolocation|\bgeolocation\b|geoip|ipinfo\.io|ip-api\.com|ipapi\.co|getLocation\(|CLLocation",
     "位置/归属地查询")
rule("priv-fingerprint", "隐私采集", "medium", "L2", "any",
     r"machine-id|uuid\.getnode|\bgetmac\b|wmic csproduct|netsh wlan show|hardware_concurrency|navigator\.platform",
     "设备指纹采集")

# ---------------------------------------------------------------- 网络
rule("net-py-http", "网络", "medium", "L3", "py",
     r"\b(requests|httpx|aiohttp|urllib3)\.(get|post|put|patch|delete|request|stream)\b|urlopen|urlretrieve",
     "Python HTTP 出站调用")
rule("net-py-socket", "网络", "medium", "L3", "py",
     r"\bsocket\.(socket|create_connection|getaddrinfo|gethostbyname)|\bsocketserver\b",
     "原始 socket / 域名解析")
rule("net-py-mail", "网络", "medium", "L3", "py",
     r"\bsmtplib\b|sendmail|send_message|win32com.*Outlook",
     "邮件外发")
rule("net-py-ssh", "网络", "medium", "L3", "py",
     r"\bparamiko\b|SSHClient|fabric|pysftp|telnetlib",
     "SSH/FTP/Telnet 远程连接")
rule("net-raw", "网络", "high", "L4", "any",
     r"SOCK_RAW|AF_PACKET|\bscapy\b|raw_?socket|pcap",
     "原始套接字/抓包,可能用于隐蔽通信")
rule("net-im", "网络", "medium", "L3", "any",
     r"webhook|bot[_-]?token|api\.telegram\.org|discord(app)?\.com/api|sendMessage|dingtalk|qyapi\.weixin",
     "IM/Webhook 外发通道")
rule("net-js-fetch", "网络", "medium", "L3", "js",
     r"\bfetch\s*\(|\baxios\b|XMLHttpRequest|\$\.ajax|\.post\s*\(|superagent|got\(",
     "JS HTTP 出站调用")
rule("net-js-http", "网络", "medium", "L3", "js",
     r"require\(['\"](https?|net|dgram|tls)['\"]\)|from\s+['\"](https?|net|dgram|tls)['\"]|http\.request|net\.connect",
     "Node 网络模块")
rule("net-js-ws", "网络", "medium", "L3", "js",
     r"new\s+WebSocket|socket\.io|\bws\(",
     "长连接 WebSocket")
rule("net-php", "网络", "medium", "L3", "php",
     r"curl_(init|exec|setopt|multi)|fsockopen|stream_socket_client|file_get_contents\s*\(\s*['\"]https?://|get_headers\s*\(\s*['\"]https?://|mail\s*\(",
     "PHP 网络请求")
rule("net-lua", "网络", "medium", "L3", "lua",
     r"socket\.(tcp|udp|connect|http)|http\.request|ngx\.socket|resty\.http|\bluasocket\b",
     "Lua 网络请求")
rule("net-shell", "网络", "medium", "L3", "sh",
     r"\b(curl|wget|nc|ncat|socat|telnet|ssh|scp|rsync)\b",
     "Shell 网络命令")
rule("net-ps1", "网络", "medium", "L3", "ps1",
     r"Invoke-WebRequest|Invoke-RestMethod|DownloadString|DownloadFile|WebClient|System\.Net\.Sockets",
     "PowerShell 网络请求")
rule("net-other", "网络", "medium", "L3", "go,java,cs,rb,pl",
     r"net\.Dial|http\.(Get|Post|NewRequest)|HttpClient|RestTemplate|WebRequest|RestClient|TCPSocket|Net::HTTP|LWP::UserAgent|HTTP::Tiny",
     "网络请求")
rule("net-url", "网络", "info", None, "any",
     r"https?://[^\s'\"<>\)\]]+",
     "出现 URL 字符串,确认是否作为访问目的地")
rule("net-hardcoded-ip", "网络", "info", None, "any",
     r"\b(?!127\.0\.0\.1\b)(?!0\.0\.0\.0\b)(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b",
     "硬编码 IP,确认是否可控/可信目的地")

# ---------------------------------------------------------------- 执行
rule("exec-py", "执行", "medium", "L4", "py",
     r"\bos\.(system|popen|spawn\w*|exec\w*)\b|subprocess\.(run|Popen|call|check_call|check_output|getoutput)|\bpty\.spawn\b",
     "Python 起进程/命令")
rule("eval-py", "执行", "high", "L4", "py",
     r"(?<![\w.])eval\s*\(|(?<![\w.])exec\s*\(|\bcompile\s*\(|marshal\.loads|pickle\.loads|__import__\s*\(",
     "Python 动态执行代码")
rule("ctypes-py", "执行", "high", "L4", "py",
     r"\bctypes\b|\bCDLL\b|\bwindll\b|\bWinDLL\b|LoadLibrary",
     "调用原生库/系统 API")
rule("exec-js", "执行", "high", "L4", "js",
     r"child_process|execSync|spawnSync|\bexec\s*\(|\bspawn\s*\(|\bvm\.(runIn\w*|createContext)|new\s+Function",
     "Node 起进程/动态执行")
rule("eval-js", "执行", "medium", "L4", "js",
     r"(?<![\w.])eval\s*\(|setTimeout\s*\(\s*['\"`]|setInterval\s*\(\s*['\"`]|atob\s*\(|fromCharCode",
     "JS 动态求值/解码")
rule("exec-php", "执行", "high", "L4", "php",
     r"\b(system|exec|shell_exec|passthru|popen|proc_open|pcntl_exec)\s*\(|(?<![\w>])eval\s*\(|assert\s*\(|create_function|preg_replace\s*\([^)]*/e",
     "PHP 命令执行/动态求值")
rule("exec-php-include", "执行", "high", "L4", "php",
     r"\b(include|require)(_once)?\s*\(?\s*\$|\b(call_user_func|array_map)\s*\(\s*\$",
     "PHP 动态包含/回调")
rule("exec-lua", "执行", "high", "L4", "lua",
     r"os\.execute|io\.popen|loadstring\s*\(|(?<![\w.])load\s*\(|dofile|require\s*\(\s*[^'\"]",
     "Lua 动态执行")
rule("exec-shell", "执行", "medium", "L4", "sh",
     r"(?<![\w])eval\b|bash\s+-c|sh\s+-c|exec\s+",
     "Shell 动态执行")
rule("exec-ps1", "执行", "high", "L4", "ps1",
     r"Invoke-Expression|\bIEX\b|-EncodedCommand|-enc\b|FromBase64String|Add-Type|Start-Process",
     "PowerShell 动态执行")

# ---------------------------------------------------------------- 写入 / 删除
rule("fs-write", "写入/删除", "low", "L1", "any",
     r"open\s*\([^)]*['\"][wa]\+?b?['\"]|\.write\s*\(|writeFile|appendFile|createWriteStream|file_put_contents|fwrite|io\.open\s*\([^)]*['\"][wa]|Add-Content|Set-Content|Out-File",
     "文件写入")
rule("fs-delete", "写入/删除", "medium", "L1", "any",
     r"shutil\.rmtree|os\.(remove|unlink|rmdir)|fs\.(unlink|rm)Sync|fs\.rm\b|unlink\s*\(|rmdir\s*\(|Remove-Item|\brm\s+-rf|\bdel\s+/[fqs]",
     "文件/目录删除")
rule("fs-chmod", "写入/删除", "medium", "L1", "any",
     r"os\.chmod|chmod\s+[ugo]*[+\-=]?[rwx0-7]|Set-ExecutionPolicy|attrib\s+\+|icacls",
     "修改文件权限")
rule("fs-syspath", "系统路径", "high", "L1", "any",
     r"/etc/|C:\\\\Windows|System32|Program Files|/usr/(bin|lib|share|local)|/Library/|site-packages|/boot/",
     "触及系统目录路径")
rule("fs-rc-file", "持久化", "high", "L1", "any",
     r"\.(bashrc|zshrc|bash_profile|profile|bash_login)|/etc/profile",
     "修改 shell 启动文件")

# ---------------------------------------------------------------- 持久化 / 提权
rule("persist-cron", "持久化", "high", "L1", "any",
     r"\bcrontab\b|/etc/cron|/var/spool/cron|schtasks|\bat\s+\d{1,2}:\d{2}|systemd|systemctl\s+(enable|start)|rc\.local|init\.d",
     "计划任务/服务自启")
rule("persist-reg", "持久化", "high", "L1", "any",
     r"reg\s+add|HKEY_|HKLM|HKCU|CurrentVersion\\\\Run|\bwinreg\b|RegistryKey",
     "注册表自启项")
rule("persist-macos", "持久化", "high", "L1", "any",
     r"LaunchAgents|LaunchDaemons|launchctl|\.plist",
     "macOS 自启项")
rule("priv-esc", "提权", "high", "L1", "any",
     r"\bsudo\b|\bdoas\b|\brunas\b|pkexec|os\.set(e)?uid|chmod\s+[ugo]*\+s|ShellExecute.*runas|AdjustTokenPrivileges|SeDebugPrivilege|RequestExecutionLevel",
     "提权操作")
rule("priv-disable-sec", "提权", "high", "L4", "any",
     r"Set-MpPreference|DisableRealtimeMonitoring|netsh advfirewall set .* off|setenforce\s+0|service .*(stop|disable).*(defender|avast|kaspersky)|winDefend",
     "关闭安全防护")

# ---------------------------------------------------------------- 反分析
rule("obf-decode", "反分析", "medium", "L4", "any",
     r"base64\.(b64decode|decodebytes|standard_b64decode)|urlsafe_b64decode|codecs\.decode|binascii\.unhexlify|bytes\.fromhex|atob\s*\(|Buffer\.from\([^,]+,\s*['\"]base64|FromBase64String|base64_decode|Convert\.FromBase64String|mime\.unb64",
     "base64/hex 解码,确认解码产物是否被执行")
rule("obf-decompress", "反分析", "medium", "L4", "any",
     r"zlib\.decompress|gzip\.decompress|bz2\.decompress|lzma\.decompress|gzinflate|gzuncompress|Inflate|Decompress|unzip_sync",
     "压缩载荷解包")
rule("obf-eval-combo", "反分析", "high", "L4", "any",
     r"(eval|exec|Function|loadstring|assert)\s*\([^)]{0,80}(decode|decrypt|unescape|atob|fromCharCode)",
     "解码后直接执行,典型混淆加载")
rule("obf-remote-fetch", "反分析", "high", "L4", "any",
     r"(urlopen|requests\.get|fetch|http\.get|file_get_contents)\s*\([^)]{0,80}\).{0,40}(exec|eval|system|sh|bash)",
     "从网络取码并执行")
rule("anti-vm", "反分析", "high", "L4", "any",
     r"\bVMware\b|VirtualBox|VBoxManage|\bqemu\b|hypervisor|SbieDll|sbiedll|cuckoo|joesandbox|vmtoolsd|/sys/class/dmi|product_name.*Virtual",
     "反虚拟机检测")
rule("anti-debug", "反分析", "high", "L4", "any",
     r"\bptrace\b|IsDebuggerPresent|CheckRemoteDebuggerPresent|NtQueryInformationProcess|sysctl.*kern\.proc|DebugActiveProcess",
     "反调试检测")
rule("anti-av", "反分析", "high", "L4", "any",
     r"MsMpEng|Windows Defender|WinDefend|avast|kaspersky|360tray|Process32First.*(av|defender)|EnumProcesses",
     "枚举安全软件/沙箱特征")
rule("anti-log-wipe", "反分析", "high", "L4", "any",
     r"wevtutil\s+cl|Clear-EventLog|rm\s+.*\.(bash_history|zsh_history)|history\s+-c|shred\s+|/var/log/\w+\s*.{0,20}(truncate|>|rm)",
     "清除日志/痕迹")
rule("obf-long-b64", "反分析", "medium", None, "any",
     r"[A-Za-z0-9+/]{200,}={0,2}",
     "大块 base64 字符串,可能是编码载荷")

# ---------------------------------------------------------------- 供应链
rule("sc-postinstall", "供应链", "medium", "L1", "any",
     r"\"(pre|post)install\"\s*:|cmdclass\s*=.*build_py|\"prepare\"\s*:",
     "安装钩子脚本,安装时会执行代码")
rule("sc-pipe-sh", "供应链", "high", "L4", "any",
     r"(curl|wget|Invoke-WebRequest)[^\n|]{0,200}\|\s*(ba|z|da)?sh",
     "下载后管道直接执行")
rule("sc-url-binary", "供应链", "high", "L4", "any",
     r"pip install .*https?://|npm install .*https?://|--index-url|curl\s+-O|wget\s+-O|chmod\s+\+x\s+.*(curl|wget)|iex\s*\(new-object",
     "从远程拉取并执行二进制/依赖")

# ---------------------------------------------------------------- 破坏 / 挖矿 / 后门
rule("ransom-encrypt", "破坏与加密", "high", "L4", "any",
     r"ransom|\.locked\b|\.encrypted\b|vssadmin\s+delete\s+shadows|bcdedit|wbadmin\s+delete|bitcoin|\.onion|decrypt.*instructions",
     "勒索软件特征")
rule("wipe", "破坏与加密", "high", "L4", "any",
     r"dd\s+if=/dev/(zero|urandom)|mkfs|\bshred\b|srm\b|cipher\s+/w|format\s+[A-Z]:",
     "数据销毁/覆写")
rule("miner", "资源滥用", "high", "L4", "any",
     r"stratum\+tcp|xmrig|minerd|randomx|cryptonight|nicehash|monero|\bmining\b|pool\.\w+\.(com|net|org)",
     "挖矿特征")
rule("backdoor", "执行", "high", "L4", "any",
     r"/dev/tcp/|nc\s+-e|ncat\s+-e|socat\s+.*exec|bash\s+-i\s+>&|reverse\s*shell|meterpreter|msfvenom|msfconsole|bind\s*shell|\bchisel\b|\bfrp\b",
     "后门/反向 shell/远控")
rule("telemetry", "网络", "info", None, "any",
     r"telemetry|analytics|gtag|googletagmanager|sentry|posthog|mixpanel|amplitude|bugsnag|crashlytics|firebase",
     "遥测/统计上报,确认是否默认开启、传什么")

# ---------------------------------------------------------------- 副作用与残留
rule("side-temp", "副作用", "low", "L1", "any",
     r"\btempfile\.|/tmp/|%TEMP%|%TMP%|os\.tmpdir|mkstemp|mkdtemp|NamedTemporaryFile|TemporaryFile",
     "临时文件:确认写在哪、退出时是否清理")
rule("side-cache", "副作用", "low", "L1", "any",
     r"__pycache__|XDG_CACHE_HOME|cache_dir|\.pytest_cache|\.mypy_cache|node_modules/\.cache",
     "缓存目录残留")
rule("side-log", "副作用", "low", "L1", "any",
     r"FileHandler|RotatingFileHandler|\.log['\"]|logfile|logging\.basicConfig|winston|bunyan|log4j",
     "日志文件落盘:确认路径、大小、是否轮转清理")
rule("side-lock", "副作用", "low", "L1", "any",
     r"\.lock\b|\.pid\b|flock|lockfile|LockFile|fcntl\.flock|msvcrt\.locking",
     "锁文件/PID 文件:异常退出可能残留")
rule("side-home", "副作用", "medium", "L1", "any",
     r"expanduser|os\.path\.home\(|Path\.home\(\)|APPDATA|LOCALAPPDATA|XDG_(CONFIG|DATA|CACHE|STATE)_HOME|~/\.\w+",
     "写入用户主目录/配置目录")
rule("side-env-set", "副作用", "medium", "L1", "any",
     r"os\.environ\[[^\]]+\]\s*=|os\.putenv|process\.env\.\w+\s*=|setx\s+\w+|export\s+[A-Z_]+=",
     "修改环境变量:仅影响自身进程,若写进 shell 配置则影响后续会话")
rule("side-config-write", "副作用", "low", "L1", "any",
     r"(config|settings)\.(json|ini|yaml|yml|toml)|\.config[\\/]|configparser\.|json\.dump\([^)]*config",
     "写配置文件")
rule("side-daemon", "副作用", "medium", "L1", "any",
     r"daemonize|setsid|nohup|start_new_session|service\s+start|systemctl\s+start",
     "后台/常驻运行:退出后可能仍有进程存活")
rule("side-cleanup", "副作用", "info", None, "any",
     r"atexit|TemporaryDirectory|cleanup|finally:\s*$",
     "疑似清理逻辑:确认是否覆盖全部临时产物(不能只看有没有 cleanup 函数)")


LANG_ALIAS = {"ts": "js", "tsx": "js"}


def lang_of(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in LANG_EXT:
        return LANG_EXT[ext]
    base = os.path.basename(path)
    if base in ("Dockerfile", "Makefile", "Rakefile", "Gemfile"):
        return "sh" if base in ("Makefile", "Dockerfile") else "rb"
    if base.startswith(".") and base.count(".") == 1:
        return "sh"
    if ext in (".txt", ".md", ".json", ".xml", ".yml", ".yaml", ".ini", ".conf", ".cfg", ".env", ""):
        return "any"
    return None


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def applicable(lang):
    out = []
    for r in RULES:
        if "any" in r["langs"] or lang in r["langs"]:
            out.append(r)
    return out


def scan_file(path, rel, lang):
    findings = []
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_FILE_BYTES + 1)
    except OSError:
        return findings
    if len(raw) > MAX_FILE_BYTES:
        raw = raw[:MAX_FILE_BYTES]
    if is_binary(raw):
        return findings
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return findings

    rules = applicable(lang)
    for lineno, line in enumerate(text.splitlines(), 1):
        probe = line[:MAX_LINE_MATCH]
        if len(line) > LONG_LINE:
            findings.append({
                "id": "obf-long-line", "category": "反分析", "severity": "low",
                "level_hint": None, "lang": lang, "file": rel, "line": lineno,
                "snippet": probe[:MAX_SNIPPET].strip(),
                "note": "超长单行(%d 字符),常见于压缩/混淆代码" % len(line),
            })
        for r in rules:
            m = r["re"].search(probe)
            if m:
                findings.append({
                    "id": r["id"], "category": r["category"], "severity": r["severity"],
                    "level_hint": r["level_hint"], "lang": lang, "file": rel,
                    "line": lineno, "snippet": probe.strip()[:MAX_SNIPPET],
                    "note": r["note"], "match": m.group(0)[:80],
                })
    return findings


def walk(target):
    if os.path.isfile(target):
        yield target, os.path.basename(target)
        return
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".git")]
        for name in sorted(files):
            ext = os.path.splitext(name)[1].lower()
            if ext in SKIP_EXT:
                continue
            full = os.path.join(root, name)
            yield full, os.path.relpath(full, target)


# ---------------------------------------------------------------- 运行时依赖
STDLIB_PY = {
    "os", "sys", "re", "json", "time", "datetime", "math", "random", "itertools",
    "collections", "functools", "pathlib", "subprocess", "shutil", "tempfile",
    "logging", "argparse", "typing", "io", "csv", "sqlite3", "socket", "ssl",
    "threading", "multiprocessing", "queue", "hashlib", "hmac", "base64", "binascii",
    "urllib", "http", "email", "smtplib", "ftplib", "getpass", "glob", "fnmatch",
    "pickle", "copy", "enum", "dataclasses", "abc", "contextlib", "warnings",
    "traceback", "unittest", "uuid", "platform", "signal", "stat", "string",
    "struct", "tarfile", "zipfile", "gzip", "bz2", "lzma", "secrets", "textwrap",
    "unicodedata", "webbrowser", "xml", "html", "configparser", "ctypes", "curses",
    "difflib", "dis", "errno", "filecmp", "getopt", "heapq", "imaplib", "importlib",
    "inspect", "ipaddress", "keyword", "locale", "mimetypes", "mmap", "operator",
    "optparse", "pdb", "pkgutil", "poplib", "pprint", "pty", "pwd", "py_compile",
    "pydoc", "runpy", "select", "shelve", "shlex", "site", "statistics", "sysconfig",
    "telnetlib", "termios", "timeit", "tkinter", "tokenize", "tomllib", "types",
    "venv", "wave", "weakref", "wsgiref", "zlib", "zoneinfo", "__future__", "builtins",
}
NODE_BUILTIN = {
    "fs", "path", "os", "http", "https", "net", "dgram", "url", "util", "crypto",
    "stream", "events", "child_process", "cluster", "dns", "tls", "zlib", "buffer",
    "assert", "console", "process", "querystring", "readline", "repl", "string_decoder",
    "timers", "tty", "v8", "vm", "worker_threads", "async_hooks", "perf_hooks",
    "module", "constants", "domain",
}
SIDE_CATS = ("副作用", "写入/删除", "系统路径", "持久化")
MANIFEST_NAMES = {
    "requirements.txt", "requirements-dev.txt", "package.json", "Pipfile",
    "pyproject.toml", "setup.py", "setup.cfg", "composer.json", "go.mod",
    "Gemfile", "environment.yml", "Cargo.toml",
}
CMD_CALL = re.compile(
    r"(?:os\.system|os\.popen|subprocess\.\w+|pty\.spawn|shell_exec|passthru|proc_open"
    r"|popen|system|exec|os\.execute|io\.popen|child_process\.\w+|execSync|spawnSync)"
    r"\s*\(\s*[fur]?['\"]([^'\"]{1,300})", re.I)
CMD_CALL_LIST = re.compile(
    r"(?:subprocess\.\w+|Popen|child_process\.\w+|execSync|spawnSync|spawn)"
    r"\s*\(\s*\[\s*['\"]([^'\"]{1,200})", re.I)
SHELL_WORDS = {
    "if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done",
    "case", "esac", "function", "return", "exit", "local", "set", "export",
    "source", "cd", "echo", "printf", "read", "test", "shift", "trap", "umask",
    "wait", "break", "continue", "[", "[[",
}
ENV_RES = [
    re.compile(r"""os\.environ(?:\.get)?\s*[\(\[]\s*['"]([A-Za-z_]\w*)['"]"""),
    re.compile(r"""process\.env\.([A-Za-z_]\w*)"""),
    re.compile(r"""getenv\s*\(\s*['"]([A-Za-z_]\w*)['"]"""),
]
PLATFORM_RES = {
    "windows": re.compile(r"C:\\\\|win32|WinDLL|HKEY_|winreg|System32|\.exe\b", re.I),
    "linux": re.compile(r"/etc/|/usr/bin|apt-get|systemd|/proc/", re.I),
    "macos": re.compile(r"LaunchAgents|/Library/|osascript|\bbrew\b", re.I),
}


def _read_text(path, limit=MAX_FILE_BYTES):
    try:
        with open(path, "rb") as f:
            raw = f.read(limit + 1)
    except OSError:
        return None
    if is_binary(raw):
        return None
    return raw[:limit].decode("utf-8", errors="replace")


def _parse_manifest(name, text):
    """从依赖声明文件里粗略提取依赖项(线索级,不做完整解析)。"""
    items = []
    try:
        if name in ("package.json", "composer.json"):
            doc = json.loads(text)
            for key in ("dependencies", "devDependencies", "peerDependencies",
                        "optionalDependencies", "require", "require-dev"):
                block = doc.get(key)
                if isinstance(block, dict):
                    items += ["%s@%s" % (k, v) for k, v in block.items()]
        elif name.startswith("requirements"):
            for line in text.splitlines():
                s = line.strip()
                if s and not s.startswith(("#", "-")):
                    items.append(s)
        elif name == "Gemfile":
            items += re.findall(r"""^\s*gem\s+['"]([^'"]+)['"]""", text, re.M)
        elif name == "go.mod":
            block = re.search(r"require\s*\((.*?)\)", text, re.S)
            if block:
                for line in block.group(1).splitlines():
                    s = line.strip()
                    if s and not s.startswith("//"):
                        items.append(s.split()[0])
            items += re.findall(r"^require\s+(\S+)\s", text, re.M)
        elif name.endswith("environment.yml"):
            items += [s.strip().lstrip("- ").strip() for s in text.splitlines()
                      if s.strip().startswith("- ")]
        elif name in ("Pipfile", "pyproject.toml", "setup.cfg", "Cargo.toml"):
            items += re.findall(r"""^\s*([A-Za-z][\w.\-]{1,40})\s*=\s*["'{]""", text, re.M)
        elif name == "setup.py":
            items += re.findall(r"""['"]([A-Za-z][\w.\-]{1,40})\s*[<>=~!]""", text)
    except Exception:
        pass
    return items


def collect_dependencies(target):
    d = {"runtimes": Counter(), "declared": {}, "imported": Counter(),
         "system_commands": Counter(), "env_vars": Counter(), "platform_hints": Counter()}
    for full, rel in walk(target):
        name = os.path.basename(full)
        text = _read_text(full)
        if text is None:
            continue
        lang = lang_of(full)
        if lang and lang != "any":
            d["runtimes"][lang] += 1
        if name in MANIFEST_NAMES:
            items = _parse_manifest(name, text)
            if items:
                d["declared"][rel] = sorted(set(items))[:80]
        for m in re.finditer(r"^\s*from\s+([A-Za-z_]\w*)", text, re.M):
            if lang == "py" and m.group(1) not in STDLIB_PY:
                d["imported"][m.group(1)] += 1
        for m in re.finditer(r"^\s*import\s+([^\n#]+)", text, re.M):
            for part in m.group(1).split(","):
                mod = part.strip().split(" ")[0].split(".")[0]
                if lang == "py" and re.match(r"^[A-Za-z_]\w*$", mod) and mod not in STDLIB_PY:
                    d["imported"][mod] += 1
        for m in re.finditer(r"""(?:require\s*\(\s*|from\s+)['"]([^'"]+)['"]""", text):
            mod = m.group(1)
            if mod.startswith((".", "/", "~")) or mod.startswith("node:"):
                continue
            base = "/".join(mod.split("/")[:2]) if mod.startswith("@") else mod.split("/")[0]
            if base in NODE_BUILTIN:
                continue
            if lang in ("js", "lua"):
                d["imported"][base] += 1
        for m in list(CMD_CALL.finditer(text)) + list(CMD_CALL_LIST.finditer(text)):
            first = re.split(r"[\s;|&>]+", m.group(1).strip())[0].strip("'\"")
            if first and first not in SHELL_WORDS and not first.startswith("$") and "=" not in first:
                d["system_commands"][first] += 1
        if lang == "sh":
            for line in text.splitlines():
                s2 = line.strip()
                if not s2 or s2.startswith("#"):
                    continue
                head = re.split(r"[\s;|&>]+", s2)[0].strip("'\"$(")
                if "=" in head or head in SHELL_WORDS:
                    continue
                if re.match(r"^[A-Za-z][\w.\-]*$", head):
                    d["system_commands"][head] += 1
        for rx in ENV_RES:
            for m in rx.finditer(text):
                d["env_vars"][m.group(1)] += 1
        for plat, rx in PLATFORM_RES.items():
            if rx.search(text):
                d["platform_hints"][plat] += 1
    return d


def print_dependencies(d):
    print("")
    print("=== 运行时依赖 ===")
    print("语言/运行时: " + (" | ".join("%s %d 文件" % (k, v) for k, v in d["runtimes"].most_common()) or "-"))
    if d["declared"]:
        print("清单声明:")
        for f, items in d["declared"].items():
            shown = ", ".join(items[:30]) + (" ..." if len(items) > 30 else "")
            print("  %s: %s" % (f, shown))
    else:
        print("清单声明: 未发现 requirements.txt / package.json 等依赖声明文件")
    if d["imported"]:
        print("源码引用(疑似第三方): " + ", ".join(
            "%s%s" % (k, "×%d" % v if v > 1 else "") for k, v in d["imported"].most_common(40)))
    if d["system_commands"]:
        print("调用的外部命令: " + ", ".join(
            "%s%s" % (k, "×%d" % v if v > 1 else "") for k, v in d["system_commands"].most_common(30)))
    if d["env_vars"]:
        print("环境变量: " + ", ".join(
            "%s%s" % (k, "×%d" % v if v > 1 else "") for k, v in d["env_vars"].most_common(30)))
    if d["platform_hints"]:
        print("平台线索: " + " | ".join("%s %d" % (k, v) for k, v in d["platform_hints"].most_common()))
    print("提示:以上为静态线索。运行前按实际环境核对版本要求、系统命令是否存在、需要哪些环境变量与凭据。")

def main():
    ap = argparse.ArgumentParser(description="code-risk-check 静态扫描(只读,不执行目标代码)")
    ap.add_argument("target")
    ap.add_argument("--json", dest="json_out", help="把结果写成 JSON")
    ap.add_argument("--min-severity", default="info",
                    choices=["high", "medium", "low", "info"], help="只显示不低于该强度的命中")
    ap.add_argument("--quiet", action="store_true", help="只打印汇总")
    args = ap.parse_args()

    if not os.path.exists(args.target):
        print("路径不存在: %s" % args.target, file=sys.stderr)
        return 2

    findings, scanned, skipped_bin, skipped_big = [], 0, 0, 0
    for full, rel in walk(args.target):
        lang = lang_of(full)
        if lang is None:
            continue
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        if size > MAX_FILE_BYTES:
            skipped_big += 1
            continue
        try:
            with open(full, "rb") as f:
                head = f.read(8192)
        except OSError:
            continue
        if is_binary(head):
            skipped_bin += 1
            continue
        scanned += 1
        findings.extend(scan_file(full, rel, lang))

    deps = collect_dependencies(args.target)

    threshold = SEV_ORDER[args.min_severity]
    kept = [f for f in findings if SEV_ORDER[f["severity"]] <= threshold]
    kept.sort(key=lambda f: (SEV_ORDER[f["severity"]], f["category"], f["file"], f["line"]))

    by_cat, by_sev = Counter(), Counter()
    for f in kept:
        by_cat[f["category"]] += 1
        by_sev[f["severity"]] += 1
    hints = [f["level_hint"] for f in kept if f["level_hint"]]
    level_max = max(hints, key=lambda x: int(x[1])) if hints else None

    print("目标: %s" % args.target)
    print("扫描: %d 个文本文件(跳过二进制 %d、超大 %d)" % (scanned, skipped_bin, skipped_big))
    print("命中: %d 条" % len(kept))
    if not args.quiet:
        print("-" * 72)
        for f in kept:
            hint = "  潜在 %s" % f["level_hint"] if f["level_hint"] else ""
            print("[%-6s] %-5s %s:%d%s" % (f["severity"], f["category"], f["file"], f["line"], hint))
            print("         %s" % f["snippet"])
            print("         → %s" % f["note"])
        print("")
        print("=== 副作用与残留(会写到哪、留下什么)===")
        side = [f for f in kept if f["category"] in SIDE_CATS]
        if side:
            for f in side:
                print("  %s:%d  [%s]  %s" % (f["file"], f["line"], f["category"], f["note"]))
        else:
            print("  未发现写盘 / 删除 / 持久化线索。注意:静态扫描看不到运行时动态拼接的路径,")
            print("  判'无副作用'前应做一次动态观察(见 references/dynamic.md)。")
    print("-" * 72)
    print("按维度: " + " | ".join("%s %d" % (k, v) for k, v in by_cat.most_common()) if by_cat else "按维度: -")
    print("按强度: " + " | ".join("%s %d" % (k, by_sev[k]) for k in ["high", "medium", "low", "info"] if by_sev[k]))
    print("潜在等级上界(需人工确认,不等于结论): %s" % (level_max or "-"))
    print("注意:命中是线索不是结论,字符串/注释/示例都会误报,请读上下文复核。")
    if not args.quiet:
        print_dependencies(deps)

    if args.json_out:
        doc = {
            "target": os.path.abspath(args.target),
            "generated_at": datetime.datetime.now().replace(microsecond=0).isoformat(),
            "scanned_files": scanned,
            "skipped": {"binary": skipped_bin, "too_large": skipped_big},
            "summary": {
                "by_category": dict(by_cat),
                "by_severity": dict(by_sev),
                "level_hint_max": level_max,
            },
            "dependencies": {
                "runtimes": dict(deps["runtimes"]),
                "declared": deps["declared"],
                "imported": dict(deps["imported"].most_common(60)),
                "system_commands": dict(deps["system_commands"].most_common(40)),
                "env_vars": dict(deps["env_vars"].most_common(40)),
                "platform_hints": dict(deps["platform_hints"]),
            },
            "side_effects": [f for f in kept if f["category"] in SIDE_CATS],
            "findings": kept,
        }
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        print("JSON 已写入: %s" % args.json_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
