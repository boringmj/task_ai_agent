"""提示词集中管理:**一个用途一个文件,程序按文件名读**。

所有写给模型的文本都放在项目根的 `prompts/` 目录里,一个用途一个 `.md`:

    prompts/system.md              主系统提示词
    prompts/memory_injection.md    长期记忆注入时的包装
    prompts/commands_list.md       终端指令清单的外壳(里面嵌 {commands})
    prompts/resume_notice.md       会话恢复时追加的提示
    prompts/switch_notice.md       切换会话时追加的提示
    prompts/compact_instruction.md 让模型压缩历史的指令
    prompts/compact_summary.md     压缩后替换掉历史的那条包装

读取统一走 `load("文件名", 变量=值)`:文件里写 `{变量名}`,调用处把值传进来即可。

为什么这么分:
- **一用途一文件**才能"按名字取"—— 调用点 `load("resume_notice")` 一眼就是它;
  合成一个大文件就得靠分隔符解析,反而绕。
- **文案与代码分开**:改措辞、改口吻、加个例子都不必动 .py。这一点在本项目里尤其
  值钱:中文文案写在 Python 字符串里,混进一个 ASCII 引号就把字符串截断了(踩过多次)。
- 只替换**调用方显式传进来的**变量,不做 `str.format` —— 提示词里常有 JSON 示例
  (`{"path": "x"}` 这种),用 format 会因为花括号报错。
"""
from __future__ import annotations

from pathlib import Path

from .core import PROJECT_DIR

PROMPTS_DIR = PROJECT_DIR / "prompts"


class MissingPrompt(FileNotFoundError):
    """提示词文件不存在 —— 这是打包/部署错误,不该被静默吞掉。"""


def load(name: str, **variables) -> str:
    """按名字读一段提示词,并把 `{变量}` 替换成给定值。

    name 可以带或不带 `.md`;只替换传进来的变量,其余花括号原样保留(提示词里会
    出现 JSON 示例)。文件不存在直接抛错:提示词缺了,agent 的行为就不可预期,
    与其悄悄跑下去,不如当场说清楚缺的是哪个文件。
    """
    path = PROMPTS_DIR / (name if name.endswith(".md") else f"{name}.md")
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise MissingPrompt(
            f"缺少提示词文件 {path};提示词都在 {PROMPTS_DIR} 下,按用途分文件存放"
        ) from None
    for key, value in variables.items():
        text = text.replace("{" + key + "}", str(value))
    return text.strip()


def exists(name: str) -> bool:
    return (PROMPTS_DIR / (name if name.endswith(".md") else f"{name}.md")).exists()
