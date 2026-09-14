"""技能相关的工具:把某个技能按需读进来。

清单(名字 + 什么时候用)会随提示词一直在场,正文不在 —— 所以需要一个"翻开它"的动作。
"""
from __future__ import annotations

from .registry import tool
from .. import skills


@tool(
    description="把一个技能的完整说明读进上下文。技能是按需加载的说明书:平时只看到清单"
                "(名字 + 什么时候用),判断某个技能对当前任务有用时,用它把正文读进来再照做。"
                "参数 name 就用清单里的名字;读错了或不存在会告诉你有哪些可选。"
                "读完如果还有 references/ 之类的附带资源,需要时再自己去读。"
                "返回里还会带上**依赖状况**:硬依赖缺了会明确警告(那时不要换别的方法硬做,"
                "停下来把做不了的部分告诉用户),可选依赖在场时会告诉你它能补上什么。",
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "技能名(清单里那个名字,如 writing-skills)",
            }
        },
        "required": ["name"],
    },
)
def load_skill(name: str) -> str:
    """读出一个技能的正文。清单在提示词里,正文不在 —— 这一步把它取出来。"""
    try:
        body = skills.load(name.strip())
    except skills.SkillError as exc:
        return f"错误:{exc}"
    return f"技能 {name} 的说明:\n\n{body}"
