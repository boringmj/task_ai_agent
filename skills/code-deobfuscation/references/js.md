# JavaScript 去混淆参考

## 常见混淆形态与判断

| 形态 | 典型特征 | 分级 |
| --- | --- | --- |
| 编码链 | `atob`/`unescape`/`\x`/`\u`/hex/`String.fromCharCode`,层层包裹 | A |
| 简单 eval 包裹 | `eval("…")` 里是明文或一次编码 | A |
| Dean Edwards packer | `eval(function(p,a,c,k,e,d){…})` | A |
| `document.write` + 差分编码 | `document.write(function(a){a=unescape(a);…})("…")`,字符码逐位差分 | A |
| JSFuck | 仅由 `[]()!+` 六种字符组成 | A |
| jjencode / aaencode | 纯符号或颜文字 | A |
| 字符串数组混淆 | 大量 `_0x1a2b` 变量 + 数组 + 解码函数(javascript-obfuscator / obfuscator.io) | B |
| 控制流扁平化 | `while(!![])` 配大 `switch`,`case` 顺序被打乱 | B |
| 自定义加密自解 | 大整数数组当 key,`RC4`/异或后拼源码再执行 | B |
| 反调试 | `debugger` 循环、时间差检测、`console` 检测 | 需绕过 |

判断顺序:先 beautify 再读结构;出现"数组 + 一个被反复调用的函数"基本就是字符串数组混淆;看到 `eval`/`Function`/`atob` 就往动态方向准备。

## 静态手法(A 类)

1. 用 `npx js-beautify` 或 Python 的 `jsbeautifier` 格式化,恢复缩进。
2. 定位字符串解码函数,把它单独抽出来在 node 里**只跑解码部分**,批量还原字符串数组。
3. 对 `eval("…")` 里是明文字符串的,直接取出内层源码;是编码的,先解一层再看。
4. Dean Edwards packer:照搬 `p,a,c,k,e,d` 参数,把 `eval` 换成 `print`,直接得到原始 JS。
5. 每剥一层存一份文件,别连锁式覆盖。
6. **差分编码**(`document.write` 型):先 `unescape`,再按「首字符码 = `a[0]` − 字符串长度」「其后每位 = `a[i]` − 前一位输出字符码」反解。注意 JS 的 `fromCharCode` 是 16 位,减法要 `& 0xFFFF`。纯算术,不必执行。

> 注意:抽解码函数单独跑,仍然是在**执行样本代码**。这一步要么在 VM 里做,要么人工审过确认无副作用后再做。

## 动态手法(B 类,VM 内)

字符串数组混淆、控制流扁平化、自解密 loader 只能靠跑起来拿结果。可行路线:

1. **旁路替换 `eval`/`Function`**:在 harness 里覆盖 `global.eval` 和 `Function` 构造器,把收到的源码写盘、再交给原函数执行。适用于"最终一定走 eval/Function"的样本。
2. **hook 字符串解码函数**:先统计哪个函数被调用最多、入参是整数、返回值像字符串,套一层 wrapper 记录 `(入参 → 返回值)` 映射,批量还原。
3. **末路:直接 dump 内存/运行时**:在关键点(`document.write`/`innerHTML`/`fs.writeFile`)下断或替换,截获解密后的内容。
4. 拿到明文**立刻落盘**,别在 VM 里继续跑后续逻辑。

配套脚本见 `scripts/js_hook.js`(wrapper harness)与 `scripts/decode_static.py`(编码链解码)。

## 反调试绕过

- `debugger` 循环:用 sed 全局删掉 `debugger;`,或把 `setInterval` 里带 `debugger` 的回调整个移除。
- 时间检测:`Date.now`/`performance.now` 差值判断是否被调试 —— 在 harness 里把它改成恒定推进或放慢。
- `console` 检测:给 `console` 各方法塞空函数。
- 环境检测(`window`/`document` 缺失、UA):用 `node:vm` 造一个最小 DOM/浏览器环境垫片。

## 无 node 时的降级

VM 里没装 node 时,**不要自己装**,按 SKILL.md 第 0 步停下来问用户。静态的 beautify 和部分解码链(纯 Python)仍可先做,把结果和"缺 node、动态部分待跑"一并汇报。
