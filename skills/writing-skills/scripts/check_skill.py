#!/usr/bin/env python3
"""技能自检器:查交叉引用、资源引用、frontmatter、边界声明。

**为什么需要它**:技能之间会互相引用(最常见的两种是写 `/skills/<名>/...` 的容器路径、
在正文里提「见 xxx 技能」),而技能一改名、文件一挪,这些引用就悄悄断了 —— 断的时候
不报错,只是模型按图索骥时找不到,或者读者以为那个技能叫这个名字。**这类问题靠人肉
grep 一定漏**,尤其是"技能引用自己"那种(改自己的名字时,眼睛正盯着别处)。

只依赖标准库,因为它要能在容器里直接跑(技能目录是只读挂载的,只能读、不能改)。

用法(路径相对技能目录;在容器里的实际位置见加载技能时给的资源清单):
    python3 scripts/check_skill.py              # 查全部技能
    python3 scripts/check_skill.py <技能目录>   # 查一个
    python3 scripts/check_skill.py <SKILL.md>   # 查单个文件
    python3 scripts/check_skill.py --graph      # 列技能间的引用关系

--graph 不给"图"、给**清单**:谁引用了谁(分"资源依赖"与"仅提及"两档,强度差很多)、
改某个技能要同步哪些地方,以及三类值得看一眼的情况 —— 互相引用(可能是在推诿)、
被很多技能引用的枢纽、与谁都不往来的孤岛。写新技能前看一眼,能少造一个重复的。
"""
from __future__ import annotations

import pathlib
import re
import sys

# PyYAML 是**可选**的:装了就能完整读 frontmatter(嵌套、列表都对),没装就只能读
# 平铺的 `键: 值`,而依赖字段恰恰是列表 —— 那时会明说读不了,不装作读懂了。
try:
    import yaml
except ImportError:                                          # noqa: SIM105
    yaml = None

# 技能正文里的路径引用:行内代码里的 scripts/xxx、references/xxx、assets/xxx。
# 只取到空白或占位符为止 —— 否则 `scripts/foo.py <目标路径>` 这种会被整句抓进来。
RES_REF = re.compile(r"`((?:scripts|references|assets)/[^\s`<*]+)")
# **用别的技能的资源**时的写法:`技能名/scripts/x.py` —— 相对,但带了域名,一眼看得出
# 是谁的。比"同一行里提到某个技能名"那种猜测可靠得多,也能直接拿去对文件在不在。
CROSS_REF = re.compile(r"`([a-z][a-z0-9-]*)/((?:scripts|references|assets)/[^\s`<*]+)")
# 写死的容器绝对路径:`/skills/<名>/...`。**这是要被禁掉的东西**(理由见 check_skill)。
# 带 `<`、`…`、`*` 的是示例占位写法,不算。
HARD_PATH = re.compile(r"/skills/([A-Za-z0-9_<>…*-]+)/([^\s`\"')\]）,。;:]+)")
# 反引号里的连字符词(候选技能名)
BACKTICK_WORD = re.compile(r"`([a-z][a-z0-9]*(?:-[a-z0-9]+)+)`")
# frontmatter 的 key: value(只在没有 PyYAML 的降级解析器里用)
FM_KEY = re.compile(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$")
# 边界声明:description 里出现这些词,说明作者交代了适用范围。
# 宁可收宽一点 —— 这一条是"提醒"不是"报错",漏报比误报更烦人。
BOUNDARY_HINTS = (
    "不适用", "不覆盖", "不在其中", "不提供", "不涉及", "不负责", "不支持", "不含",
    "只管", "只做", "只覆盖", "仅覆盖", "限于", "限定", "覆盖",
)
# 谁能加载这个技能 —— 和 agent/skills.py 的 parse_agents 是**同一套**。
AGENT_ROLES = ("main", "sub")
# 依赖项的规矩 —— 和 agent/skills.py 的 parse_deps 是**同一套**。
DEP_KINDS = ("skill", "pip")
DEP_FIELDS = set(DEP_KINDS) | {"reason", "fallback"}
# 依赖项要能**单独拎出来读懂**,不能靠"上文" —— 哪几项会出现是加载时才定的(缺席的
# 可选依赖根本不显示),所以「同上」很可能是一句没有上文的孤零零的话。
#
# **这条只在自检里卡,运行时(agent/skills.py 的 parse_deps)不管。** 是故意的:
# 这是行文质量,不是结构错误 —— 别人的技能写了「同上」,该照常能加载,不该整个报废。
# 自检是交出去之前的把关,可以严;运行时是"再差也得能跑"。
DEP_BACKREF = re.compile(r"^(同上|见上|如上|同前|同上所述|同\s*上|ditto|same as above)")
# 标准库模块名(3.10+ 才有),用来拦 `pip: json` 这种 —— 它永远报缺,却看着人畜无害。
# 基础镜像自带的包(pip / setuptools)这里查不到,只能靠 bodies 里那张表提醒。
_STDLIB = getattr(sys, "stdlib_module_names", None)
# 举例用的占位名,不当成真实引用 —— 按**主名**判,不看扩展名(foo.py / foo.md 都算)
PLACEHOLDERS = {"foo", "bar", "baz", "qux", "xxx", "yyy", "name"}
# 单字母文件名(x.py / y.md)也是写说明时常用的举例写法
SINGLE_LETTER = re.compile(r"^[a-z]\.\w+$")


def _is_placeholder(rel: str) -> bool:
    """这个是"举个例子"的文件名,还是真的引用?"""
    base = pathlib.Path(rel).name
    return (pathlib.Path(base).stem in PLACEHOLDERS or SINGLE_LETTER.match(base) is not None)
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


def _split_frontmatter(text: str) -> tuple[str, str, int]:
    """拆出 frontmatter,**返回原文**(解析交给 _load_meta)。

    返回 (frontmatter 原文, 正文, 正文起始行号)。没有 frontmatter 就返回空串。
    """
    if not text.startswith("---\n"):
        return "", text, 0
    end = text.find("\n---", 4)
    if end < 0:
        return "", text, 0
    body_start = text[: end + 4].count("\n") + 1
    return text[4:end], text[end + 4:], body_start


def _load_meta(raw: str) -> tuple[dict, list[str]]:
    """解析 frontmatter,返回 (字段表, 问题列表)。

    优先用 PyYAML —— 技能格式是标准 YAML,依赖字段又是列表,只有真解析器读得准。
    没装 PyYAML 时退到内置的平铺解析器,**并且明说它读不了什么** —— 降级可以,装作
    没降级不行:那会让人以为依赖字段检查过了。
    """
    if yaml is not None:
        try:
            meta = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            return {}, [f"frontmatter 不是合法的 YAML:{exc}"]
        if meta is None:
            return {}, []
        if not isinstance(meta, dict):
            return {}, ["frontmatter 要是一组 `键: 值`,现在解析出来不是"]
        return meta, []

    # ---- 降级:只有平铺的 `键: 值` ----
    meta: dict = {}
    nested = False
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = FM_KEY.match(line)
        if m:
            if not m.group(2).strip():
                # `requires:` 后面挂着列表 —— 读不了。**这个键干脆不放进去**:留个空串的话,
                # 校验会把它当成"值写错了"再报一次,平白多出两条假错误。读不懂就别装作读过。
                nested = True
                continue
            meta[m.group(1)] = m.group(2).strip()
        elif line.lstrip().startswith("-"):
            nested = True
        elif line[:1].isspace() and meta:
            # YAML 折行续行:接到上一个 key 后面
            last = next(reversed(meta))
            meta[last] = (meta[last] + " " + line.strip()).strip()
    note = []
    if nested:
        note.append(
            "没装 PyYAML,内置解析器读不了嵌套结构 —— **requires / optional 这两个字段"
            "这次没被检查**(它们都是列表)。确认依赖写对没,要么装一下 PyYAML,"
            "要么自己肉眼过一遍"
        )
    return meta, note


def check_agents(meta: dict, known: dict) -> list[str]:
    """查 agents 字段写得对不对。

    **必填,不给默认。**「没写」和「两边都能用」是两回事 —— 分析型技能会让加载它的
    agent 多吃几万 token 上下文,那必须是有意为之,不能靠"没写就放行"。
    """
    raw = meta.get("agents")
    if raw is None or raw == []:
        return ["frontmatter 缺 agents —— 给谁用?写 `agents: [main]` / `[sub]` / `[main, sub]`"]
    if not isinstance(raw, list):
        return ["agents 要是一个列表,如 `agents: [main, sub]`"]
    bad = [str(a) for a in raw if str(a).strip() not in AGENT_ROLES]
    if bad:
        return [f"agents 里有不认识的角色 {'、'.join(bad)} —— 只认 {'、'.join(AGENT_ROLES)}"]
    return []


def check_dep_agents(meta: dict, known: dict, field: str) -> list[str]:
    """**硬**依赖的类型对不对得上:本技能给谁用,被依赖的技能就得谁都能用。

    **只看 requires,不看 optional** —— 这条差别是实打实的:

    - `requires` 是说"没有它这活干不成"。可要是**能加载本技能的那一方压根加载不了它**,
      这个"硬"就是空头承诺 —— 一个 `[main]` 的技能硬依赖一个 `[sub]` 的技能,主 agent
      永远拿不到它,而这个错**当场不报**,跑到那一步才发现。
    - `optional` 本来就带着 `fallback`(缺了怎么办)。对加载它的那一方来说"角色不对、
      加载不了"**就是"缺了"的一种**,照着 fallback 走即可 —— 不需要额外拦。
      而且 `skill:` 依赖多半只是要**用对方的资源**(跑个脚本),那件事跟能不能加载
      对方的**正文**是两回事,不该混为一谈。
    """
    mine = {str(a).strip() for a in (meta.get("agents") or []) if isinstance(a, str)}
    if not mine:
        return []
    problems = []
    for item in (meta.get(field) or []):
        if not isinstance(item, dict) or not item.get("skill"):
            continue
        dep = str(item["skill"]).strip()
        theirs = known.get(dep)
        if theirs is None:
            continue                      # 技能不存在,check_deps 那边已经报过了
        missing = mine - set(theirs)
        if missing:
            who = lambda s: "、".join("主 agent" if a == "main" else "子 agent" for a in sorted(s))
            problems.append(
                f"{field} 里的 `skill: {dep}` 对不上类型:本技能给{who(mine)}用,"
                f"但 `{dep}` 只给{who(set(theirs))}用 —— 加载本技能的那一方会加载不了它。"
                f"要么把 `{dep}` 也开给{who(missing)},要么本技能别只给{who(mine)}用"
            )
    return problems


def check_deps(meta: dict, known: dict, field: str) -> list[str]:
    """查 requires / optional 写得对不对,外加引用的技能在不在。

    规矩和 agent/skills.py 的 parse_deps 一致(那边是运行时的把关,这边是交出去之前的)。
    **两边都要有**:这边能在装进 agent 之前就报出来,那边管的是"技能坏了也得让人看见"。
    """
    raw = meta.get(field)
    if raw is None or raw == []:
        return []
    if not isinstance(raw, list):
        return [f"{field} 要是一个列表,每项写成 `- pip: 包名` 这样"]

    problems: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            problems.append(f"{field} 里的每一项都要写成 `- pip: 包名` 这样的键值对")
            continue
        unknown = set(item) - DEP_FIELDS
        if unknown:
            problems.append(
                f"{field} 里有不认识的字段 {'、'.join(sorted(unknown))} —— "
                f"只认 {'、'.join(sorted(DEP_FIELDS))}"
            )
            continue
        kinds = [k for k in DEP_KINDS if item.get(k)]
        if len(kinds) != 1:
            problems.append(
                f"{field} 里每一项要**恰好**声明 skill / pip 中的一个,现在有 {len(kinds)} 个:{item}"
            )
            continue
        kind, name = kinds[0], str(item[kinds[0]]).strip()
        if not item.get("reason"):
            problems.append(
                f"{field} 里的 `{kind}: {name}` 没写 reason —— "
                f"不写清为什么需要它,读的人没法判断它能不能少"
            )
        if field == "optional" and not item.get("fallback"):
            problems.append(
                f"{field} 里的 `{kind}: {name}` 没写 fallback —— "
                f"**可选依赖的缺失代价必须写出来**,写不出一个能接受的下场,它就该是 requires"
            )
        for key in ("reason", "fallback"):
            val = str(item.get(key) or "").strip()
            if DEP_BACKREF.match(val):
                problems.append(
                    f"{field} 里的 `{kind}: {name}` 的 {key} 写成了「{val[:12]}」—— "
                    f"依赖是**可插拔、无序**的,而且哪些项会出现要到加载时才定(缺席的"
                    f"可选依赖不显示),所以「同上」很可能是指向一句根本不在场的话。"
                    f"每条都要能单独拎出来读懂"
                )
        if kind == "pip" and _STDLIB is not None and name.replace("-", "_") in _STDLIB:
            problems.append(
                f"{field} 里的 `pip: {name}` 是**标准库**,不用装、也不该声明 —— "
                f"宿主查的是 .pylibs 里有没有,标准库不在那儿,所以它会永远报缺"
            )
        if kind == "skill" and name not in known:
            problems.append(f"{field} 里的 `skill: {name}` 没有这个技能(名字写错了?改名了?)")
    return problems


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


def _hard_path_notes(where: str, line: str, skill_dir: pathlib.Path) -> list[str]:
    """这一行里有没有**写死的容器路径**(`/skills/<名>/...`)。

    这是要禁掉的东西。技能是要**移植**的,而 `/skills` 只是当前这个宿主的挂载点 ——
    换个宿主、换个挂载方式,写死的路径就全是错的。更阴的是它**不会报错**:加载技能时
    照样把正文给你,你照着跑,容器说找不到文件,你以为是文件没了,其实是那句路径只在
    这个项目里成立。

    正确写法是只写**相对路径**(`scripts/x.py`)—— 真实位置由加载技能时一并给出。
    因此这里不再做"路径还在不在"的检查了:**路径根本不出现,就没有过期一说。**
    """
    out = []
    for m in HARD_PATH.finditer(line):
        other, sub = m.group(1), m.group(2).rstrip(".,)")
        if any(ch in other or ch in sub for ch in "<>…*"):
            continue                      # 占位/通配是示例写法,不是真路径
        hint = (f"若这是 `{other}` 的资源,在 requires / optional 里声明 `skill: {other}`,"
                f"加载时会告诉你它在哪") if other != skill_dir.name else \
               "它自己的资源直接写相对路径就行"
        out.append(
            f"{where}: 写死了容器路径 `/skills/{other}/{sub}` —— 技能是可移植的,"
            f"`/skills` 只是当前宿主的挂载点,换个环境这句话就是错的。"
            f"改成相对路径 `{sub}`;{hint}"
        )
    return out


def check_skill(skill_dir: pathlib.Path, known: set[str]) -> list[str]:
    """检查一个技能目录,返回问题列表(空 = 没问题)。"""
    problems: list[str] = []
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return [f"{skill_dir.name}: 没有 SKILL.md"]

    text = skill_md.read_text(encoding="utf-8")
    raw_fm, body, body_start = _split_frontmatter(text)
    meta, fm_notes = _load_meta(raw_fm)
    problems.extend(fm_notes)
    name = str(meta.get("name") or "")
    desc = str(meta.get("description") or "")

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

    # ---- 谁用、依赖 ----
    problems.extend(check_agents(meta, known))
    problems.extend(check_deps(meta, known, "requires"))
    problems.extend(check_deps(meta, known, "optional"))
    problems.extend(check_dep_agents(meta, known, "requires"))
    declared = {
        str(d[k]).strip() for f in ("requires", "optional")
        for d in (meta.get(f) or []) if isinstance(d, dict) for k in DEP_KINDS if d.get(k)
    }
    used_other: set[str] = set()        # 正文/脚本里实际用到了谁的资源

    # ---- 正文 ----
    # 写死的路径**连代码块一起查** —— 别处的检查要跳过围栏(示例里全是示意路径),
    # 但这条不用:它的豁免靠"带占位符"(<名>、…、*),不靠"在不在围栏里"。而**围栏里
    # 恰恰是最容易写死路径的地方** —— 一行 `python3 /skills/...` 抄进正文时,谁都不会
    # 觉得它有问题。跳过围栏就正好把最该查的地方放过去了。
    for i, raw_line in enumerate(body.splitlines(), 1):
        problems.extend(_hard_path_notes(f"{body_start + i}", raw_line, skill_dir))

    lines = _strip_fences(body)
    for i, line in lines:
        n = body_start + i

        # ---- 用别的技能的资源:`技能名/scripts/x.py` ----
        for m in CROSS_REF.finditer(line):
            other, rel = m.group(1), m.group(2).rstrip(".,)")
            if other == skill_dir.name or other not in known:
                continue                      # 自己家的按下面那条查;不存在的技能名不当资源引用报
            if "*" in rel:
                continue
            if not (skill_dir.parent / other / rel).exists():
                problems.append(f"{n}: 提到 `{other}/{rel}`,但 `{other}` 里没有这个文件")
            else:
                used_other.add(other)

        # ---- 自己的资源:`scripts/x.py` 这类(相对本技能目录) ----
        for m in RES_REF.finditer(line):
            rel = m.group(1).rstrip(".,)")
            if "*" in rel or _is_placeholder(rel):
                continue
            if (skill_dir / rel).exists():
                continue
            # 没带技能名 = 说的是自己家的。别去猜"是不是别人的" —— 要引用别人的就写
            # 全名(上面那条),猜出来的东西没法校验,还容易把错误指到别人头上。
            problems.append(f"{n}: 正文提到 `{rel}`,但本技能目录里没有这个文件")

        # ---- 技能名引用:`某技能名` ----
        for m in BACKTICK_WORD.finditer(line):
            word = m.group(1)
            if word in known or word in (name, skill_dir.name):
                continue
            # 只在"首段像某个已知技能"时报,免得把 apt-get / try-except 全抓进来
            if word.split("-")[0] in {k.split("-")[0] for k in known}:
                problems.append(f"{n}: 提到 `{word}`,但没有这个技能(是不是改名了?)")

    # ---- 用了别人的资源,却没在 requires / optional 里声明 ----
    # 声明了才会在加载时告诉你对方的位置;不声明的话,正文里那个相对路径就没有着落
    # (而且对方改名、被卸载,你这边悄无声息)。
    for other in sorted(used_other - declared):
        problems.append(
            f"用到了 `{other}` 的资源,但没在 requires / optional 里声明 `skill: {other}` —— "
            f"声明了才拿得到它的实际位置,也才算把依赖摆到明面上"
        )

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

    # ---- 脚本里的写死路径 ----
    # md 那部分上面按行查过了(带围栏剔除),这里补非 md 的:脚本里那句
    # `print("见 /skills/<名>/…")` 藏在几百行代码中间,最容易被改名漏掉 —— 而且断了
    # 不报错、要等运行时才看得出来。脚本要引用自己的资源,一律用 `__file__` 推。
    for f in _scannable_files(skill_dir):
        if f.suffix.lower() == ".md":
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel_f = str(f.relative_to(skill_dir))
        for i, line in enumerate(text.splitlines(), 1):
            problems.extend(_hard_path_notes(f"{rel_f}:{i}", line, skill_dir))

    # ---- 附带脚本的语法 ----
    for py in sorted((skill_dir / "scripts").glob("*.py")) if (skill_dir / "scripts").is_dir() else []:
        try:
            compile(py.read_text(encoding="utf-8"), str(py), "exec")
        except SyntaxError as exc:
            problems.append(f"scripts/{py.name} 语法错误:第 {exc.lineno} 行 {exc.msg}")

    return [f"{skill_dir.name}: {p}" if not p.startswith(skill_dir.name) else p for p in problems]


def _refs_to_others(skill_dir: pathlib.Path, known: set[str]) -> dict[str, set[str]]:
    """这个技能引用了谁。分三档,因为强度差得远:

    - **声明**:frontmatter 的 requires / optional 里写了 `skill: X` —— 最硬的一档,
      是作者明说的依赖,卸载对方就该有人告诉用户。
    - **资源引用**:正文里"复用 X 的 `scripts/y.py`" —— 对方的文件被真的用着,它一改
      就坏。(写死绝对路径的旧写法也算一档,但那种写法本身就会被 `_hard_path_notes` 拦下。)
    - **文字提及**:正文里出现 `某技能名` —— 只是"细节见那边"的指路,对方改名会失效,
      但内容上并不耦合。
    """
    out: dict[str, set[str]] = {"声明": set(), "资源": set(), "提及": set()}
    # 声明在 frontmatter 里的依赖:那是**明说的**依赖,比任何从正文里猜出来的都硬
    raw_fm, _, _ = _split_frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
    meta, _ = _load_meta(raw_fm)
    for field in ("requires", "optional"):
        for d in (meta.get(field) or []):
            if isinstance(d, dict) and d.get("skill") and str(d["skill"]) in known:
                out["资源"].add(str(d["skill"]).strip())
                out["声明"].add(str(d["skill"]).strip())

    for f in _scannable_files(skill_dir):
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        is_md = f.suffix.lower() == ".md"
        lines = _strip_fences(text) if is_md else list(enumerate(text.splitlines(), 1))
        for _, content in lines:
            for m in HARD_PATH.finditer(content):
                if m.group(1) in known and m.group(1) != skill_dir.name:
                    out["资源"].add(m.group(1))       # 这是要禁的写法,但先如实记下来
            for m in CROSS_REF.finditer(content):
                if m.group(1) in known and m.group(1) != skill_dir.name:
                    out["资源"].add(m.group(1))       # `技能名/scripts/x.py` —— 正规写法
            if not is_md:
                continue      # 脚本里只认路径;再去猜"技能名提及"误报太多
            out["提及"].update(w for w in BACKTICK_WORD.findall(content)
                               if w in known and w != skill_dir.name)
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
        for dst in refs["声明"] | refs["资源"] | refs["提及"]:
            incoming.setdefault(dst, set()).add(src)

    def _all(n: str) -> set[str]:
        return graph.get(n, {}).get("声明", set()) | graph[n]["资源"] | graph[n]["提及"] \
            if n in graph else set()

    print("=== 引用关系(改了对方要改这里的,是「引用方」) ===")
    for name in sorted(graph):
        refs = graph[name]
        if not (refs["声明"] or refs["资源"] or refs["提及"]):
            print(f"  {name}: (不引用任何技能)")
            continue
        parts = []
        if refs["声明"]:
            parts.append("声明依赖 → " + ", ".join(sorted(refs["声明"])))
        undeclared = sorted(refs["资源"] - refs["声明"])
        if undeclared:
            parts.append("!用了但没声明 → " + ", ".join(undeclared))
        if refs["提及"]:
            parts.append("仅提及 → " + ", ".join(sorted(refs["提及"])))
        print(f"  {name}: " + " ; ".join(parts))

    print()
    print("=== 被引用(改这个技能时要同步这些地方) ===")
    for name in sorted(graph):
        back = incoming.get(name, set())
        if not back:
            continue
        by_dep = sorted(b for b in back if name in graph[b]["声明"])
        by_res = sorted(b for b in back if name in graph[b]["资源"] and name not in graph[b]["声明"])
        by_men = sorted(b for b in back if name in graph[b]["提及"] and name not in graph[b]["资源"])
        parts = []
        if by_dep:
            parts.append("声明依赖 ← " + ", ".join(by_dep))
        if by_res:
            parts.append("!用了但没声明 ← " + ", ".join(by_res))
        if by_men:
            parts.append("仅提及 ← " + ", ".join(by_men))
        print(f"  {name}: " + " ; ".join(parts))

    print()
    print("=== 值得看一眼的 ===")
    flagged = False
    for a in sorted(graph):
        for b in sorted(_all(a)):
            if a < b and a in _all(b):
                print(f"  ! {a} 与 {b} 互相引用 —— 检查是不是在互相推诿(都说是对方的事)")
                flagged = True
    for name in sorted(graph):
        back = incoming.get(name, set())
        if len(back) >= 3:
            print(f"  ! {name} 被 {len(back)} 个技能引用(枢纽)—— 改它影响面大,先看上面那份清单")
            flagged = True
    for name in sorted(graph):
        if not incoming.get(name) and not _all(name):
            print(f"  . {name} 与其它技能没有往来(孤岛)—— 独立也正常,确认一下不是漏了引用")
            flagged = True
    undeclared_any = sorted(n for n in graph if graph[n]["资源"] - graph[n]["声明"])
    if undeclared_any:
        print(f"  ! 用了别的技能的资源却没声明依赖:{', '.join(undeclared_any)}"
              f" —— 在 requires / optional 里补 `skill: …`")
        flagged = True
    if not flagged:
        print("  (没有需要特别留意的)")


def _known_skills(root: pathlib.Path) -> dict:
    """{技能名: 它的 agents}。

    是个 dict 但**当集合用也没问题**(`x in dict` 查的是键),所以老的交叉引用检查不用改;
    多带一份 agents 是为了查"依赖的类型对不对得上"。
    """
    out: dict = {}
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if not (d / "SKILL.md").is_file():
            continue
        try:
            meta, _ = _load_meta(_split_frontmatter(
                (d / "SKILL.md").read_text(encoding="utf-8"))[0])
            agents = meta.get("agents")
            out[d.name] = [str(a).strip() for a in agents] if isinstance(agents, list) else []
        except (OSError, UnicodeDecodeError):
            out[d.name] = []
    return out


def main() -> int:
    args = sys.argv[1:]
    root = _default_skills_dir()
    known = _known_skills(root)

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
