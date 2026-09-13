# Lua 去混淆参考

## 常见混淆形态与判断

| 形态 | 典型特征 | 分级 |
| --- | --- | --- |
| 编码链 | `loadstring(base64.decode("…"))` / `load(…)` | A |
| 字符码数组 | `string.char(104, 101, …)` | A |
| 异或 / 自定义加密 | 字符串与 key 逐字节异或后再 `load` | B |
| 字节码 | LuaJIT bytecode 或 `luac` 编译产物 | B/C |
| 加壳 | 少见 | C |

Lua(lua5.4)里 `loadstring` 已移除,统一用 `load(chunk)`。

## 静态手法(A 类)

- base64 / hex / `string.char` 数组 / 异或,都用 Python 复现即可。
- `string.char(a, b, …)` → 逗号分隔的码位直接转字节。
- 异或:找出 key(常量或短字符串),逐字节还原。
- 字节码:标准 `luac` chunk 用 `luac -l` 反汇编;LuaJIT 用 `luajit -bl`。

## 动态手法(B 类,VM 内)

Lua 的载入点很集中,覆盖这几个全局函数就能截获运行时拼出的代码:

- `load`(5.2+,含 5.4)、`loadstring`(5.1 兼容名)、`dofile`、`loadfile`、`require`。

做法:在 harness 里包一层,把收到的 chunk 字符串落盘,再转交原函数执行。见 `scripts/lua_hook.lua`。

## 注意

- Lua 里可能用 `setfenv` / `_ENV` 改环境来藏东西,5.4 里是 `_ENV`。
- `pcall` / `xpcall` 包住的报错会吞掉异常,调试时先临时去掉。
- 执行样本一律在 VM 内、断网。

## 脚本

- `scripts/decode_lua_static.py`
- `scripts/lua_hook.lua`
