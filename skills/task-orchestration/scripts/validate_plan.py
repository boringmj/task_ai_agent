#!/usr/bin/env python3
"""校验任务编排的计划文件(plan.md)。

纯标准库、可离线跑。检查项:
  1. 至少有一个步骤
  2. 步骤 ID 唯一
  3. 状态合法(todo / doing / done / blocked)
  4. 验收标准非空 —— 空验收等于没想清楚,不许开工
  5. 依赖引用的步骤确实存在
  6. 依赖图无环

在此基础上算出**当前就绪的步骤**:自身是 todo,且它的所有依赖都已经是 done。
一个任务同一时刻应该只有一个"就绪步骤"值得动手,脚本会把它标出来。

用法:
    python3 validate_plan.py <plan.md> [--json]

退出码:0 = 计划合法;1 = 有问题或文件读不了。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

STATUSES = ("todo", "doing", "done", "blocked")
STEP_RE = re.compile(r"^###\s+(\S+)\s*(.*)$")
FIELD_RE = re.compile(r"^\s*[-*]\s*(依赖|状态|验收|产出)\s*[:：]\s*(.*)$")
NONE_WORDS = {"", "无", "-", "—", "none", "None", "n/a", "NA"}
SPLIT_RE = re.compile(r"[,、/;；\s]+")


def parse_plan(text: str) -> tuple[dict, list[dict]]:
    """解析为 (元信息, 步骤列表)。只认固定结构,别的行一律忽略。"""
    meta: dict[str, str] = {}
    steps: list[dict] = []
    in_steps = False
    cur: dict | None = None

    for lineno, line in enumerate(text.splitlines(), 1):
        if line.startswith("## "):
            in_steps = line.strip().startswith("## 步骤")
            cur = None
            continue

        if not in_steps:
            # 只为报错时给个上下文:抓一级标题当任务名
            if line.startswith("# ") and "name" not in meta:
                meta["name"] = _clean_name(line[2:].strip())
            continue

        m = STEP_RE.match(line)
        if m:
            cur = {
                "id": m.group(1),
                "title": m.group(2).strip(),
                "deps": [],
                "status": "",
                "accept": "",
                "output": "",
                "line": lineno,
            }
            steps.append(cur)
            continue

        if cur is None:
            continue

        m = FIELD_RE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if key == "依赖":
            cur["deps"] = (
                [] if value in NONE_WORDS
                else [d for d in SPLIT_RE.split(value) if d and d not in NONE_WORDS]
            )
        elif key == "状态":
            cur["status"] = value
        elif key == "验收":
            cur["accept"] = value
        elif key == "产出":
            cur["output"] = value

    return meta, steps


def find_cycles(steps: list[dict]) -> list[str]:
    """返回环的描述列表(空 = 无环)。"""
    by_id = {s["id"]: s for s in steps}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {sid: WHITE for sid in by_id}
    cycles: list[str] = []

    def visit(sid: str, stack: list[str]) -> None:
        color[sid] = GRAY
        stack.append(sid)
        for dep in by_id[sid]["deps"]:
            if dep not in by_id:
                continue                      # 悬空依赖另有专门的检查来报
            if color[dep] == GRAY:
                path = stack[stack.index(dep):] + [dep]
                cycles.append(" → ".join(path))
            elif color[dep] == WHITE:
                visit(dep, stack)
        stack.pop()
        color[sid] = BLACK

    for sid in by_id:
        if color[sid] == WHITE:
            visit(sid, [])
    return cycles


def analyze(text: str) -> dict:
    meta, steps = parse_plan(text)
    errors: list[str] = []
    warnings: list[str] = []

    if not steps:
        errors.append("没有解析到任何步骤 —— 检查是否有 `## 步骤` 小节,以及步骤是否写成 `### S1 标题` 形式")

    ids = [s["id"] for s in steps]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        errors.append(f"步骤 ID 重复:{'、'.join(dupes)}(ID 必须唯一,依赖关系靠它引用)")

    known = set(ids)
    for s in steps:
        where = f"{s['id']}(第 {s['line']} 行)"
        if not s["status"]:
            errors.append(f"{where} 缺 `状态` 字段")
        elif s["status"] not in STATUSES:
            errors.append(f"{where} 状态非法:`{s['status']}`,只能是 {'/'.join(STATUSES)}")
        if not s["accept"]:
            errors.append(f"{where} 验收标准为空 —— 写不出可判定的验收,说明这步没想清楚")
        for dep in s["deps"]:
            if dep not in known:
                errors.append(f"{where} 依赖了不存在的步骤 `{dep}`")
            if dep == s["id"]:
                errors.append(f"{where} 依赖了自己")

    for cycle in find_cycles(steps):
        errors.append(f"依赖成环:{cycle}")

    status_of = {s["id"]: s["status"] for s in steps}
    ready: list[dict] = []
    waiting: list[dict] = []
    blocked_by: list[dict] = []

    for s in steps:
        if s["status"] != "todo":
            continue
        unmet = [d for d in s["deps"] if status_of.get(d) != "done"]
        if not unmet:
            ready.append(s)
        elif any(status_of.get(d) == "blocked" for d in unmet):
            blocked_by.append({**s, "unmet": unmet})
        else:
            waiting.append({**s, "unmet": unmet})

    return {
        "name": meta.get("name", ""),
        "steps": steps,
        "errors": errors,
        "warnings": warnings,
        "ready": ready,
        "waiting": waiting,
        "blocked_by": blocked_by,
    }


def _clean_name(raw: str) -> str:
    """标题里已经写了「计划:」的话,别再重复加一遍。"""
    for prefix in ("计划:", "计划：", "计划 "):
        if raw.startswith(prefix):
            return raw[len(prefix):].strip()
    return raw


def _w(text: str) -> int:
    """按终端显示宽度计算:中日韩字符占 2 列,否则列宽对不齐。"""
    return sum(2 if ord(c) > 0x2E7F else 1 for c in text)


def _pad(text: str, target: int) -> str:
    """按显示宽度左对齐补空格。"""
    return text + " " * max(1, target - _w(text))


def render(r: dict) -> str:
    out: list[str] = []
    name = r["name"] or "(未命名计划)"
    out.append(f"{name}   共 {len(r['steps'])} 个步骤")

    if r["steps"]:
        width = max(_w(s["id"]) for s in r["steps"]) + 2
        ready_ids = {s["id"] for s in r["ready"]}
        for s in r["steps"]:
            mark = "← 就绪" if s["id"] in ready_ids else ""
            deps = f"依赖 {','.join(s['deps'])}" if s["deps"] else "无依赖"
            status = s["status"] or "(缺状态)"
            title = s["title"] or "(无标题)"
            out.append(f"  {_pad(s['id'], width)}{_pad(title, 28)}{_pad(status, 8)}{deps}  {mark}")

    if r["errors"]:
        out.append("")
        out.append(f"✗ {len(r['errors'])} 个问题,先修完再动手:")
        out.extend(f"  - {e}" for e in r["errors"])
        return "\n".join(out)

    out.append("")
    if r["ready"]:
        nxt = r["ready"][0]
        out.append(f"✓ 计划合法。当前就绪步骤:{nxt['id']} {nxt['title']}")
        out.append(f"  验收:{nxt['accept']}")
        if len(r["ready"]) > 1:
            others = "、".join(s["id"] for s in r["ready"][1:])
            out.append(f"  (另有 {others} 也可执行,但一次只做一步)")
    else:
        done = [s for s in r["steps"] if s["status"] == "done"]
        if len(done) == len(r["steps"]):
            out.append("✓ 计划合法。所有步骤均为 done —— 去跑整体「完成标准」确认收尾。")
        else:
            out.append("✓ 计划合法,但当前没有可执行的步骤:")
            for s in r["blocked_by"]:
                out.append(f"  - {s['id']} 被阻塞,等待 {','.join(s['unmet'])}")
            for s in r["waiting"]:
                out.append(f"  - {s['id']} 等待 {','.join(s['unmet'])}")
            if r["blocked_by"]:
                out.append("  有 blocked 的步骤 —— 按技能里的规矩:记进「阻塞与问题」并停下来问用户。")

    if r["warnings"]:
        out.append("")
        out.extend(f"  ! {w}" for w in r["warnings"])
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="校验任务编排的计划文件")
    ap.add_argument("plan", help="plan.md 的路径(在容器里是 /workspace/plans/...)")
    ap.add_argument("--json", action="store_true", help="输出 JSON,便于程序处理")
    args = ap.parse_args()

    path = Path(args.plan)
    if not path.is_file():
        print(f"错误:找不到计划文件 {path}", file=sys.stderr)
        return 1
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"错误:读不了 {path}:{exc}", file=sys.stderr)
        return 1

    result = analyze(text)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render(result))

    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
