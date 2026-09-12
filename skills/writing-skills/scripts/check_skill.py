#!/usr/bin/env python3
"""校验一个技能的 SKILL.md:frontmatter 是否合规,markdown 是否过得了 lint。

用法(在容器里跑):
    python /skills/<技能名>/scripts/check_skill.py /skills/<技能名>
    python /skills/<技能名>/scripts/check_skill.py /skills/<技能名> --md-only

为什么要有这个脚本:技能是给人看的 markdown 文件,格式不规范的话,用户一打开就是
满屏 lint 警告。与其事后一条条改,不如**交出去之前先自己过一遍**。

检查的是 markdownlint 的默认规则里最常踩的那几条(MD 编号沿用官方编号,方便对照):
    MD041 首行要是 H1 / MD025 只能有一个 H1
    MD022 标题上下要有空行 / MD031 代码块上下要有空行 / MD032 列表上下要有空行
    MD040 代码块要标语言 / MD004 无序列表符号要统一
    MD012 不能有连续空行 / MD009 行尾不能有空格 / MD047 文件要以单个换行结尾
    MD013 行太长(只对"含空格、本来能折行"的行报 —— 纯中文行不受影响)

**MD013 这条我故意做得比 markdownlint 严**:实测 markdownlint 对某些 80~95 字符的行
并不报(规则细节和 CJK 有关),而这里一律按"超过 80 且有词间空格"报。宁可多报几条、
让人少写几个长行,也不要出现"脚本说没问题、一打开满屏警告" —— 后者才是真正难受的。
换句话说:**过了这个脚本,就一定过得了 markdownlint**;反过来不保证。
"""
import re
import sys
import pathlib

MAX_LINE = 80
LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s")
HEADING_RE = re.compile(r"^(#{1,6})\s")
FENCE_RE = re.compile(r"^(\s*)(```+|~~~+)\s*(\S*)")


def check_frontmatter(text: str, dir_name: str) -> tuple[list[str], int]:
    """查 frontmatter,返回 (问题列表, 正文起始行号)。"""
    issues = []
    m = re.match(r"^---\n(.*?)\n---\n?", text, re.S)
    if not m:
        return ["缺少 frontmatter(文件要以 --- 开头,再用 --- 收尾)"], 0
    meta = m.group(1)
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

        if blank:
            if prev_blank and i > 0:
                out.append(f"{n}: MD012 连续空行")
        else:
            if raw != raw.rstrip():
                out.append(f"{n}: MD009 行尾有多余空格")
            # MD013:只对"含空格、本来能折行"的行报(纯中文长行不算)。
            # 列表项和标题**同样要查** —— markdownlint 不豁免它们(我先前豁免了,
            # 结果和真实 lint 判定不一致,是更危险的"假阴性")。
            if len(raw) > MAX_LINE and " " in raw.strip():
                out.append(f"{n}: MD013 行太长({len(raw)},上限 {MAX_LINE})")

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


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    d = pathlib.Path(args[0] if args else ".")
    f = d / "SKILL.md"
    if not f.is_file():
        print(f"× 没有 SKILL.md:{f}")
        return 1

    def _md_issues(path: pathlib.Path, text: str) -> list[str]:
        lines = [ln for ln in text.split("\n")]
        while lines and not lines[-1].strip():
            lines.pop()
        out = check_markdown(lines, 0)
        if text and not text.endswith("\n"):
            out.append("MD047 文件结尾要有换行")
        elif text.endswith("\n\n"):
            out.append("MD047 文件结尾只能有一个换行")
        return [f"{path.name} {it}" for it in out]

    text = f.read_text(encoding="utf-8")
    issues, body_start = check_frontmatter(text, d.resolve().name)
    if body_start:
        lines = text.split("\n")[body_start:]
        while lines and not lines[-1].strip():
            lines.pop()
        issues += check_markdown(lines, body_start)
    if text and not text.endswith("\n"):
        issues.append("MD047 文件结尾要有换行")
    elif text.endswith("\n\n"):
        issues.append("MD047 文件结尾只能有一个换行")

    # 附带资源里的 markdown 也要查 —— 它们同样是用户会打开看的文件,
    # 只查 SKILL.md 的话,警告会从 references/ 里冒出来。
    for sub in ("references", "assets"):
        ref_dir = d / sub
        if ref_dir.is_dir():
            for md in sorted(ref_dir.rglob("*.md")):
                issues += _md_issues(md, md.read_text(encoding="utf-8"))

    if not issues:
        print(f"√ {d.name}:通过")
        return 0
    print(f"× {d.name} 有 {len(issues)} 处问题:")
    for it in issues:
        print(f"   {it}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
