#!/usr/bin/env python3
"""PHP 代码质量度量 —— code-quality-check 技能的 PHP 补充扫描器。

技能自带的 quality_scan.py 对 PHP 只覆盖空 catch 与重复块(它的 AST 精确度量
只支持 Python)。本脚本用 tree-sitter-php 补齐这几个维度:

- 函数规模:行数、嵌套深度、圈复杂度(近似)、参数个数
- 类型安全:缺参数类型、缺返回类型(自动排除魔术方法)
- 错误处理:空 catch、只处理一两条语句且不重抛的 catch

输出的是**候选**,不是结论 —— 误报难免(测试夹具、生成代码、有意为之的
外观模式),必须按技能的"四条铁律"读上下文复核后再写进报告。

用法:

    python3 php_quality_scan.py <目标路径> [--json out.json] [--top 20]
    python3 php_quality_scan.py <目标路径> --min-lines 80 --min-depth 4 --min-cyclo 15

依赖(容器里一次性安装,之后持久保留):

    pip install --target /workspace/.pylibs tree_sitter tree_sitter_php
"""

import argparse
import collections
import json
import os
import sys

sys.path.insert(0, "/workspace/.pylibs")  # 容器内第三方库的持久位置

try:
    from tree_sitter import Language, Parser
    import tree_sitter_php as tsp
except ImportError:
    sys.exit(
        "缺少依赖。请先运行:\n"
        "  pip install --target /workspace/.pylibs tree_sitter tree_sitter_php"
    )

PHP = Language(tsp.language_php())

FUNC_TYPES = {"method_declaration", "function_definition"}
PARAM_TYPES = {"simple_parameter", "variadic_parameter", "property_promotion_parameter"}
TYPE_NODES = {
    "primitive_type", "named_type", "optional_type", "union_type",
    "intersection_type", "bottom_type", "disjunctive_normal_form_type",
}
# 嵌套深度按控制结构计
CTRL = {
    "if_statement", "for_statement", "foreach_statement", "while_statement",
    "do_statement", "switch_statement", "try_statement", "match_expression",
    "conditional_expression",
}
# 圈复杂度近似:每个判定点 +1
CYCLO = {
    "if_statement", "elseif_clause", "for_statement", "foreach_statement",
    "while_statement", "do_statement", "case_statement", "catch_clause",
    "match_expression", "conditional_expression",
}
# 这些魔术方法不能声明返回类型,不该算"缺标注"
MAGIC = {"__construct", "__destruct", "__clone", "__wakeup", "__sleep", "__set_state"}
SKIP_IN_CATCH = {"{", "}", "comment"}


def walk(node):
    yield node
    for child in node.children:
        yield from walk(child)


def max_depth(node, depth=0):
    best = depth
    for child in node.children:
        next_depth = depth + 1 if child.type in CTRL else depth
        best = max(best, max_depth(child, next_depth))
    return best


def parse(path, parser):
    with open(path, "rb") as fh:
        src = fh.read()
    return src, parser.parse(src)


def collect_functions(root_dir, parser, src_cache):
    """返回 (functions, catches)。"""
    functions, catches = [], []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = [d for d in dirnames if d not in (".git", "vendor", "node_modules")]
        for name in sorted(filenames):
            if not name.endswith(".php"):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root_dir)
            try:
                src, tree = parse(path, parser)
            except Exception as exc:  # noqa: BLE001 - 单个文件解析失败不该中断整个扫描
                print(f"  ! 解析失败 {rel}: {exc}", file=sys.stderr)
                continue
            src_cache[rel] = src
            for node in walk(tree.root_node):
                if node.type in FUNC_TYPES:
                    functions.append(_function_record(node, src, rel))
                elif node.type == "catch_clause":
                    catches.append(_catch_record(node, src, rel))
    return [f for f in functions if f], catches


def _function_record(node, src, rel):
    name_node = node.child_by_field_name("name")
    params = node.child_by_field_name("parameters")
    body = next((c for c in node.children if c.type == "compound_statement"), None)
    if body is None:  # 抽象方法 / 接口声明
        return None
    param_list = [c for c in (params.children if params else []) if c.type in PARAM_TYPES]
    typed = sum(1 for c in param_list if _has_type(c))
    return {
        "name": src[name_node.start_byte:name_node.end_byte].decode("utf-8", "replace") if name_node else "?",
        "file": rel,
        "line": node.start_point[0] + 1,
        "end_line": node.end_point[0] + 1,
        "lines": node.end_point[0] - node.start_point[0] + 1,
        "depth": max_depth(body),
        "params": len(param_list),
        "typed_params": typed,
        "has_return_type": node.child_by_field_name("return_type") is not None,
        "cyclo": 1 + sum(
            1 for x in walk(body)
            if x.type in CYCLO
            or (x.type == "binary_expression" and any(ch.type in ("&&", "||") for ch in x.children))
        ),
    }


def _has_type(param_node):
    return any(child.type in TYPE_NODES for child in param_node.children) or (
        param_node.children and param_node.children[0].type not in ("variable_name", "$")
    )


def _catch_record(node, src, rel):
    body = next((c for c in node.children if c.type == "compound_statement"), None)
    statements = [c for c in (body.children if body else []) if c.type not in SKIP_IN_CATCH]
    head = src[node.start_byte:node.start_byte + 120].decode("utf-8", "replace").split("\n")[0]
    return {
        "file": rel,
        "line": node.start_point[0] + 1,
        "type": head.split("(")[1].split(")")[0].strip() if "(" in head else "?",
        "statements": len(statements),
        "kinds": [s.type for s in statements],
        "head": head.strip(),
    }


def build_findings(functions, catches, thresholds):
    findings = []
    for fn in functions:
        loc = {"file": fn["file"], "line": fn["line"], "end_line": fn["end_line"]}
        if fn["lines"] > thresholds["lines"]:
            findings.append({
                "dimension": "function-size", "level": "L2", "rule": "long-function",
                "confidence": "high", "note": f"{fn['lines']} 行,超过 {thresholds['lines']} 行阈值",
                "measured": {"func_lines": fn["lines"], "nesting": fn["depth"], "cyclo": fn["cyclo"]},
                "symbol": fn["name"], **loc,
            })
        if fn["depth"] >= thresholds["depth"]:
            findings.append({
                "dimension": "function-size", "level": "L2", "rule": "deep-nesting",
                "confidence": "high", "note": f"嵌套 {fn['depth']} 层,常可用早返回(guard clause)化解",
                "measured": {"nesting": fn["depth"], "func_lines": fn["lines"]},
                "symbol": fn["name"], **loc,
            })
        if fn["cyclo"] >= thresholds["cyclo"]:
            findings.append({
                "dimension": "function-size", "level": "L2", "rule": "high-cyclomatic",
                "confidence": "high", "note": f"圈复杂度约 {fn['cyclo']},分支规则可能已成表",
                "measured": {"cyclo": fn["cyclo"], "func_lines": fn["lines"]},
                "symbol": fn["name"], **loc,
            })
        if fn["params"] >= thresholds["params"]:
            findings.append({
                "dimension": "function-size", "level": "L1", "rule": "many-params",
                "confidence": "high",
                "note": f"{fn['params']} 个参数;若是值对象全可选默认参,则不算问题",
                "measured": {"params": fn["params"]}, "symbol": fn["name"], **loc,
            })
        if fn["params"] > 0 and fn["typed_params"] == 0:
            findings.append({
                "dimension": "typing", "level": "L2", "rule": "missing-param-type",
                "confidence": "high", "note": "参数完全没有类型标注",
                "measured": {"params": fn["params"]}, "symbol": fn["name"], **loc,
            })
        if not fn["has_return_type"] and fn["name"] not in MAGIC:
            findings.append({
                "dimension": "typing", "level": "L1", "rule": "missing-return-type",
                "confidence": "high", "note": "无返回类型标注",
                "measured": {"func_lines": fn["lines"]}, "symbol": fn["name"], **loc,
            })

    for ct in catches:
        loc = {"file": ct["file"], "line": ct["line"]}
        if ct["statements"] == 0:
            findings.append({
                "dimension": "error-handling", "level": "L3", "rule": "swallowed-exception",
                "confidence": "high", "note": "空 catch,异常被吞(若无注释说明意图,按 L3 起报)",
                "measured": {}, **loc,
            })
        elif ct["statements"] <= 2 and not any(
            k in ("throw_expression", "return_statement", "break_statement", "continue_statement")
            for k in ct["kinds"]
        ):
            findings.append({
                "dimension": "error-handling", "level": "L2", "rule": "handled-without-rethrow",
                "confidence": "medium", "note": "捕获后只做了少量处理,未重抛也未返回(需看是否本意)",
                "measured": {"statements": ct["statements"]}, **loc,
            })
    return findings


def main():
    ap = argparse.ArgumentParser(description="PHP 代码质量度量(tree-sitter)")
    ap.add_argument("target", help="目标目录或 .php 文件")
    ap.add_argument("--json", metavar="FILE", help="把候选写成 JSON(字段与技能模板对齐)")
    ap.add_argument("--top", type=int, default=15, help="每类最多打印几条,默认 15")
    ap.add_argument("--min-lines", type=int, default=80)
    ap.add_argument("--min-depth", type=int, default=4)
    ap.add_argument("--min-cyclo", type=int, default=15)
    ap.add_argument("--min-params", type=int, default=6)
    args = ap.parse_args()

    target = args.target
    parser = Parser(PHP)
    src_cache = {}
    if os.path.isfile(target):
        if not target.endswith(".php"):
            sys.exit("目标文件不是 .php")
        src, tree = parse(target, parser)
        rel_root = os.path.dirname(target) or "."
        functions, catches = [], []
        for node in walk(tree.root_node):
            if node.type in FUNC_TYPES:
                rec = _function_record(node, src, os.path.relpath(target, rel_root))
                if rec:
                    functions.append(rec)
            elif node.type == "catch_clause":
                catches.append(_catch_record(node, src, os.path.relpath(target, rel_root)))
    else:
        functions, catches = collect_functions(target, parser, src_cache)

    thresholds = {"lines": args.min_lines, "depth": args.min_depth,
                  "cyclo": args.min_cyclo, "params": args.min_params}
    findings = build_findings(functions, catches, thresholds)

    prod = [f for f in functions if not f["file"].startswith("tests/")]
    print(f"PHP 代码质量度量 —— {target}")
    print(f"函数/方法(有体):{len(functions)}(生产 {len(prod)} / 测试 {len(functions) - len(prod)})")
    print(f"catch 子句:{len(catches)}  候选命中:{len(findings)} 条")

    by_level = collections.Counter(f["level"] for f in findings)
    by_dim = collections.Counter(f["dimension"] for f in findings)
    print(f"  按等级:{dict(sorted(by_level.items()))}")
    print(f"  按维度:{dict(by_dim.most_common())}")

    prod_names = {(f["file"], f["line"]) for f in prod}
    def sort_key(f):
        order = {"L3": 0, "L2": 1, "L1": 2}
        return (order.get(f["level"], 3), -f.get("measured", {}).get("cyclo", 0))
    shown = [f for f in findings if (f["file"], f["line"]) in prod_names]
    print("\n生产代码命中(测试里的候选归入 JSON):")
    for f in sorted(shown, key=sort_key)[: args.top]:
        m = f.get("measured", {})
        extra = " ".join(f"{k}={v}" for k, v in m.items())
        print(f"  [{f['level']}] {f['dimension']:15s} {f['file']}:{f['line']} "
              f"{f.get('symbol', '')} — {f['note']} {extra}".rstrip())
    if len(shown) > args.top:
        print(f"  … 另有 {len(shown) - args.top} 条")

    print("\n以上为候选,必须读上下文复核后再写进报告(办法见本技能的 SKILL.md)。")

    if args.json:
        payload = {
            "target": target,
            "summary": {
                "files_scanned": len(src_cache) or 1,
                "functions": len(functions),
                "by_level": dict(sorted(by_level.items())),
                "by_dimension": dict(by_dim.most_common()),
            },
            "findings": findings,
            "catches": catches,
        }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"JSON 已写出:{args.json}")


if __name__ == "__main__":
    main()
