from __future__ import annotations

from .registry import tool

from ..core import (
    MAX_WRITE_BYTES,
    MEMORY_FILE,
)


# ---------------- 长期记忆 ----------------
# memory.md 记录重启后仍需记住的关键信息,区别于只活一个会话的 messages:
# 它持久化在工作区,下次启动时读回并注入系统提示词,人可读、可编辑、可回退。


def _read_memory() -> str:
    if not MEMORY_FILE.exists():
        return ""
    return MEMORY_FILE.read_text(encoding="utf-8")


@tool(
    description="把一条需要跨会话记住的关键事实写进长期记忆(追加,不覆盖已有记录)。"
                "**主动用**:发现用户偏好、习惯、禁忌、重要约定或项目背景时,当场就记下来,"
                "别等用户提醒你记 —— 下一次会话你不会记得任何事。"
                "记之前先用 read_memory 确认没记过;要修改已有条目,请用 read_file + edit_lines "
                "直接改 .agent/memory.md,不要重复追加一条。一次性、随风而去的临时信息不要记。",
    parameters={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要记住的内容,支持多行;每行会存成一条记忆",
                    }
                },
                "required": ["content"],
            },
)
def remember(content: str) -> str:
    """把一条关键事实写进长期记忆。每条追加一行,不覆盖已有记录。"""
    content = content.strip()
    if not content:
        raise ValueError("要记住的内容不能为空")
    size = len(content.encode("utf-8"))  # 上限是字节数,不能拿字符数比 —— 中文一字 3 字节
    if size > MAX_WRITE_BYTES:
        raise ValueError(f"内容过大({size} 字节),超过单次写入上限 {MAX_WRITE_BYTES} 字节")
    # 记内容前先洗一遍:去掉空行,也去掉纯分隔符行(===、--- 这类),
    # 否则会把记忆文件的结构弄脏
    lines = [
        line.strip()
        for line in content.splitlines()
        if line.strip() and set(line.strip()) != {"="} and set(line.strip()) != {"-"}
    ]
    bullets = "\n".join(f"- {line}" for line in lines)
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)  # 首次写时把隐藏目录建出来
    MEMORY_FILE.touch(exist_ok=True)

    with MEMORY_FILE.open("a", encoding="utf-8") as fp:
        if fp.tell() == 0:  # 第一次写时补个标题
            fp.write("# 长期记忆\n\n")
        fp.write(bullets + "\n")
    return f"已记住 {len(lines)} 行。"


@tool(
    description="读取当前的全部长期记忆。需要回忆以前记下的关键信息、或确认自己记住了什么时使用。",
    parameters={"type": "object", "properties": {}},
)
def read_memory() -> str:
    """读取当前的全部长期记忆。"""
    content = _read_memory().strip()
    return content if content else "(长期记忆目前是空的。要记住重要信息,用 remember 工具。)"