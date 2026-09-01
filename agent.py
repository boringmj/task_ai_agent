"""最小可用的 DeepSeek Agent:一个 while 循环 + 工具调用。

用法:
    pip install openai python-dotenv
    # 在 .env 里写上 DEEPSEEK_API_KEY=sk-xxxxxx
    python agent.py
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# deepseek-chat 支持工具调用;deepseek-reasoner(R1)目前不支持 tools
MODEL = "deepseek-v4-flash"
# 护栏:一次提问最多允许几轮"模型 <-> 工具"往返,防止死循环烧钱
MAX_STEPS = 30
# 护栏:列目录时最多返回多少项,避免超大目录把上下文撑爆
MAX_ENTRIES = 200
# 护栏:单次写入的字节上限,防止模型一口气写爆磁盘
MAX_WRITE_BYTES = 256 * 1024
# 即使在沙箱内,这些文件也禁止读取 —— 纵深防御,防止沙箱里混入密钥文件
DENY_READ = {".env"}

PROJECT_DIR = Path(__file__).parent.resolve()
# 系统提示词跟着代码走,不放进沙箱 —— 模型不能读自己的提示词,更不能改
PROMPT_FILE = PROJECT_DIR / "system_prompt.md"


def _resolve_workspace() -> Path:
    """确定沙箱目录:项目下的一个专用子目录,模型只能在里面活动。

    刻意不允许指向项目根或项目外 —— 否则 agent.py、.env 都会落进射程。
    """
    root = (PROJECT_DIR / os.environ.get("AGENT_WORKSPACE", "workspace")).resolve()
    if root == PROJECT_DIR or not root.is_relative_to(PROJECT_DIR):
        raise SystemExit(
            f"AGENT_WORKSPACE 必须是项目内的子目录,不能是项目根目录或外部路径。\n"
            f"  当前解析结果:{root}\n"
            f"  项目目录:    {PROJECT_DIR}"
        )
    root.mkdir(parents=True, exist_ok=True)  # 不存在就建一个,免得首次运行到处报错
    return root


# 沙箱:所有文件操作都被限制在这个目录内
ROOT = _resolve_workspace()
# 回收站:删除的文件移到这里,不做真删除
TRASH_DIR = ROOT / ".trash"

# DeepSeek 兼容 OpenAI 协议,只需要换 base_url
client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
)


# ---------------- 工具:普通函数 + 一段 JSON Schema 描述 ----------------


def safe_path(path: str) -> Path:
    """把模型给的路径解析为绝对路径,并确保它没有逃出 ROOT。

    相对路径基于 ROOT 解析;绝对路径经 resolve() 后一并校验。
    resolve() 会展开 .. 和符号链接,所以 "../../etc" 和指向外部的软链都会被挡下。
    """
    resolved = (ROOT / path).resolve()
    if not resolved.is_relative_to(ROOT):
        raise PermissionError(f"拒绝访问工作区 {ROOT} 之外的路径:{resolved}")
    return resolved


def get_current_time() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_file(path: str, with_line_numbers: bool = False) -> str:
    target = safe_path(path)
    if target.name in DENY_READ:
        raise PermissionError(f"{target.name} 属于敏感文件,禁止读取")
    text = target.read_text(encoding="utf-8")[:4000]
    if not with_line_numbers:
        return text
    return "\n".join(f"{i:>4} | {line}" for i, line in enumerate(text.splitlines(), 1))


def get_current_directory() -> str:
    return f"{ROOT}(你的工作区,所有文件操作都被限制在这个目录内)"


def list_files(path: str = ".", show_hidden: bool = False) -> str:
    target = safe_path(path)
    if not target.is_dir():
        return f"错误:{target} 不是一个目录"

    entries = [e for e in target.iterdir() if show_hidden or not e.name.startswith(".")]
    entries.sort(key=lambda e: (e.is_file(), e.name.lower()))  # 目录在前,再按名字排

    if not entries:
        return f"{target} 是空目录"

    lines = [
        f"{e.name}/" if e.is_dir() else f"{e.name}  ({e.stat().st_size} B)"
        for e in entries[:MAX_ENTRIES]
    ]
    if len(entries) > MAX_ENTRIES:
        lines.append(f"…(共 {len(entries)} 项,只显示前 {MAX_ENTRIES} 项)")
    return "\n".join([f"{target}(共 {len(entries)} 项):", *lines])


def _check_writable(target: Path, content: str) -> int:
    """写入前的公共校验,返回内容的字节数。"""
    size = len(content.encode("utf-8"))
    if size > MAX_WRITE_BYTES:
        raise ValueError(f"内容过大({size} 字节),单次写入上限为 {MAX_WRITE_BYTES} 字节")
    if target.is_dir():
        raise IsADirectoryError(f"{target} 是一个目录,不能当作文件写入")
    return size


def write_file(path: str, content: str, overwrite: bool = False) -> str:
    target = safe_path(path)
    size = _check_writable(target, content)

    # 覆盖是不可逆的,必须由模型显式声明意图,不能靠默认行为悄悄发生
    existed = target.is_file()
    if existed and not overwrite:
        return (
            f"错误:{target.name} 已存在({target.stat().st_size} 字节)。"
            f"确认要覆盖就带 overwrite=true 重新调用,否则换个文件名。"
        )

    target.parent.mkdir(parents=True, exist_ok=True)  # 子目录不存在就顺手建出来
    target.write_text(content, encoding="utf-8")
    return f"已{'覆盖' if existed else '创建'} {target}({size} 字节)"


def append_file(path: str, content: str) -> str:
    target = safe_path(path)
    size = _check_writable(target, content)

    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fp:
        fp.write(content)
    return f"已向 {target} 追加 {size} 字节(当前共 {target.stat().st_size} 字节)"


def _load_lines(path: str) -> tuple[Path, list[str]]:
    """按行读出文件,保留行尾换行符,供按行编辑的工具复用。"""
    target = safe_path(path)
    if target.name in DENY_READ:
        raise PermissionError(f"{target.name} 属于敏感文件,禁止编辑")
    if not target.is_file():
        raise FileNotFoundError(f"{target} 不存在;要新建文件请用 write_file")
    return target, target.read_text(encoding="utf-8").splitlines(keepends=True)


def _save_lines(target: Path, lines: list[str], summary: str, center: int) -> str:
    """写回并返回改动摘要 + 附近几行,让模型能自己确认改对没有。"""
    text = "".join(lines)
    _check_writable(target, text)
    target.write_text(text, encoding="utf-8")

    lo, hi = max(1, center - 3), min(len(lines), center + 3)
    preview = "\n".join(f"{i:>4} | {lines[i - 1].rstrip(chr(10))}" for i in range(lo, hi + 1))
    tail = f"\n改动附近的内容:\n{preview}" if preview else "\n(文件现在是空的)"
    return f"{summary},文件现共 {len(lines)} 行{tail}"


def _terminate(line: str) -> str:
    """补上缺失的行尾换行,否则插入的内容会和下一行粘成一行。"""
    return line if line.endswith("\n") else line + "\n"


def edit_lines(path: str, start_line: int, end_line: int, content: str = "") -> str:
    target, lines = _load_lines(path)
    total = len(lines)
    if not 1 <= start_line <= total or not start_line <= end_line <= total:
        raise ValueError(
            f"行号超出范围:文件共 {total} 行,收到 start_line={start_line}、end_line={end_line}。"
            f"请先用 read_file(with_line_numbers=true) 确认行号。"
        )

    new = content.splitlines(keepends=True) if content else []
    if new and end_line < total:  # 末行之外的替换必须以换行结尾,否则会粘住后一行
        new[-1] = _terminate(new[-1])

    replaced = end_line - start_line + 1
    updated = lines[:start_line - 1] + new + lines[end_line:]
    verb = "删除" if not new else "替换"
    summary = f"已{verb}第 {start_line}-{end_line} 行({replaced} 行 → {len(new)} 行)"
    return _save_lines(target, updated, summary, start_line)


def insert_lines(path: str, after_line: int, content: str) -> str:
    target, lines = _load_lines(path)
    total = len(lines)
    if not 0 <= after_line <= total:
        raise ValueError(
            f"after_line 必须在 0 到 {total} 之间(0 表示插入到文件最开头),收到 {after_line}。"
        )

    if after_line > 0:  # 插入点的前一行若缺换行,先补上
        lines[after_line - 1] = _terminate(lines[after_line - 1])
    new = content.splitlines(keepends=True)
    if new and after_line < total:
        new[-1] = _terminate(new[-1])

    updated = lines[:after_line] + new + lines[after_line:]
    summary = f"已在第 {after_line} 行之后插入 {len(new)} 行"
    return _save_lines(target, updated, summary, after_line + 1)


def move_file(source: str, destination: str, overwrite: bool = False) -> str:
    src = safe_path(source)
    if not src.exists():
        raise FileNotFoundError(f"{src} 不存在")

    dest = safe_path(destination)
    # 目标是已存在的目录 -> 移动进去并沿用原名,与 shell 里 mv a.txt dir/ 的习惯一致
    if dest.is_dir() and dest != src:
        dest = dest / src.name

    if src == dest:
        return f"错误:源路径和目标路径相同({src}),无需移动"
    if src.is_dir() and dest.is_relative_to(src):
        raise ValueError(f"不能把目录 {src.name} 移动到它自己内部:{dest}")

    existed = dest.exists()
    if existed:
        if dest.is_dir():
            raise IsADirectoryError(f"目标 {dest} 是已存在的目录,不能覆盖")
        if not overwrite:
            return (
                f"错误:目标 {dest.name} 已存在({dest.stat().st_size} 字节)。"
                f"确认要覆盖就带 overwrite=true 重新调用,否则换个目标名。"
            )

    kind = "目录" if src.is_dir() else "文件"  # 移动之后 src 就不在了,先取
    dest.parent.mkdir(parents=True, exist_ok=True)
    src.replace(dest)  # 用 replace 不用 rename:Windows 上 rename 遇到已存在的目标会失败

    if existed:
        action = "覆盖并移动"
    elif src.parent == dest.parent:
        action = "重命名"
    else:
        action = "移动"
    return f"已{action}{kind}:{src} -> {dest}"


def delete_file(path: str) -> str:
    target = safe_path(path)
    if not target.exists():
        raise FileNotFoundError(f"{target} 不存在")
    if target.is_dir():
        raise IsADirectoryError(f"{target} 是目录,本工具只删除单个文件")
    if TRASH_DIR == target.parent:
        raise PermissionError("该文件已在回收站中,不能重复删除")

    # 软删除:移进回收站而不是真删,给用户留后悔的余地
    TRASH_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = TRASH_DIR / f"{target.stem}.{stamp}{target.suffix}"
    seq = 1
    while dest.exists():  # 同一秒内删了同名文件
        dest = TRASH_DIR / f"{target.stem}.{stamp}-{seq}{target.suffix}"
        seq += 1

    target.rename(dest)
    return f"已把 {target.name} 移入回收站:{dest}(并未真正删除,用户可自行恢复)"


TOOL_FUNCS = {
    "get_current_time": get_current_time,
    "read_file": read_file,
    "get_current_directory": get_current_directory,
    "list_files": list_files,
    "write_file": write_file,
    "append_file": append_file,
    "edit_lines": edit_lines,
    "insert_lines": insert_lines,
    "move_file": move_file,
    "delete_file": delete_file,
}

# description 写得越清楚,模型用得越准 —— 这比换模型的收益还大
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前的日期和时间。当用户询问「现在几点」「今天几号」时使用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "读取一个文本文件的内容(最多返回前 4000 个字符)。只能读取工作区内的文件。"
                "准备用 edit_lines 或 insert_lines 按行修改文件前,先带 with_line_numbers=true 读一遍确认行号。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件路径,可以是相对路径或绝对路径",
                    },
                    "with_line_numbers": {
                        "type": "boolean",
                        "description": "是否在每行前面加上行号,默认 false。按行编辑前应设为 true",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_directory",
            "description": "获取你的工作区目录(绝对路径)。所有相对路径都以它为基准,文件操作也不能超出它。当用户问「我在哪」「当前目录是什么」时使用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "列出某个目录下的文件和子目录。目录名以 / 结尾,文件会附带字节大小。"
                "当用户问「这里有什么文件」「列一下目录」,或者你需要先找到文件名再去读取它时使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要列出的目录路径(相对工作区),省略则为工作区根目录",
                    },
                    "show_hidden": {
                        "type": "boolean",
                        "description": "是否包含以点开头的隐藏文件,默认 false",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "把文本内容写入工作区内的文件,父目录不存在会自动创建。"
                "默认不允许覆盖已存在的文件:如果文件已存在,调用会失败并提示你,"
                "此时应当先征求用户同意,确认后再带 overwrite=true 重新调用。"
                "只想在文件末尾补充内容时,用 append_file 而不是本工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "目标文件路径(相对工作区),例如 notes/todo.md",
                    },
                    "content": {
                        "type": "string",
                        "description": "要写入的完整文本内容",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "是否允许覆盖已存在的文件,默认 false。覆盖不可撤销,务必先得到用户确认",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_file",
            "description": (
                "在工作区内某个文件的末尾追加文本,文件不存在则自动创建。"
                "适合记日志、往清单里加条目这类场景,不会破坏已有内容。"
                "注意:本工具不会自动加换行,需要换行请在 content 里自己写 \\n。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "目标文件路径(相对工作区)",
                    },
                    "content": {
                        "type": "string",
                        "description": "要追加到文件末尾的文本",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_lines",
            "description": (
                "替换文件中指定行号区间的内容,只改这几行,文件其余部分原样保留。"
                "行号从 1 开始,start_line 和 end_line 都包含在内。"
                "把 content 留空('')就是删除这几行。"
                "改一两处内容时优先用本工具,不要用 write_file 整篇重写。"
                "调用前务必先用 read_file(with_line_numbers=true) 确认行号,不要凭记忆猜。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径(相对工作区)"},
                    "start_line": {
                        "type": "integer",
                        "description": "起始行号(从 1 开始,包含该行)",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "结束行号(包含该行);只改一行时填成和 start_line 相同",
                    },
                    "content": {
                        "type": "string",
                        "description": "替换进去的新内容,可以是多行;留空则删除这些行",
                    },
                },
                "required": ["path", "start_line", "end_line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "insert_lines",
            "description": (
                "在指定行之后插入新内容,不覆盖任何已有行。"
                "after_line=0 表示插入到文件最开头,after_line=5 表示插到第 5 行和第 6 行之间。"
                "只是往文件末尾补内容的话,用 append_file 更简单。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径(相对工作区)"},
                    "after_line": {
                        "type": "integer",
                        "description": "在这一行之后插入;0 表示插到文件最开头",
                    },
                    "content": {
                        "type": "string",
                        "description": "要插入的内容,可以是多行",
                    },
                },
                "required": ["path", "after_line", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": (
                "移动或重命名工作区内的文件、目录。同一目录内换个名字就是重命名,换到别的目录就是移动。"
                "目标的父目录不存在会自动创建;目标是一个已存在的目录时,会把源移动进去并沿用原名。"
                "默认不允许覆盖已存在的目标文件,需要覆盖时先征求用户同意再带 overwrite=true 重试。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "源文件或目录的路径(相对工作区)",
                    },
                    "destination": {
                        "type": "string",
                        "description": "目标路径(相对工作区);可以是新的文件名,也可以是已存在的目录",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "是否允许覆盖已存在的目标文件,默认 false。覆盖不可撤销,务必先得到用户确认",
                    },
                },
                "required": ["source", "destination"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": (
                "删除工作区内的一个文件。实际行为是移入 .trash/ 回收站而非物理删除,用户可以自行恢复。"
                "删除是破坏性操作:调用之前必须先取得用户的明确同意,不要自作主张删文件。"
                "本工具只能删单个文件,不能删目录。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要删除的文件路径(相对工作区)",
                    }
                },
                "required": ["path"],
            },
        },
    },
]


def dispatch(name: str, arguments: str) -> str:
    """执行工具。出错不崩溃,把错误信息回传给模型,它通常能自己改参数重试。"""
    func = TOOL_FUNCS.get(name)
    if func is None:
        return f"错误:不存在名为 {name} 的工具"
    try:
        return str(func(**json.loads(arguments)))
    except Exception as exc:  # noqa: BLE001 - 任何异常都应该反馈给模型
        return f"错误:{type(exc).__name__}: {exc}"


# ---------------- 核心循环 ----------------


def run(user_input: str, messages: list[dict]) -> str:
    messages.append({"role": "user", "content": user_input})

    for _ in range(MAX_STEPS):
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
        )
        msg = response.choices[0].message

        # 把模型这一轮的回复原样放回对话历史,messages 就是 agent 的全部记忆
        assistant_msg: dict = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            assistant_msg["tool_calls"] = [tc.model_dump() for tc in msg.tool_calls]
        messages.append(assistant_msg)

        # 模型不再要求调用工具 —— 说明它已经能回答了,循环结束
        if not msg.tool_calls:
            return msg.content or ""

        for call in msg.tool_calls:
            print(f"  ⚙ {call.function.name}({call.function.arguments})")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": dispatch(call.function.name, call.function.arguments),
                }
            )

    return f"(已达到最大步数 {MAX_STEPS},中止)"


def load_system_prompt() -> str:
    """从 system_prompt.md 读取系统提示词。改提示词只需要编辑那个文件。"""
    try:
        return PROMPT_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise SystemExit(f"找不到系统提示词文件:{PROMPT_FILE}") from None


def main() -> None:
    messages: list[dict] = [{"role": "system", "content": load_system_prompt()}]
    print("DeepSeek Agent 已启动,输入 exit 退出。\n")

    while True:
        try:
            user_input = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user_input:
            continue
        if user_input in {"exit", "quit"}:
            break

        print(f"AI > {run(user_input, messages)}\n")


if __name__ == "__main__":
    main()
