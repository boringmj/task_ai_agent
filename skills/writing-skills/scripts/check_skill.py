#!/usr/bin/env python3
"""技能自检器:查交叉引用、资源引用、frontmatter、边界声明。

**为什么需要它**:技能之间会互相引用(最常见的两种是写 `/skills/<名>/...` 的容器路径、
在正文里提「见 xxx 技能」),而技能一改名、文件一挪,这些引用就悄悄断了 —— 断的时候
不报错,只是模型按图索骥时找不到,或者读者以为那个技能叫这个名字。**这类问题靠人肉
grep 一定漏**,尤其是"技能引用自己"那种(改自己的名字时,眼睛正盯着别处)。

只依赖标准库,因为它要能在容器里直接跑(技能目录是只读挂载的,只能读、不能改)。

用法:
    python3 /skills/writing-skills/scripts/check_skill.py              # 查全部技能
    python3 /skills/writing-skills/scripts/check_skill.py <技能目录>   # 查一个
    python3 /skills/writing-skills/scripts/check_skill.py <SKILL.md>   # 查单个文件
    python3 /skills/writing-skills/scripts/check_skill.py --graph      # 列技能间的引用关系

--graph 不给"图"、给**清单**:谁引用了谁(分"资源依赖"与"仅提及"两档,强度差很多)、
改某个技能要同步哪些地方,以及三类值得看一眼的情况 —— 互相引用(可能是在推诿)、
被很多技能引用的枢纽、与谁都不往来的孤岛。写新技能前看一眼,能少造一个重复的。
"""
from __future__ import annotations

import pathlib
import re
import sys

# 技能正文里的路径引用:行内代码里的 scripts/xxx、references/xxx、assets/xxx。
# 只取到空白或占位符为止 —— 否则 `scripts/foo.py <目标路径>` 这种会被整句抓进来。
RES_REF = re.compile(r"`((?:scripts|references|assets)/[^\s`<*]+)")
# 容器里的绝对路径引用:/skills/<名>/<路径>
CONTAINER_REF = re.compile(r"/skills/([A-Za-z0-9_-]+)/([^\s`\"')\]）,。;:]+)")
# 反引号里的连字符词(候选技能名)
BACKTICK_WORD = re.compile(r"`([a-z][a-z0-9]*(?:-[a-z0-9]+)+)`")
# frontmatter 的 key: value
FM_KEY = re.compile(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$")
# 边界声明:description 里出现这些词,说明作者交代了适用范围。
# 宁可收宽一点 —— 这一条是"提醒"不是"报错",漏报比误报更烦人。
BOUNDARY_HINTS = (
    "不适用", "不覆盖", "不在其中", "不提供", "不涉及", "不负责", "不支持", "不含",
    "只管", "只做", "只覆盖", "仅覆盖", "限于", "限定", "覆盖",
)
# 举例用的占位名,不当成真实引用
PLACEHOLDERS = {"foo", "bar", "baz", "xxx", "yyy", "name", "foo.py", "bar.py"}
# 单字母文件名(x.py / y.md)也是写说明时常用的举例写法
SINGLE_LETTER = re.compile(r"^[a-z]\.\w+$")
# 技能里可能写着路径引用的文件类型 —— 正文、参考资料、脚本**都算**。
# 脚本尤其不能漏:改名的时候 md 里的引用肉眼还能扫到,而脚本里那行 print("见 /skills/<名>/…")
# 藏在几百行代码中间,同样会断、而且要到运行时才报出来。
SCAN_EXT = {".md", ".py", ".sh", ".js", ".lua", ".yaml", ".yml"}


def _scannable_files(skill_dir: pathlib.Path) -> list[pathlib.Path]:
    """技能里所有可能写着路径引用的文件。"""
    return [p for p in sorted(skill_dir.rglob("*"))
            if p.is_file() and p.suffix.lower() in SCAN_EXT and "__pycache__" not in p.parts]

MAX_BODY_LINES = 200       # 正文超过这个行数就提示(SKILL.md 太长会把上下文吃掉)


def _default_skills_dir() -> pathlib.Path:
    """默认的 skills/ 目录:从本脚本位置往上推两级(scripts/ -> 技能目录 -> skills/)。

    这样容器里(/skills/...)和本地仓库里都能直接用,不必写死任何一边的路径。
    """
    return pathlib.Path(__file__).resolve().parent.parent.parent


def _split_frontmatter(text: str) -> tuple[dict, str, int]:
    """拆出 frontmatter,返回 (字段表, 正文, 正文起始行号)。没有 frontmatter 就返回空表。"""
    if not text.startswith("---\n"):
        return {}, text, 0
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text, 0
    meta: dict[str, str] = {}
    for line in text[4:end].splitlines():
        m = FM_KEY.match(line)
        if m:
            meta[m.group(1)] = m.group(2).strip()
        elif line[:1].isspace() and meta:
            # YAML 折行续行:接到上一个 key 后面
            last = next(reversed(meta))
            meta[last] = (meta[last] + " " + line.strip()).strip()
    body_start = text[: end + 4].count("\n") + 1
    return meta, text[end + 4:], body_start


def _strip_fences(body: str) -> list[tuple[int, str]]:
    """去掉围栏代码块里的行,返回 [(通篇行号, 内容)]。

    示例块里会写着 `/skills/<技能名>/...` 这种**示意**路径,不排除的话全是误报。
    """
    out, in_fence, marker = [], False, ""
    for i, line in enumerate(body.splitlines(), 1):
        s = line.strip()
        if s.startswith("```") or s.startswith("~~~"):
            mark = s[:3] if s.startswith("```") else "~~~"
            if not in_fence:
                in_fence, marker = True, mark
            elif s.startswith(marker):
                in_fence = False
            continue
        if not in_fence:
            out.append((i, line))
    return out


# ======================= markdown 结构检查 =======================
# 这一段和 agent/markdown.py 里的规则是**同一套**,只是那份跑在宿主、这份跑在容器。
# 为什么非要有两份:技能目录对宿主是"工作区之外",agent 的文件工具够不到它(那是硬性
# 安全边界,不该为这个松掉),所以只能让容器里这个脚本代劳。**改一边记得改另一边。**
LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s")
HEADING_RE = re.compile(r"^(#{1,6})\s")
FENCE_RE = re.compile(r"^(\s*)(```+|~~~+)\s*(\S*)")


def _emphasis_only(s: str) -> bool:
    """整行只由一个强调元素构成吗(`**粗体**` / `*斜体*`)。

    MD036 判的是"拿强调当标题"(`**第一步**` 顶替 `### 第一步`),但只有它**自成一段**时
    才算数,所以调用方还得确认这行前后都空着。
    """
    for mark in ("**", "__", "*", "_"):
        if len(s) > 2 * len(mark) and s.startswith(mark) and s.endswith(mark):
            inner = s[len(mark):-len(mark)]
            if inner and mark[0] not in inner:
                return True
    return False


def check_markdown(lines: list[str], offset: int) -> list[str]:
    """查 markdown 结构问题。offset 是正文首行在全文件里的行号(用于报行号)。"""
    out: list[str] = []
    in_fence, fence_marker = False, ""
    prev_blank, prev_kind = True, ""      # 文件开头视为空行
    h1_seen: list[int] = []
    markers: set[str] = set()

    for i, raw in enumerate(lines):
        n = offset + i + 1
        fence = FENCE_RE.match(raw)
        if fence:
            marker = fence.group(2)                 # 可能是 ``` 也可能更长
            if not in_fence:
                if not prev_blank:
                    out.append(f"{n}: MD031 代码块上方要空一行")
                if not fence.group(3):
                    out.append(f"{n}: MD040 代码块要标明语言(```python / ```bash / ```text)")
                in_fence, fence_marker = True, marker
            elif (raw.strip().startswith(fence_marker[0])
                  and len(raw.strip()) >= len(fence_marker)
                  and set(raw.strip()) == {fence_marker[0]}):
                # 闭合围栏要**不短于**开启的那个(CommonMark 的规矩)。这条很关键:
                # 要示范"本身含代码块"的 markdown 时,外层得用更长的围栏,否则内层会
                # 把外层提前闭合,后面全乱。
                in_fence = False
                if i + 1 < len(lines) and lines[i + 1].strip():
                    out.append(f"{n}: MD031 代码块下方要空一行")
            prev_blank, prev_kind = False, "fence"
            continue

        if in_fence:
            continue                                # 代码块内部不管

        blank = not raw.strip()
        head = HEADING_RE.match(raw)
        item = LIST_RE.match(raw) and not raw.lstrip().startswith(("|", ">"))

        if head:
            if len(head.group(1)) == 1:
                h1_seen.append(n)
            if not prev_blank:
                out.append(f"{n}: MD022 标题上方要空一行")
            if i + 1 < len(lines) and lines[i + 1].strip():
                out.append(f"{n}: MD022 标题下方要空一行(下一行不能紧跟内容)")
        elif item:
            if not prev_blank and prev_kind != "list":
                out.append(f"{n}: MD032 列表上方要空一行")
            m_u = re.match(r"^\s*([-*+])", raw)     # MD004 只管无序列表的符号
            if m_u:
                markers.add(m_u.group(1))
            if i + 1 < len(lines) and lines[i + 1].strip() and not LIST_RE.match(lines[i + 1]):
                nxt = lines[i + 1]
                if not nxt.startswith((" ", "\t")) and not HEADING_RE.match(nxt):
                    out.append(f"{n}: MD032 列表下方要空一行")

        # MD036:整行加粗/斜体、前后都空着 = 自成一段,是拿强调当标题用。
        # 紧跟正文的 `**重点**:` 只是段内强调,不报。
        if (not blank and not head and not item and prev_blank
                and not raw[:1].isspace()
                and (i + 1 >= len(lines) or not lines[i + 1].strip())
                and _emphasis_only(raw.strip())):
            out.append(f"{n}: MD036 别用加粗当标题(标题请用 #,整行加粗只会被当成强调)")

        if blank:
            if prev_blank and i > 0:
                out.append(f"{n}: MD012 连续空行")
        elif raw != raw.rstrip():
            out.append(f"{n}: MD009 行尾有多余空格")
        # MD013(行太长)刻意不查 —— 和 agent/markdown.py 口径一致,理由见那边。

        if item:
            kind = "list"
        elif prev_kind == "list" and raw[:1].isspace():
            kind = "list"                # 列表项的缩进续行仍属于这个列表
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


def check_markdown_file(path: pathlib.Path) -> list[str]:
    """查一个 .md 文件的结构(frontmatter 归 check_skill 自己管,这里只查正文)。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    _, _, body_start = _split_frontmatter(text)
    lines = text.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    issues = check_markdown(lines[body_start:] if body_start else lines, body_start)
    if text and not text.endswith("\n"):
        issues.append("MD047 文件结尾要有换行")
    elif text.endswith("\n\n"):
        issues.append("MD047 文件结尾只能有一个换行")
    return issues


def check_skill(skill_dir: pathlib.Path, known: set[str]) -> list[str]:
    """检查一个技能目录,返回问题列表(空 = 没问题)。"""
    problems: list[str] = []
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return [f"{skill_dir.name}: 没有 SKILL.md"]

    text = skill_md.read_text(encoding="utf-8")
    meta, body, body_start = _split_frontmatter(text)
    name = meta.get("name", "")
    desc = meta.get("description", "")

    # ---- frontmatter ----
    if not name:
        problems.append("frontmatter 缺 name")
    elif name != skill_dir.name:
        problems.append(f"frontmatter 的 name({name})与目录名({skill_dir.name})不一致")
    if not desc:
        problems.append("frontmatter 缺 description(清单里只显示它,模型靠它决定要不要翻开)")
    elif not any(h in desc for h in BOUNDARY_HINTS):
        # 不是错误,是提醒:名字宽、能力窄的技能最需要这一句
        problems.append(
            "description 里没看到范围声明 —— 若这个技能有『处理不了的输入』,"
            "建议写明(如「不适用于…」「只覆盖…」),否则模型会拿它硬答"
        )

    lines = _strip_fences(body)
    for i, line in lines:
        n = body_start + i

        # ---- 容器路径引用:/skills/<名>/<路径> ----
        for m in CONTAINER_REF.finditer(line):
            other, sub = m.group(1), m.group(2).rstrip(".,)")
            if "…" in sub or "*" in sub:        # 省略号和通配符是示意写法,不是具体文件
                continue
            if other not in known:
                problems.append(f"{n}: /skills/{other}/… 里的技能 `{other}` 不存在")
            elif not (skill_dir.parent / other / sub).exists():
                problems.append(f"{n}: /skills/{other}/{sub} 不存在(改名或挪文件后没同步?)")

        # ---- 资源引用:`scripts/x.py` 这类 ----
        # 同一行里提到别的技能名时,那个 `scripts/xxx` 多半是**人家的**资源
        # (如"结构分析复用 X 的 `scripts/scan_repo.py`"),不该拿本技能目录去对。
        mentions_other = any(w != skill_dir.name for w in BACKTICK_WORD.findall(line) if w in known)
        for m in RES_REF.finditer(line):
            rel = m.group(1).rstrip(".,)")
            base = pathlib.Path(rel).name
            if ("*" in rel or mentions_other or base in PLACEHOLDERS
                    or SINGLE_LETTER.match(base)):
                continue
            if not (skill_dir / rel).exists():
                problems.append(f"{n}: 正文提到 `{rel}`,但技能目录里没有这个文件")

        # ---- 技能名引用:`某技能名` ----
        for m in BACKTICK_WORD.finditer(line):
            word = m.group(1)
            if word in known or word in (name, skill_dir.name):
                continue
            # 只在"首段像某个已知技能"时报,免得把 apt-get / try-except 全抓进来
            if word.split("-")[0] in {k.split("-")[0] for k in known}:
                problems.append(f"{n}: 提到 `{word}`,但没有这个技能(是不是改名了?)")

    # ---- 正文长度 ----
    n_body = len(body.splitlines())
    if n_body > MAX_BODY_LINES:
        problems.append(
            f"正文 {n_body} 行(超过 {MAX_BODY_LINES})—— 技能正文是按需加载的,"
            f"太长会把上下文吃掉;大段参考资料放 references/ 里按需读"
        )

    # ---- markdown 结构(技能里的**所有** .md,不只 SKILL.md —— references 用户也会打开)----
    for md_file in sorted(skill_dir.rglob("*.md")):
        if "__pycache__" in md_file.parts:
            continue
        for issue in check_markdown_file(md_file):
            problems.append(f"{md_file.relative_to(skill_dir)} {issue}")

    # ---- 脚本里的容器路径引用 ----
    # md 那部分上面按行查过了(带围栏剔除),这里补非 md 的:脚本里那句
    # `print("见 /skills/<名>/…")` 最容易被改名漏掉 —— 它藏在几百行代码中间,
    # 而且断了不报错、要等运行时才看得出来。
    for f in _scannable_files(skill_dir):
        if f.suffix.lower() == ".md":
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel_f = f.relative_to(skill_dir)
        for m in CONTAINER_REF.finditer(text):
            other, sub = m.group(1), m.group(2).rstrip(".,)")
            if "…" in sub or "*" in sub:
                continue
            if other not in known:
                problems.append(f"{rel_f}: 引用的技能 `{other}` 不存在")
            elif not (skill_dir.parent / other / sub).exists():
                problems.append(f"{rel_f}: /skills/{other}/{sub} 不存在(改名或挪文件后没同步?)")

    # ---- 附带脚本的语法 ----
    for py in sorted((skill_dir / "scripts").glob("*.py")) if (skill_dir / "scripts").is_dir() else []:
        try:
            compile(py.read_text(encoding="utf-8"), str(py), "exec")
        except SyntaxError as exc:
            problems.append(f"scripts/{py.name} 语法错误:第 {exc.lineno} 行 {exc.msg}")

    return [f"{skill_dir.name}: {p}" if not p.startswith(skill_dir.name) else p for p in problems]


def _refs_to_others(skill_dir: pathlib.Path, known: set[str]) -> dict[str, set[str]]:
    """这个技能引用了谁。分两档,因为强度差得远:

    - **资源引用**:`/skills/<名>/<路径>` 或"复用 X 的 `scripts/y.py`" —— 对方的文件被
      真的用着,它一改/一挪就坏。这是**依赖**。
    - **文字提及**:正文里出现 `某技能名` —— 只是"细节见那边"的指路,对方改名会失效,
      但内容上并不耦合。
    """
    out: dict[str, set[str]] = {"资源": set(), "提及": set()}
    for f in _scannable_files(skill_dir):
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        is_md = f.suffix.lower() == ".md"
        lines = _strip_fences(text) if is_md else list(enumerate(text.splitlines(), 1))
        for _, content in lines:
            for m in CONTAINER_REF.finditer(content):
                if m.group(1) in known and m.group(1) != skill_dir.name:
                    out["资源"].add(m.group(1))
            if not is_md:
                continue      # 脚本里只认容器路径;再去猜"技能名提及"误报太多
            # "复用 X 的 `scripts/y.py`" 这种:同一行的资源引用算在 X 头上
            words = [w for w in BACKTICK_WORD.findall(content) if w in known and w != skill_dir.name]
            if words and RES_REF.search(content):
                out["资源"].update(words)
            else:
                out["提及"].update(words)
    return out


def print_graph(root: pathlib.Path, known: set[str]) -> None:
    """列"谁引用谁"。不画图 —— 给 agent 读的东西,清单比图有用。

    重点标三种情况:
    - **双向往来**:两个技能互相引用 —— 要么是真配合,要么是互相推诿(都说是对方的事),
      后者会让模型两边都不管,值得人看一眼
    - **枢纽**:被很多技能引用的 —— 改它影响面大
    - **孤岛**:没被任何技能提到的 —— 要么独立,要么该被别处引用却漏了
    """
    graph = {d.name: _refs_to_others(d, known) for d in sorted(root.iterdir())
             if d.is_dir() and (d / "SKILL.md").exists()}
    incoming: dict[str, set[str]] = {k: set() for k in graph}
    for src, refs in graph.items():
        for dst in refs["资源"] | refs["提及"]:
            incoming.setdefault(dst, set()).add(src)

    print("=== 引用关系(改了对方要改这里的,是「引用方」) ===")
    for name in sorted(graph):
        refs = graph[name]
        if not (refs["资源"] or refs["提及"]):
            print(f"  {name}: (不引用任何技能)")
            continue
        parts = []
        if refs["资源"]:
            parts.append("资源依赖 → " + ", ".join(sorted(refs["资源"])))
        if refs["提及"]:
            parts.append("仅提及 → " + ", ".join(sorted(refs["提及"])))
        print(f"  {name}: " + " ; ".join(parts))

    print()
    print("=== 被引用(改这个技能时要同步这些地方) ===")
    for name in sorted(graph):
        back = incoming.get(name, set())
        if not back:
            continue
        by_res = sorted(b for b in back if name in graph[b]["资源"])
        by_men = sorted(b for b in back if name in graph[b]["提及"] and name not in graph[b]["资源"])
        parts = []
        if by_res:
            parts.append("资源依赖 ← " + ", ".join(by_res))
        if by_men:
            parts.append("仅提及 ← " + ", ".join(by_men))
        print(f"  {name}: " + " ; ".join(parts))

    print()
    print("=== 值得看一眼的 ===")
    flagged = False
    for a in sorted(graph):
        for b in sorted(graph[a]["资源"] | graph[a]["提及"]):
            if a < b and a in (graph.get(b, {}).get("资源", set()) | graph.get(b, {}).get("提及", set())):
                print(f"  ! {a} 与 {b} 互相引用 —— 检查是不是在互相推诿(都说是对方的事)")
                flagged = True
    for name in sorted(graph):
        back = incoming.get(name, set())
        if len(back) >= 3:
            print(f"  ! {name} 被 {len(back)} 个技能引用(枢纽)—— 改它影响面大,先看上面那份清单")
            flagged = True
    for name in sorted(graph):
        if not incoming.get(name) and not (graph[name]["资源"] or graph[name]["提及"]):
            print(f"  . {name} 与其它技能没有往来(孤岛)—— 独立也正常,确认一下不是漏了引用")
            flagged = True
    if not flagged:
        print("  (没有需要特别留意的)")


def main() -> int:
    args = sys.argv[1:]
    root = _default_skills_dir()
    known = {p.name for p in root.iterdir() if p.is_dir()} if root.is_dir() else set()

    if args and args[0] == "--graph":
        print_graph(root, known)
        return 0

    if not args:
        targets = sorted(p for p in root.iterdir() if p.is_dir() and (p / "SKILL.md").exists())
        if not targets:
            print(f"在 {root} 下没找到技能")
            return 1
    else:
        targets = []
        for a in args:
            p = pathlib.Path(a).resolve()
            targets.append(p.parent if p.is_file() else p)

    print(f"检查 {len(targets)} 个技能(已知技能名 {len(known)} 个)")
    print()
    total = 0
    for d in targets:
        problems = check_skill(d, known)
        if problems:
            total += len(problems)
            for p in problems:
                print(f"  ✗ {p}")
        else:
            print(f"  ✓ {d.name}")
    print()
    print(f"共 {total} 处问题" if total else "全部通过")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
