"""技能(skill):按需加载的说明书,给模型处理复杂事情的能力。

一个技能 = 一个目录 + 一份 `SKILL.md`:

    skills/<技能名>/
        SKILL.md            必需。YAML frontmatter(name/description)+ 正文
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
"""
from __future__ import annotations

import re
from pathlib import Path

from .core import PROJECT_DIR

SKILLS_DIR = PROJECT_DIR / "skills"
_SKILL_FILE = "SKILL.md"
# 正文里附带的资源目录,加载时一并告诉模型"还有这些可看"
_RESOURCE_DIRS = ("scripts", "references", "assets")


class SkillError(RuntimeError):
    """技能有问题(找不到、frontmatter 缺失等)。"""


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析开头的 YAML frontmatter,返回 (元数据, 正文)。

    只认最简单的 `键: 值`,不引入 yaml 依赖 —— 技能元数据本来就只有 name 和
    description 两个字段。值可以折行(YAML 的普通标量规则):后续**缩进**的行
    属于上一个键,一直读到下一个顶格的 `键:` 或结束标记为止。
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end is None:
        return {}, text                      # 没有结束标记:当它没有 frontmatter

    meta: dict[str, str] = {}
    key = None
    for raw in lines[1:end]:
        if not raw.strip():
            continue
        if raw[:1].isspace() and key:        # 折行的续行
            meta[key] = (meta[key] + " " + raw.strip()).strip()
            continue
        k, sep, v = raw.partition(":")
        if not sep:
            continue
        key = k.strip()
        meta[key] = v.strip()
    return meta, "\n".join(lines[end + 1:]).strip()


def _skill_paths() -> list[Path]:
    if not SKILLS_DIR.exists():
        return []
    return sorted(p for p in SKILLS_DIR.iterdir()
                  if p.is_dir() and (p / _SKILL_FILE).is_file())


def discover() -> list[dict]:
    """列出全部技能,按名字排序。每项含 name/description/目录/附带资源。

    目录名就是技能名(和 frontmatter 里的 name 一致时以目录名为准)——
    这样"技能叫什么"看目录就知道,不必打开文件。
    """
    out = []
    for path in _skill_paths():
        try:
            text = (path / _SKILL_FILE).read_text(encoding="utf-8")
        except OSError:
            continue
        meta, _ = _parse_frontmatter(text)
        resources = [d for d in _RESOURCE_DIRS if (path / d).is_dir()]
        out.append({
            "name": path.name,
            "description": (meta.get("description") or "").strip(),
            "dir": path,
            "resources": resources,
        })
    return out


def load(name: str) -> str:
    """读一个技能的**正文**(不含 frontmatter),供模型按需加载。

    正文之外还附一句"这个技能还带了哪些资源",让模型知道可以进一步去看 ——
    但那些文件不会自动读进来,要它自己决定(三级加载的第三级)。
    """
    path = SKILLS_DIR / name
    skill_file = path / _SKILL_FILE
    if not skill_file.is_file():
        available = "、".join(s["name"] for s in discover()) or "(还没有任何技能)"
        raise SkillError(f"没有名为 {name} 的技能。现有:{available}")

    text = skill_file.read_text(encoding="utf-8")
    _meta, body = _parse_frontmatter(text)
    extra = [d for d in _RESOURCE_DIRS if (path / d).is_dir()]
    if extra:
        listing = []
        for d in extra:
            files = sorted(f.name for f in (path / d).rglob("*") if f.is_file())[:20]
            listing.append(f"- {d}/:{'、'.join(files) or '(空)'}")
        body += ("\n\n---\n这个技能还带了这些资源(需要时自己去读,不会自动进上下文):\n"
                 + "\n".join(listing))
    return body


def names() -> list[str]:
    return [s["name"] for s in discover()]


def prompt_section() -> str:
    """把技能清单渲染进提示词(只有名字和"什么时候用",正文不进去)。"""
    from . import prompts
    lines = []
    for s in discover():
        desc = s["description"] or "(没有写 description —— 模型将无从判断何时该用它)"
        lines.append(f"- `{s['name']}` —— {desc}")
    body = "\n".join(lines) if lines else "(目前没有任何技能)"
    return prompts.load("skills_list", skills=body)
