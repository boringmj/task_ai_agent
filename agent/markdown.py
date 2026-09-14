"""markdown 结构检查:和 markdownlint 对齐的一套轻量规则,给写文件时自动把关。

为什么要有它:agent 生成的 markdown(报告、技能、笔记)经常格式不规范,用户一打开
就是满屏 lint 警告 —— 很影响观感,也显得不专业。与其事后让用户一条条指出来,不如
**写的时候就查**。所以这个模块有两个消费方(见 tools/markdown.py 与 tools/fs.py):
写文件时自动检查并附上问题;也可以用 `check_markdown` 工具单独查一个文件。

只覆盖 markdownlint 默认规则里最常踩的那些(MD 编号沿用官方编号,方便对照):

    MD041 首行要是 H1 / MD025 只能有一个 H1
    MD022 标题上下要有空行 / MD031 代码块上下要有空行 / MD032 列表上下要有空行
    MD040 代码块要标语言 / MD004 无序列表符号要统一
    MD012 不能有连续空行 / MD009 行尾不能有空格 / MD047 文件要以单个换行结尾
    MD036 别拿加粗/斜体当标题(整行加粗自成一段)

**MD032 和 MD036 是模型写 markdown 的重灾区**:前者是老忘了跟正文之间空行,
后者是习惯用 `**第一步**` 顶替 `### 第一步`。这两条在检查里都按最严的口径判。
(反过来,MD013 行太长**刻意关掉了** —— 理由见下面 MAX_LINE 那一段。)

**这里有一份副本要保持同步**:技能目录(`skills/`)在工作区之外,agent 的文件工具够不到
它(那是硬性安全边界),所以那边 `skills/writing-skills/scripts/check_skill.py` 里放着
同一套规则 —— 它跑在容器里,能读到只读挂载的技能目录。**改这里的规则时别忘了改那份。**
"""
import re
import pathlib

# MD013(行太长)曾经在这里按 80 字符卡,而且故意卡得比 markdownlint 还严。关掉的理由:
#   1. 它是所有规则里最容易**反复触发**的一条 —— 模型写个长句被拦、折了行又踩到别的,
#      来回几轮上下文就烧没了,而换回来的只是一个用户根本看不到的警告;
#   2. 用户的编辑器里没开这条,修不修他其实看不出来;
#   3. 行长度本来就是主观规则(markdownlint 自己也把它归在"可关"的一类)。
# 其余规则都留着 —— 空行/围栏/标题层级那些,才是编辑器真会报、用户真会烦的。
# MAX_LINE = 80
LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s")
HEADING_RE = re.compile(r"^(#{1,6})\s")
FENCE_RE = re.compile(r"^(\s*)(```+|~~~+)\s*(\S*)")


def _emphasis_only(s: str) -> bool:
    """整行只由一个强调元素构成吗(`**粗体**` / `*斜体*` / `__粗体__` / `_斜体_`)。

    MD036 判的是"拿强调当标题"这种写法(`**第一步**` 顶替 `### 第一步`)——
    但只有它**自成一段**时才算数,所以调用方还得确认这行前后都空着。
    """
    for mark in ("**", "__", "*", "_"):
        if len(s) > 2 * len(mark) and s.startswith(mark) and s.endswith(mark):
            inner = s[len(mark):-len(mark)]
            if inner and mark[0] not in inner:
                return True
    return False


def check_frontmatter(text: str, dir_name: str, *, is_skill: bool) -> tuple[list[str], int]:
    """查开头的 YAML frontmatter,返回 (问题列表, 正文起始行号)。

    `is_skill` 只对技能(文件名 SKILL.md)才要求 name/description —— 普通 markdown
    带 frontmatter 是常见的(有的静态站点生成器就用),不该要求它有这两个字段。
    """
    issues = []
    m = re.match(r"^---\n(.*?)\n---\n?", text, re.S)
    if not m:
        return (["缺少 frontmatter(文件要以 --- 开头,再用 --- 收尾)"] if is_skill else []), 0
    meta = m.group(1)
    if is_skill:
        for key in ("name", "description"):
            if not re.search(rf"^{key}\s*:\s*\S", meta, re.M):
                issues.append(f"frontmatter 缺 {key}")
        nm = re.search(r"^name\s*:\s*(\S+)", meta, re.M)
        if nm and nm.group(1) != dir_name:
            issues.append(f"name({nm.group(1)}) 与目录名({dir_name})不一致")
        if not re.search(r"^description\s*:\s*\S", meta, re.M):
            issues.append("description 是空的 —— 清单里只显示它,决定了模型何时会用到这个技能")
    return issues, len(m.group(0).splitlines())


def check_markdown(lines: list[str], offset: int) -> list[str]:
    """查 markdown 结构问题。offset 是正文第一行在全文件里的行号(用于报行号)。"""
    out = []
    in_fence = False
    fence_marker = ""
    prev_blank = True          # 文件开头视为空行
    prev_kind = ""
    h1_seen = []
    markers = set()

    for i, raw in enumerate(lines):
        n = offset + i + 1

        fence = FENCE_RE.match(raw)
        if fence:
            marker = fence.group(2)                     # 可能是 ``` 也可能是 ```` 甚至更长
            if not in_fence:
                if not prev_blank:
                    out.append(f"{n}: MD031 代码块上方要空一行")
                if not fence.group(3):
                    out.append(f"{n}: MD040 代码块要标明语言(```python / ```bash / ```text)")
                in_fence, fence_marker = True, marker
            elif raw.strip() == fence_marker or (
                    raw.strip().startswith(fence_marker[0])
                    and len(raw.strip()) >= len(fence_marker)
                    and set(raw.strip()) == {fence_marker[0]}):
                # 闭合围栏要**不短于**开启的那个(CommonMark 的规矩)。
                # 这条很关键:要示范"本身含代码块"的 markdown 时,外层得用更长的围栏
                # (外层 ```` + 内层 ```);否则内层会把外层提前闭合,后面全乱。
                in_fence = False
                if i + 1 < len(lines) and lines[i + 1].strip():
                    out.append(f"{n}: MD031 代码块下方要空一行")
            prev_blank = False
            prev_kind = "fence"
            continue

        if in_fence:
            continue                                    # 代码块内部不管

        blank = not raw.strip()
        head = HEADING_RE.match(raw)
        item = LIST_RE.match(raw) and not raw.lstrip().startswith(("|", ">"))

        if head:
            h1_seen.append(n) if len(head.group(1)) == 1 else None
            if not prev_blank:
                out.append(f"{n}: MD022 标题上方要空一行")
            if i + 1 < len(lines) and lines[i + 1].strip():
                out.append(f"{n}: MD022 标题下方要空一行(下一行不能紧跟内容)")
        elif item:
            if not prev_blank and prev_kind != "list":
                out.append(f"{n}: MD032 列表上方要空一行")
            # MD004 只管**无序**列表的符号;有序列表(1. 2.)不参与
            m_u = re.match(r"^\s*([-*+])", raw)
            if m_u:
                markers.add(m_u.group(1))
            if i + 1 < len(lines) and lines[i + 1].strip() and not LIST_RE.match(lines[i + 1]):
                nxt = lines[i + 1]
                if not nxt.startswith((" ", "\t")) and not HEADING_RE.match(nxt):
                    out.append(f"{n}: MD032 列表下方要空一行")

        # MD036:整行加粗/斜体、前后都空着 = 自成一段。这是拿强调当标题用(老毛病),
        # 必须前后都是空行才算 —— 紧跟正文的 `**重点**:` 只是段内强调,不该报。
        if (not blank and not head and not item and prev_blank
                and not raw[:1].isspace()
                and (i + 1 >= len(lines) or not lines[i + 1].strip())
                and _emphasis_only(raw.strip())):
            out.append(f"{n}: MD036 别用加粗当标题(标题请用 #,整行加粗只会被当成强调)")

        if blank:
            if prev_blank and i > 0:
                out.append(f"{n}: MD012 连续空行")
        else:
            if raw != raw.rstrip():
                out.append(f"{n}: MD009 行尾有多余空格")
            # MD013 在这里卡行宽 —— **已关掉**(理由见文件开头 MAX_LINE 那段)。
            # 要恢复就把下面两行的注释去掉。
            # if len(raw) > MAX_LINE and " " in raw.strip():
            #     out.append(f"{n}: MD013 行太长({len(raw)},上限 {MAX_LINE})")

        # 列表项的**续行**(缩进着写的那种)仍属于这个列表 —— 不能把它当成普通段落,
        # 否则下一项会被误判成"列表上方没空行"。
        if item:
            kind = "list"
        elif prev_kind == "list" and raw[:1].isspace():
            kind = "list"
        else:
            kind = "text"
        prev_blank, prev_kind = blank, kind

    if not in_fence:
        first = next((k for k, ln in enumerate(lines) if ln.strip()), None)
        if first is None:
            out.append("MD041 文件是空的")
        elif not HEADING_RE.match(lines[first]):
            out.append(f"{offset + first + 1}: MD041 正文第一行要是一级标题(如 # 标题)")
    if len(h1_seen) > 1:
        out.append(f"MD025 有多个一级标题(第 {', '.join(map(str, h1_seen))} 行)—— 一份文档只该有一个")
    if len(markers) > 1:
        out.append(f"MD004 无序列表符号不统一:{' '.join(sorted(markers))}")
    return out


def lint(text: str, *, filename: str = "", dir_name: str = "") -> list[str]:
    """检查一段 markdown,返回问题列表(空列表 = 没问题)。每条形如 `12: MD022 …`。

    filename 只用于判断"是不是技能的 SKILL.md"(决定要不要要求 frontmatter);
    dir_name 用于校验技能 name 与目录名是否一致。
    """
    is_skill = filename == "SKILL.md"
    issues, body_start = check_frontmatter(text, dir_name, is_skill=is_skill)
    lines = text.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if body_start:
        issues += check_markdown(lines[body_start:], body_start)
    else:
        issues += check_markdown(lines, 0)
    if text and not text.endswith("\n"):
        issues.append("MD047 文件结尾要有换行")
    elif text.endswith("\n\n"):
        issues.append("MD047 文件结尾只能有一个换行")
    return issues


def lint_file(path) -> list[str]:
    """检查一个 .md 文件。读不了就返回空(不该因为读不了文件而挡住写操作)。"""
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return lint(text, filename=pathlib.Path(path).name,
                dir_name=pathlib.Path(path).resolve().parent.name)
