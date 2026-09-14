"""技能(skill):按需加载的说明书,给模型处理复杂事情的能力。

一个技能 = 一个目录 + 一份 `SKILL.md`:

    skills/<技能名>/
        SKILL.md            必需。YAML frontmatter(name/description/依赖)+ 正文
        scripts/            可选。可执行脚本 —— **可以跑而不读进上下文**
        references/         可选。按需读进上下文的参考资料
        assets/             可选。产出里要用的文件(模板、图标等)

关键在**三级加载**,这是技能真正的价值:

    1. 元数据(name + description)   一直在上下文里     ~100 词/个
    2. SKILL.md 正文                 被触发时才读       <5k 词
    3. 附带资源                      用到了才读         不限(脚本可只跑不读)

所以技能**再多**也只按"每个 ~100 词"占用常驻空间;真正的大段说明只在需要时才进来。
这和"把说明一直塞在系统提示词里"是两种成本结构。

与工具的区别(别混):**工具是模型调用的可执行函数,必须有代码;技能是一份说明书,
改变的是"怎么做这件事"。** 两者可以配合 —— 技能正文里可以讲"遇到 X 就用 Y 工具"。

frontmatter 用**标准 YAML**(`yaml.safe_load`),不是自己糊的简版解析器 —— 技能是要
**移植**的东西,格式得按通用规矩来。现在支持:

    name         必需。和目录名一致
    description  必需。写"什么时候该用"
    requires     可选。**硬依赖**:缺一个这个技能就干不了活
    optional     可选。**可选依赖**:缺了只是降级,代价可接受

依赖项里 `skill` / `pip` **二选一**,加上 `reason`(为什么需要它);可选依赖还要
`fallback`(缺了怎么办)。**这两样怎么用、写不写得出 `fallback` 意味着什么,
见 `skills/writing-skills/SKILL.md`** —— 那是给人看的规矩,这里只负责机械地执行。

**为什么只有这两种依赖**:因为只有它们能被**确切**回答"装没装" —— `skill` 看技能目录
在不在,`pip` 看容器包仓库里有没有。

`pip` 的**范围要说死**:它查的是「有没有用 `pip --target` 装进 `/workspace/.pylibs`」,
仅此而已。标准库、基础镜像自带的包(pip / setuptools)、npm / composer 那些、VM 里的东西,
**一概看不见** —— 声明了它们会永远报缺,硬依赖因此假警报。所以那些写进正文,别设成字段。
**给不出确定答案的检查,比没有检查更危险。**
"""
from __future__ import annotations

from pathlib import Path

try:
    import yaml
except ImportError:                     # pragma: no cover - 只有在环境没装齐时才会走到
    raise RuntimeError(
        "缺少 PyYAML —— 技能的 frontmatter 是标准 YAML,读它必须要这个库。\n"
        "装一下就好:pip install -r requirements.txt"
    ) from None

from .core import PROJECT_DIR, ROOT

SKILLS_DIR = PROJECT_DIR / "skills"
# 技能目录在容器里的挂载点(只读;见 agent/tools/container.py)。技能脚本要从容器跑,
# 这里集中定义一次,免得两处各写一个路径字符串、改一处漏一处。
CONTAINER_SKILLS_DIR = "/skills"
_SKILL_FILE = "SKILL.md"
# 正文里附带的资源目录,加载时一并告诉模型"还有这些可看"
_RESOURCE_DIRS = ("scripts", "references", "assets")

# 容器里第三方包的落地位置:agent 用 `pip install --target /workspace/.pylibs <包>` 装,
# 容器再把它挂进 PYTHONPATH(见 agent/tools/container.py)。所以**宿主这边扫一眼这个目录**,
# 就知道容器里装没装 —— 不必真起一个容器去问。
PYLIBS = ROOT / ".pylibs"

# 一个依赖项能声明的东西。二选一 —— 一个 dep 只说一件事,别混着写。
# 只有"宿主能确切回答"的两种才配进来,理由见模块开头。
DEP_KINDS = ("skill", "pip")
_DEP_FIELDS = set(DEP_KINDS) | {"reason", "fallback"}

# 谁能加载这个技能。**必填,不给默认** —— 「没写」和「两边都能用」是两回事,
# 默默默认成后者就等于没分类。分析型的技能动辄让主 agent 多吃几万 token 的上下文,
# 这种事必须是有意为之,不能靠"没写就放行"。
AGENT_ROLES = ("main", "sub")


class SkillError(RuntimeError):
    """技能有问题(找不到、frontmatter 缺失或不是合法 YAML 等)。"""


# ========================= frontmatter =========================

def _split_frontmatter(text: str) -> tuple[str, str]:
    """把文件切成 (frontmatter 原文, 正文)。没有 frontmatter 就返回 ("", 全文)。"""
    if not text.startswith("---"):
        return "", text
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end is None:
        return "", text                      # 没有结束标记:当它没有 frontmatter
    return "\n".join(lines[1:end]), "\n".join(lines[end + 1:]).strip()


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析开头的 YAML frontmatter,返回 (元数据, 正文)。

    用 `yaml.safe_load` —— 不再自己写解析器。技能是要**移植**出去的解决方案,格式必须
    按 YAML 的通用规矩来(嵌套、列表、引号、多行块都是标准行为),而不是"恰好我们这套
    能读懂"的方言。宿主和容器都装了 PyYAML(见 requirements.txt)。
    """
    raw, body = _split_frontmatter(text)
    if not raw.strip():
        return {}, body
    try:
        meta = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise SkillError(f"frontmatter 不是合法的 YAML:{exc}") from None
    if meta is None:
        return {}, body
    if not isinstance(meta, dict):
        raise SkillError("frontmatter 要是一组 `键: 值`,现在解析出来不是")
    return meta, body


def parse_agents(meta: dict) -> list[str]:
    """读出这个技能谁能加载。

    **必填。** 分析型的技能(安全审计、结构分析那一类)会让加载它的 agent 一口气多读
    几万 token 的上下文 —— 那正是子 agent 存在的理由。这种事必须作者**明确声明**,
    不能靠"没写就当谁都能用"放行,否则新写的重技能会默认继续压在主 agent 身上,
    分类就白做了。
    """
    raw = meta.get("agents")
    if raw is None or raw == []:
        raise SkillError(
            "frontmatter 缺 agents —— 这个技能给谁用?写 `agents: [main]`、`agents: [sub]` "
            "或 `agents: [main, sub]`。不给默认值是故意的:分析型技能会让加载它的 agent "
            "多吃很多上下文,那必须是有意为之"
        )
    if not isinstance(raw, list):
        raise SkillError("agents 要是一个列表,如 `agents: [main, sub]`")
    out = [str(a).strip() for a in raw]
    bad = [a for a in out if a not in AGENT_ROLES]
    if bad:
        raise SkillError(
            f"agents 里有不认识的角色 {'、'.join(bad)} —— 只认 {'、'.join(AGENT_ROLES)}"
        )
    seen: list[str] = []
    for a in out:                       # 去重,但保持写的顺序(报错信息里读起来顺)
        if a not in seen:
            seen.append(a)
    return seen


def parse_deps(meta: dict, field: str) -> list[dict]:
    """把一个依赖字段(requires / optional)规整成 [{'kind','name','reason',...}]。

    这里**只做机械校验**(结构对不对、字段全不全),不判断依赖本身存不存在 ——
    那是 `resolve_dep` 的事。分两步是因为"写得对不对"是作者的问题(该在自检时就报),
    而"装没装"是环境的问题(该在加载时才说)。
    """
    raw = meta.get(field)
    if raw is None or raw == []:
        return []
    if not isinstance(raw, list):
        raise SkillError(f"{field} 要是一个列表,每项写成 `- pip: 包名` 这样")

    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            raise SkillError(f"{field} 里的每一项都要写成 `- pip: 包名` 这样的键值对")
        unknown = set(item) - _DEP_FIELDS
        if unknown:
            raise SkillError(
                f"{field} 里有不认识的字段 {'、'.join(sorted(unknown))} —— "
                f"只认 {'、'.join(sorted(_DEP_FIELDS))}"
            )
        kinds = [k for k in DEP_KINDS if item.get(k)]
        if len(kinds) != 1:
            raise SkillError(
                f"{field} 里每一项要**恰好**声明 skill / pip 中的一个,"
                f"现在有 {len(kinds)} 个:{item}"
            )
        kind = kinds[0]
        if not item.get("reason"):
            raise SkillError(
                f"{field} 里的 `{kind}: {item[kind]}` 没写 reason —— "
                f"不写清为什么需要它,读的人没法判断它到底能不能少(也判断不了它该不该是可选依赖)"
            )
        if field == "optional" and not item.get("fallback"):
            raise SkillError(
                f"{field} 里的 `{kind}: {item[kind]}` 没写 fallback —— "
                f"**可选依赖的缺失代价必须写出来**,否则没法证明这个代价是可以接受的;"
                f"要是写不出一个能接受的下场,它就该是 requires(硬依赖)"
            )
        out.append({"kind": kind, "name": str(item[kind]).strip(),
                    "reason": str(item["reason"]).strip(),
                    "fallback": str(item.get("fallback") or "").strip()})
    return out


# ========================= 发现与读取 =========================

def _skill_paths() -> list[Path]:
    if not SKILLS_DIR.exists():
        return []
    return sorted(p for p in SKILLS_DIR.iterdir()
                  if p.is_dir() and (p / _SKILL_FILE).is_file())


def _read_skill(path: Path) -> tuple[dict, str]:
    """读一个技能,返回 (元数据, 正文)。有问题抛 SkillError。"""
    try:
        text = (path / _SKILL_FILE).read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillError(f"读不出 {_SKILL_FILE}:{exc}") from None
    meta, body = _parse_frontmatter(text)
    # 元数据在这里就校验,别拖到加载时才炸 —— 技能坏在元数据上,清单里就该看得出来。
    # (只校验"写得对不对";能不能满足是加载时的事。)
    parse_agents(meta)
    parse_deps(meta, "requires")
    parse_deps(meta, "optional")
    return meta, body


def discover() -> list[dict]:
    """列出全部技能,按名字排序。每项含 name/description/目录/附带资源/依赖/错误。

    目录名就是技能名(和 frontmatter 里的 name 一致时以目录名为准)——
    这样"技能叫什么"看目录就知道,不必打开文件。

    **坏掉的技能不藏起来**:frontmatter 读不了(比如 YAML 写错)也照样列出来,只是把
    错误写在 `error` 里。静默消失才是最糟的 —— 作者会以为技能装上了,而模型从来没见过它。
    """
    out = []
    for path in _skill_paths():
        entry = {"name": path.name, "description": "", "dir": path, "agents": [],
                 "resources": [], "requires": [], "optional": [], "error": ""}
        try:
            meta, _ = _read_skill(path)
            entry["description"] = str(meta.get("description") or "").strip()
            entry["agents"] = parse_agents(meta)
            entry["requires"] = parse_deps(meta, "requires")
            entry["optional"] = parse_deps(meta, "optional")
            entry["resources"] = [d for d in _RESOURCE_DIRS if (path / d).is_dir()]
        except SkillError as exc:
            entry["error"] = str(exc)
        out.append(entry)
    return out


def names() -> list[str]:
    return [s["name"] for s in discover()]


# ========================= 依赖 =========================

def _installed_packages() -> set[str]:
    """扫 `.pylibs`,返回装了哪些包(名字归一化过)。

    `.pylibs` 是**容器**的包仓库(`pip install --target` 落在这儿),不是宿主环境的 ——
    技能脚本跑在容器里,所以该看的是它。两边各有一套 Python,别混。

    名字有两个来源,都得算:`*.dist-info` 目录给的是**发行名**(`pyyaml-6.0.3.dist-info`),
    而顶层目录给的是**导入名**(`yaml`)。技能里两种写法都可能出现,所以两个都收。
    """
    pkgs: set[str] = set()
    if not PYLIBS.is_dir():
        return pkgs
    for p in PYLIBS.iterdir():
        n = p.name
        if n == "__pycache__":
            continue
        for suffix in (".dist-info", ".egg-info"):
            if n.endswith(suffix):
                # `pyyaml-6.0.3.dist-info` → `pyyaml`(版本号在最后,去掉)
                pkgs.add(_norm_pkg(n[: -len(suffix)].rsplit("-", 1)[0]))
                break
        else:
            if p.suffix == ".py":                       # 单文件模块
                pkgs.add(_norm_pkg(p.stem))
            elif (p / "__init__.py").is_file():         # 常规包目录
                pkgs.add(_norm_pkg(n))
    return pkgs


def _norm_pkg(name: str) -> str:
    """包名归一化 —— 大小写不敏感、`-`/`_`/`.` 等价(PEP 503 的规矩)。"""
    return name.lower().replace("-", "_").replace(".", "_")


def resolve_dep(dep: dict) -> str:
    """这个依赖现在满不满足?返回 `have` 或 `missing`。

    **只给两种答案,不给"大概是吧"。** 不认识的包 = 当它没有 —— 这个方向的错(把有的
    说成没有)只是让人多确认一句,反过来的错(把没有的说成有)会让人**直接动手**,然后
    在用到它的那一刻才发现跑不起来。
    """
    kind, name = dep["kind"], dep["name"]
    if kind == "skill":
        return "have" if (SKILLS_DIR / name / _SKILL_FILE).is_file() else "missing"
    return "have" if _norm_pkg(name) in _installed_packages() else "missing"


# 两种状态在提示里长什么样。用 `[+]` / `[!]` 而不是图形符号 —— 那种在老控制台上会显示成
# 乱码甚至把输出截断(踩过)。
_MARK = {"have": "[+]", "missing": "[!]"}


def _dep_lines(deps: list[dict], field: str, role: str = "main") -> list[str]:
    out = []
    for d in deps:
        state = resolve_dep(d)
        head = f"- {_MARK[state]} {d['kind']} `{d['name']}` —— {d['reason']}"
        if state == "have" and d["kind"] == "skill":
            head += f"(在容器里是 {CONTAINER_SKILLS_DIR}/{d['name']}/,可以用它的资源)"
            # 依赖的**资源**能用、但它的**正文**这一方加载不了 —— 这种半可用状态必须
            # 说清楚。不说的话,模型看到"在场"就会去 load_skill,被拒之后又得重新理解
            # 一遍为什么,白烧一轮。
            others = _agents_of(d["name"])
            if others and role not in others:
                head += " —— 注意:它的**正文你加载不了**(只给子 agent),只能用它的文件"
        elif state == "missing" and d["kind"] == "pip":
            # 说清**查的是什么**,而不是断言"没装"。宿主只能看见 .pylibs,像基础镜像自带的
            # 包它就看不见 —— 写成"没有"是把话说过了头,而模型据此去告诉用户"做不了"。
            head += "(容器包仓库里没找到;若是镜像本来就带的,它可能其实在)"
            if field == "optional":
                head += f"。缺了怎么办:{d['fallback']}"
        elif state == "missing" and field == "optional":
            head += f";缺了怎么办:{d['fallback']}"
        out.append(head)
    return out


def _dependency_note(meta: dict, role: str = "main") -> str:
    """把依赖状况渲染成给模型看的一段话。

    这里的态度很关键,分两种:

    **硬依赖** —— 缺了就是干不了活。要说清三件事:缺了什么、**不许换个法子硬做**、
    老老实实停下来告诉用户哪一部分做不了。模型的本能是"绕过去",绕出来的东西用户
    看不出来是残的,那是**看着像结论的猜测**,比明说做不了糟糕得多。

    **可选依赖** —— 缺了**不提醒**(它的缺失代价本该是可接受的,不值得占注意力);
    但在场时要**主动说**:它能补上哪块、资源在哪,让模型知道手里的牌比想象中多。
    """
    try:
        requires = parse_deps(meta, "requires")
        optional = parse_deps(meta, "optional")
    except SkillError:
        return ""
    if not requires and not optional:
        return ""

    parts: list[str] = []
    if requires:
        lines = _dep_lines(requires, "requires", role)
        if any(resolve_dep(d) != "have" for d in requires):
            parts.append("这个技能声明了**硬依赖**,下面标 `[!]` 的在容器里没找到:")
            parts.append("\n".join(lines))
            parts.append(
                "**缺硬依赖时不要换别的方法硬做,也不要用别的工具凑一个结果出来** —— "
                "那样产出的东西看着完整、实际是残的。正确做法:停下来,把这个技能里"
                "**做不了的是哪一部分**如实告诉用户,由他决定装依赖还是算了。"
            )
        else:
            parts.append("硬依赖都满足了:")
            parts.append("\n".join(lines))
    if optional:
        have = [d for d in optional if resolve_dep(d) == "have"]
        if have:
            parts.append("还带了几个**可选**能力,当前环境里是有的 —— 用得上就用:")
            parts.append("\n".join(_dep_lines(have, "optional", role)))
    # 一个字都没得说就别出声 —— 可选依赖全缺时正是这种情况,而那时候本来就该**静默**,
    # 留一个光秃秃的 "### 依赖" 标题纯属占地方(还容易让人以为下面漏印了东西)。
    if not parts:
        return ""
    return "### 依赖\n\n" + "\n\n".join(parts)


# ========================= 加载 =========================

def _agents_of(name: str) -> list[str]:
    """某个技能给了谁用(读不出来就返回空 —— 坏技能的事由别处报)。"""
    try:
        meta, _ = _read_skill(SKILLS_DIR / name)
        return parse_agents(meta)
    except (SkillError, OSError):
        return []


def _refuse_reason(agents: list[str], role: str) -> str:
    """不能加载时,给一句**能接着往下走**的话。

    只说"不行"是最糟的:模型会开始瞎绕 —— 换个工具硬做、或者干脆凭印象编。所以拒绝的
    同时必须告诉它**该找谁**。两侧的说法不一样:主 agent 是"派出去",子 agent 是"报上去"
    —— 后者自己不能往下派(深度锁死一层)。
    """
    who = "、".join("主 agent" if a == "main" else "子 agent" for a in agents)
    if role == "main":
        return (f"这个技能只给{who}用,你不能直接加载。它做的事整套都在子 agent 那边 —— "
                f"**把活派给一个子 agent 去做**,别自己硬做:你的上下文要留着协调全局,"
                f"把这类分析塞进来就正好毁掉这件事。")
    return (f"这个技能只给{who}用,你是子 agent,加载不了。"
            f"如果这个任务确实需要它,把这件事**写进你的报告**交由主 agent 决定 —— "
            f"不要用别的方法凑一个结果出来。")


def load(name: str, role: str = "main") -> str:
    """读一个技能的**正文**(不含 frontmatter),供模型按需加载。

    正文之外还附两段说明:能补上什么(依赖)和到哪去找(附带资源)。那些文件**不会**
    自动读进来,要模型自己决定(三级加载的第三级)。

    **这里也是"路径"唯一被写出来的地方。** 技能正文里只写相对路径(`scripts/x.py`),
    绝不该写死 `/skills/<名>/...` —— 技能是可移植的,`/skills` 只是**我们这个项目**的
    挂载点,换个宿主就不成立了;而且写死的路径会随改名、挪文件悄悄失效。所以由这里
    在加载时把真实位置告诉模型,正文只需要说"跑 `scripts/x.py`"。
    """
    path = SKILLS_DIR / name
    skill_file = path / _SKILL_FILE
    if not skill_file.is_file():
        available = "、".join(s["name"] for s in discover()) or "(还没有任何技能)"
        raise SkillError(f"没有名为 {name} 的技能。现有:{available}")

    meta, body = _read_skill(path)

    # 谁能加载:在**返回正文之前**拦。放进去再拦就晚了 —— 正文一进上下文,省上下文
    # 这件事就已经失败了。
    agents = parse_agents(meta)
    if role not in agents:
        raise SkillError(_refuse_reason(agents, role))

    # 附带资源不进上下文,但要说清"在哪、怎么用" —— 尤其脚本:技能目录在工作区**之外**,
    # 你的文件工具够不到;它是**只读**挂载在容器里的 /skills 下(见 container.py 的挂载),
    # 所以跑脚本要用 run_command / run_python 走容器那条路。
    # **一个都不能少。** 这里以前截过 40 个,那是错的:没列出来的文件模型根本不知道它存在,
    # 也就永远不会去用 —— 而它**不会报错、不会被察觉**,只表现为"这个技能能干的活比实际少"。
    # 真嫌长就该去精简技能的附带资源,不该在这儿悄悄砍掉一截(而且砍了数量还对不上,
    # 那一行会说"带了 40 个")。
    #
    # 但**编译产物要排除**:`__pycache__/*.pyc` 是跑脚本时掉出来的垃圾,列出来纯属占地方 ——
    # 模型既不会去读它(源码就在旁边),那行绝对路径也没有任何用。这是"少列几个"的反面:
    # 不是漏了真东西,是把不是东西的列进来了。
    files = [f for d in _RESOURCE_DIRS if (path / d).is_dir()
             for f in sorted((path / d).rglob("*"))
             if f.is_file() and "__pycache__" not in f.parts
             and f.suffix not in (".pyc", ".pyo")]
    if files:
        rel = [f.relative_to(path).as_posix() for f in files]
        listing = "\n".join(f"- {r}  →  容器里:{CONTAINER_SKILLS_DIR}/{name}/{r}" for r in rel)
        body += (
            f"\n\n---\n这个技能还带了 {len(rel)} 个附带资源(不会自动进上下文,需要时自己去取)。"
            f"技能目录**不在工作区里**、你的文件工具够不到它,但它在容器里**只读**挂载着 —— "
            f"所以要用 `run_command` / `run_python` 走容器那条路:\n{listing}\n"
            f"(只读:能跑能读,改不了。VM 里看不到这些文件。)"
        )

    dep = _dependency_note(meta, role)
    if dep:
        body += f"\n\n---\n{dep}"
    return body


_SUB_ONLY_TAG = " **[子 agent 专用]**"
_SUB_SCOPE = ("(这份清单只列**你能用**的技能 —— 你是子 agent,不能再往下派活。)")
_MAIN_SCOPE = (
    "标着 **[子 agent 专用]** 的技能,你**加载不了**(`load_skill` 会直接拒绝)—— "
    "它们照样列在下面,因为**你得知道有哪些活可以派出去**。要用的办法是派一个子 agent "
    "去做,而不是自己硬做。"
)


def prompt_section(role: str = "main") -> str:
    """把技能清单渲染进提示词(只有名字和"什么时候用",正文不进去)。

    **主 agent 看得见全部技能,包括它自己加载不了的** —— 这是有意的:不知道有哪些活
    可以派出去,就没法合理分配任务。所以"只给子 agent"的技能在这里是**列出来并打标**,
    而不是藏起来;真正拦人的地方在 `load()`。

    子 agent 反过来:只列它能加载的 —— 它不能往下派,给它看一堆用不了的技能只是
    白占上下文,还会诱导它去试。
    """
    from . import prompts
    lines = []
    for s in discover():
        if s["error"]:
            lines.append(f"- `{s['name']}` —— !(这个技能有错,加载会失败:{s['error']})")
            continue
        sub_only = "sub" in s["agents"] and "main" not in s["agents"]
        if role == "sub" and role not in s["agents"]:
            continue
        desc = s["description"] or "(没有写 description —— 模型将无从判断何时该用它)"
        tag = _SUB_ONLY_TAG if (sub_only and role == "main") else ""
        lines.append(f"- `{s['name']}`{tag} —— {desc}")
    body = "\n".join(lines) if lines else "(目前没有任何技能)"
    return prompts.load("skills_list", skills=body,
                        scope=_SUB_SCOPE if role == "sub" else _MAIN_SCOPE)
