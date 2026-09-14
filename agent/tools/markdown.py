"""检查 markdown 格式的工具。

写文件时其实**已经自动查过一遍**了(见 fs.py 的 _markdown_note),这个工具是给
"想单独复查"用的:比如改完一批文件、或用户明确说某个 md 有警告时。
"""
from __future__ import annotations

from .registry import tool
from .. import markdown as md
from ..core import safe_path

# 查目录时跳过这些:第三方包里自带一堆 README/LICENSE.md,报出来只会刷屏,
# 而且那些不是我们写的、也改不得。
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "site-packages", ".pylibs"}


@tool(
    agents=("main", "sub"),
    description="检查 markdown 的格式是否规范(和 markdownlint 对齐的一套规则),"
                "返回具体哪一行有什么问题。写 .md 文件时系统会自动查并把问题附在结果里,"
                "所以平时不用特意调用;想单独复查某个文件、或一次检查一个目录下所有 .md 时用它。"
                "path 可以是文件,也可以是目录(会递归查目录下的 .md)。",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要检查的文件或目录(相对工作区),如 report.md 或 docs/",
            }
        },
        "required": ["path"],
    },
)
def check_markdown(path: str) -> str:
    """检查一个 .md 文件或目录下所有 .md 的格式。"""
    target = safe_path(path, "read")
    if not target.exists():
        return f"错误:{target} 不存在"

    files = ([f for f in sorted(target.rglob("*.md")) if not _SKIP_DIRS & set(f.parts)]
             if target.is_dir() else [target])
    if not files:
        return f"{target} 下没有 .md 文件"

    checked = [(f, md.lint_file(f)) for f in files]      # 每个文件只查一次
    bad = [(f, issues) for f, issues in checked if issues]
    if not bad:
        return f"检查了 {len(files)} 个 markdown 文件,格式都没问题。"

    total = sum(len(issues) for _, issues in bad)
    out = [f"检查了 {len(files)} 个文件,其中 {len(bad)} 个有问题(共 {total} 处):", ""]
    for f, issues in bad:
        out.append(f"{f.name}:")
        out += [f"  {it}" for it in issues]
        out.append("")
    return "\n".join(out).rstrip()
