# PHP 去混淆参考

## 常见混淆形态与判断

| 形态 | 典型特征 | 分级 |
| --- | --- | --- |
| 简单编码链 | `eval(gzinflate(base64_decode("…")))` | A |
| 多层嵌套 | `base64` / `gzinflate` / `str_rot13` / `urldecode` 层层套 | A |
| 十六进制字符串 | `"\x65\x76\x61\x6c"`(其实就是 `eval`) | A |
| 变量函数 | `$a = 'eval'; $a($code);` 或 `$GLOBALS['x'](…)` | A/B |
| 单字符拼接 | 用一堆单字符变量拼出函数名或代码 | B |
| 混淆器输出 | `$O00OO0 = urldecode(…); … eval($O00OO0($O00OO1));` | B |
| 加壳 | ionCube / Zend Guard / SourceGuardian | C |

PHP 的"加密"绝大多数是**编码链**,直接静态就能剥,很少需要动态。

## 静态手法(A 类)

把 PHP 的解码函数一对一映射到 Python:

- `base64_decode` → `base64.b64decode`
- `gzinflate` → `zlib.decompress(data, -15)`
- `gzuncompress` / `gzdecode` → `zlib.decompress` / `gzip.decompress`
- `str_rot13` → 字母 rot13
- `urldecode` → `urllib.parse.unquote`
- `strrev` → 反转
- `"\xNN"` → 该字节

遇到 `eval(gzinflate(base64_decode($x)))` 这种,把最内层的字符串取出来,按链反着解,再对内层结果继续。用 `scripts/decode_php_static.py`,或直接写几行 Python。

每剥一层存一份中间产物。

## 动态手法(B 类,VM 内)

`eval` / `assert` 在 PHP 里是**语言结构,不是函数**,不能像 JS 那样覆盖。主流做法是**源码改写**:

1. 把 `eval(` 替换成自定义捕获函数 `__dump(`,并注入定义:`__dump($code)` 先把 `$code` 落盘、再调用原逻辑(或直接 `eval`)。
2. 同样处理 `assert(`(`create_function`、旧版 `preg_replace` 的 `/e` 修饰符)。
3. 变量函数 `$f = '…'; $f()` 较难自动化,可先在头部 dump 相关变量值,再人工看。
4. 改写务必保证语法没被破坏(`eval` 的参数是一个表达式)。

配套:用 `scripts/php_hook.sh` 做「改写 + 在 VM 里跑」。

## 加壳与识别(C 类)

- ionCube:文件里含 `ionCube` 特征串或 `<?php //004xx` 之类头部。
- Zend Guard:含 `Zend` 编码头。
- SourceGuardian:含 `sg_load` / `SourceGuardian`。

识别到就如实说明需要专用工具或人工,别假装能一键解。

## 注意

- PHP 混淆常伴随 `@` 抑制、`exit`/`die` 提前结束、`php_sapi_name` 环境检测。
- 执行任何样本都在 VM 内、断网。
