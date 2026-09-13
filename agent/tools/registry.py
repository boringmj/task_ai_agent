"""工具注册表:自动发现本包下的工具模块,收集工具函数与它们的 schema。

每个工具在**自己所在的模块**里,用 `@tool(description=..., parameters=...)` 声明自己的
描述与参数(也就是模型看到的提示词),不再集中到本文件里维护。
本模块在导入时自动扫描 agent/tools/ 下的所有模块并收集 —— 新增一个工具只要在对应域模块
里写函数 + 装饰器,不必回来改这里。

约定:模块名以下划线开头视为私有,不参与扫描;工具名默认取函数名,也可以在装饰器里用 name= 覆盖。
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

_TOOL_FUNCS: dict[str, object] = {}   # 工具名 -> 实现函数
_TOOLS: list[dict] = []               # 工具名 -> JSON schema(发给模型看)


def tool(description: str, parameters: dict | None = None, name: str | None = None):
    """把一个函数登记为模型可调用的工具,并附上它的 JSON schema。

    description 写清楚"什么时候用它",模型选工具主要靠这段文字;parameters 是
    JSON Schema 形式的参数说明(缺省表示无参数工具)。
    """
    def decorate(fn):
        tool_name = name or fn.__name__
        if tool_name in _TOOL_FUNCS:
            raise RuntimeError(f"工具名重复:{tool_name}(来自 {fn.__module__})")
        _TOOL_FUNCS[tool_name] = fn
        _TOOLS.append({
            "type": "function",
            "function": {
                "name": tool_name,
                "description": description,
                "parameters": parameters or {"type": "object", "properties": {}},
            },
        })
        return fn                      # 装饰器不改变函数本身,原有直接调用照常可用
    return decorate


def _discover() -> None:
    """导入本包下所有工具模块,触发它们的 @tool 注册。"""
    pkg = __name__.rsplit(".", 1)[0]            # "agent.tools"
    here = Path(__file__).parent
    for path in sorted(here.glob("*.py")):
        if path.stem.startswith("_") or path.stem == Path(__file__).stem:
            continue
        importlib.import_module(f"{pkg}.{path.stem}")
    _check_no_private_tools()


def _check_no_private_tools() -> None:
    """注册表体检:工具名不该以下划线开头。

    抓的是**装饰器错位** —— 往 `@tool` 和它的 `def` 之间插辅助函数时,装饰器会落到辅助
    函数头上,原工具反过来变成裸函数。症状很隐蔽:某个天天在用的工具莫名从清单里消失,
    多出一个没人认识的私有名,模型还会照着那个错名字去调。

    这个坑踩过三次(vm_status、read_file、delete_file 各一次),每次都是靠人肉核对工具表
    才发现。项目里所有工具都是公开名,私有名只可能是辅助函数 —— 所以一条断言就能兜住。
    """
    bad = sorted(n for n in _TOOL_FUNCS if n.startswith("_"))
    if bad:
        raise RuntimeError(
            f"这些工具名以下划线开头:{bad} —— 几乎可以肯定是 @tool 装饰器落错了位置"
            f"(插辅助函数时被夹在了 @tool 和原 def 之间,把装饰器抢走了)。"
        )


_discover()

# 对外的只读视图(名字保持不变,loop/llm 无需改动)
TOOL_FUNCS = _TOOL_FUNCS
TOOLS = _TOOLS


def dispatch(name: str, arguments: str) -> str:
    """执行工具。出错不崩溃,把错误信息回传给模型,它通常能自己改参数重试。"""
    func = TOOL_FUNCS.get(name)
    if func is None:
        return f"错误:不存在名为 {name} 的工具"
    try:
        return str(func(**json.loads(arguments)))
    except Exception as exc:  # noqa: BLE001 - 任何异常都应该反馈给模型
        return f"错误:{type(exc).__name__}: {exc}"
