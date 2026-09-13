---
name: code-deobfuscation
description: 当用户要求还原、解密或分析被混淆/加密的代码时使用,例如「这段代码被加密了,帮我解开」「这个脚本到底干了什么」「分析这个混淆的 js/php/lua」。覆盖静态去混淆与 VM 内动态脱壳,面向可疑样本分析与自有代码恢复。
---

# 代码去混淆与解密

目标:把被混淆/加密的代码还原成可读形式,并给出**可复核的证据链**(每一层用了什么方法、中间产物是什么)。分析场景里,结果的可信度和分析过程一样重要。

适用:分析来源不明的可疑脚本(恶意 JS/PHP/Lua 等)、恢复自有或投稿来的代码、CTF 反混淆。

核心思路:**静态优先,动态兜底** —— 能静态剥就静态剥,卡住了才进 VM 动态执行。动态执行是最后手段,不是首选。

使用本技能的技术手段所产生的一切后果,由使用者自行承担。

## 安全铁律

- 不可信样本**只在 VM 内执行**。容器与宿主共享文件系统、隔离不足,禁止用容器跑样本;宿主绝对禁止。
- 执行样本前先让 VM 断网或伪造 DNS,阻断外联。
- 提取到的明文先落盘再分析,不要直接在宿主上打开或运行。

## 第 0 步:环境检查(动样本之前必做)

在 VM 里跑对应语言的样本前,先确认运行时是否就绪:

```bash
vm_run("command -v node php lua5.4")
```

| 语言 | 运行时 | Alpine 安装 |
| --- | --- | --- |
| JavaScript | node / npm | `apk add nodejs npm` |
| PHP | php | `apk add php` |
| Lua | lua5.4 | `apk add lua5.4` |

**缺运行时怎么办**:停下来,把「缺哪个、要装什么、装它做什么」告诉用户,**由用户决定是否安装**。不要自行 `apk add`。

注意 Alpine 默认把 community 源注释掉了,装 `php` / `npm` 这类包前得先启用:

```bash
sed -i 's|^#\(.*community\)|\1|' /etc/apk/repositories && apk update
```

## 已知环境坑(都踩过,别再踩)

- **`sh` 脚本必须是 LF 换行**:带 CRLF 会报 `illegal option -`。传进 VM 前先
  `sed -i 's/\r$//' 脚本` 最省心 —— 脚本可能来自任何地方,别假定它是 LF。
- **busybox 的 `mktemp` 模板必须以 `XXXXXX` 结尾**,不能带后缀 —— `/tmp/x.XXXXXX.php` 会报 `Invalid argument`。
- **Lua 5.4 的 `load` 显式传 `nil` 的 `env` 会把 `_ENV` 置空**(不等于省略参数)。写 wrapper 时要按实际参数个数转发,不能补 `nil`。

## 总流程

### 1. 侦查

- 看文件类型与后缀、大小,先用 `file`、看前几行。
- 找"加密迹象":超长字符串字面量、`eval`/`exec`/`Function`/`create_function`/`load`、`base64`/`gzip`/`hex`/`chr`、大整数数组、`\x`/`\u` 转义串。
- 判断语言,并猜混淆器家族(见各语言 reference)。

### 2. 分级

- **A 静态可解**:编码链、字符串数组加固定解码函数、简单 `eval` 包裹、字符码差分 —— 直接静态剥。
- **B 运行时自解**:只有跑起来才产出真正代码(自解密 loader、动态拼函数名)—— 进 VM 动态提取。
- **C 加壳或商用保护**:PyArmor、ionCube、Zend Guard、ConfuserEx、VMProtect 之类,自动化成功率低。识别到就如实说明难度、给出人工方向,别假装能一键解。

### 3. 静态剥离(A 类)

- 逐层解码编码链,优先用 `scripts/` 里的静态解码器,或直接写一段 Python。
- 对 JS 一类,做常量折叠、字符串数组还原、`eval` 内联。
- 每剥一层就存一份中间产物,作为证据链。

### 4. 动态提取(B 类,VM 内)

- 在 VM 里用对应语言的 hook 手段**旁路捕获**运行时解密出的代码(见各语言 reference):拦截 `eval`/`exec`/`compile`、替换字符串解码函数、监听文件写入等。
- 拿到明文即落盘;能停就停,不要在 VM 里继续跑后续逻辑。

### 5. 输出

按下面的统一格式写,**不要只丢一段代码**。

## 输出格式

```text
## 样本
- 来源/文件名、语言、大小、hash

## 混淆判定
- 家族/类型、分级(A/B/C)、判断依据

## 剥离过程(证据链)
- 第 1 层:方法 → 产物片段或落盘文件
- 第 2 层:……

## 还原结果
- 明文代码,或指向落盘文件

## 风险与结论
- 样本行为、可疑点、可信度说明、未解部分
```

## 语言指引

按需读对应 reference:

- JavaScript → `references/js.md`
- PHP → `references/php.md`
- Lua → `references/lua.md`

## 脚本

静态(只解码、不执行)可在容器或 VM 里跑;动态(会执行样本)**必须在 VM 内**跑,先用 `vm_push` 传进去。

| 脚本 | 用途 | 执行环境 |
| --- | --- | --- |
| `scripts/decode_static.py` | 通用编码链解码(自动搜索最优链) | 容器 / VM |
| `scripts/js_hook.js` | JS:拦 `eval`/`Function` 截明文 | VM(node) |
| `scripts/decode_php_static.py` | PHP:`gzinflate`/`base64`/`str_rot13` 等链 | 容器 / VM |
| `scripts/php_hook.sh` | PHP:改写 `eval` 为捕获函数后运行 | VM(php) |
| `scripts/decode_lua_static.py` | Lua:`string.char`/异或/hex/base64 | 容器 / VM |
| `scripts/lua_hook.lua` | Lua:覆盖 `load`/`dofile` 等截 chunk | VM(lua5.4) |

容器里这些脚本的路径是 `/skills/code-deobfuscation/scripts/…`(技能目录只读挂载);VM 里则需先 `vm_push`。
