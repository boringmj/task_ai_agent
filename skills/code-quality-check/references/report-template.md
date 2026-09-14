# 报告与 quality JSON 模板

## 报告模板

报告默认存到工作区,文件名 `<目标名>-代码质量.md`。结构如下(外层围栏里是示例正文):

````markdown
# <目标名> 代码质量检查

## 结论摘要

- **检查范围**:`<路径>`,N 个文件,主要语言 <语言>
- **问题总数**:N(其中 L3 及以上:M)
- **最该先动的三处**:
  1. `src/db.py:88` **L4** —— 写库失败被吞,调用方以为成功
  2. `src/order.py:210` **L3** —— 捕获过宽且静默,异常路径下状态不一致
  3. `src/utils.py:12` **L2** —— 同一段校验在 4 处复制,改一处漏三处

## 现在改(L3 / L4)

### 1. `src/db.py:88` L4 —— 写库失败被吞,调用方以为成功

- **问题**:`except Exception: pass` 吞掉了 `write_record` 的所有异常。
- **为什么在这个上下文里是问题**:该函数位于下单主流程,调用方依赖它抛错来触发回滚;吞掉后订单会记入而库存不扣。
- **证据**:

  ```python
  try:
      write_record(row)
  except Exception:
      pass
  ```

- **建议**:至少记录并重抛;若确实要容错,改成返回错误并以显式分支处理。

### 2. ...

## 排期(L2)

### 1. `src/utils.py:12` L2 —— 校验逻辑四处复制

（同上格式:问题 / 为什么是问题 / 证据 / 建议）

## 不建议动(L1 与经重要性下调的条目)

- `src/legacy/etl.py:31` L1 —— 命名不达意,但该模块即将下线,改动收益低。

## 未覆盖范围与待确认

- 未纳入:`tests/`(夹具相似不按重复计)、`vendor/`(第三方代码)。
- 待确认:`src/pay.py:56` 的 `Any` 是否由外部库签名决定,若是则不算问题。

## 方法与局限

- 用 `scripts/quality_scan.py` 扫描候选 + 人工逐条复核上下文。
- 只覆盖**静态可见**的内容;运行时行为、动态分派、生成代码未纳入。
- 等级为判断结果,`LX` 表示上下文不足、需人工确认。
````

## quality JSON 字段

`scripts/quality_scan.py --json <文件>` 输出的结构。**脚本给的是候选与初判**,`level` 字段是**预估等级**,最终等级由复核后人工调整,报告里以报告为准。

```json
{
  "target": "clones/foo",
  "summary": {
    "files_scanned": 42,
    "by_level": { "L4": 1, "L3": 3, "L2": 7, "L1": 12 },
    "by_dimension": { "function-size": 9, "error-handling": 4 }
  },
  "findings": [
    {
      "dimension": "error-handling",
      "level": "L3",
      "rule": "swallowed-exception",
      "file": "src/db.py",
      "line": 88,
      "end_line": 90,
      "snippet": "except Exception:\n    pass",
      "measured": {},
      "note": "捕获后无任何处理"
    }
  ],
  "duplicates": [
    {
      "fingerprint": "a1b2c3",
      "lines": 24,
      "test": false,
      "occurrences": [
        { "file": "src/a.py", "line": 12 },
        { "file": "src/b.py", "line": 40 }
      ]
    }
  ]
}
```

字段说明:

| 字段 | 含义 |
| --- | --- |
| `dimension` | 七个维度之一:`function-size` / `naming` / `error-handling` / `resource` / `duplication` / `typing` / `testing` |
| `level` | 预估等级 `L1`~`L4`;`L0` 不入档 |
| `rule` | 命中的规则名,便于回查与误报统计 |
| `measured` | 度量值(如 `{"func_lines": 120, "nesting": 5}`),无则为空对象 |
| `note` | 一句话说明为何命中 |
| `confidence` | 脚本初判的可信度:`high`(AST 精确)/ `medium` / `low`(正则粗筛);**等级仍以复核为准** |

`duplicates` 项字段:`fingerprint`(内容指纹)、`lines`(块行数)、`test`(是否全部来自测试文件)、`occurrences`(各出现位置,每个文件一项)。
