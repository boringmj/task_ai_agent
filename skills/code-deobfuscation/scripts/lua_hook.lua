-- Lua 动态去混淆 harness:覆盖载入函数,截获运行时拼出的 chunk。
--
-- 用法:  lua5.4 lua_hook.lua <样本.lua> [输出文件]
-- 安全:  样本会被真实执行,只能在 VM 沙箱内运行,禁止在宿主或容器上跑。
--
-- 注意:vm_push 上传后换行会变成 CRLF,先 `sed -i 's/\r$//' lua_hook.lua` 再跑。

local samplePath = arg[1]
local outPath = arg[2] or "lua_extracted.lua"
if not samplePath then
  io.stderr:write("用法: lua5.4 lua_hook.lua <样本.lua> [输出文件]\n")
  os.exit(1)
end

local outFile = io.open(outPath, "w")
local hits = 0

local realLoad = load

local function record(tag, chunk)
  if type(chunk) == "string" then
    hits = hits + 1
    outFile:write("-- ---- via " .. tag .. " ----\n" .. chunk .. "\n")
    outFile:flush()
  end
end

local function readAll(path)
  local f = io.open(path, "r")
  if not f then return nil end
  local c = f:read("*a")
  f:close()
  return c
end

-- 关键:Lua 5.4 里给 load 显式传 nil 的 env 会把 _ENV 置空,
-- 所以必须按"实际传了几个参数"转发,而不能无脑补 nil。
_G.load = function(...)
  local chunk = ...
  record("load", chunk)
  local n = select("#", ...)
  if n >= 4 then return realLoad(...) end
  if n >= 3 then return realLoad(chunk, (select(2, ...)), (select(3, ...))) end
  if n >= 2 then return realLoad(chunk, (select(2, ...))) end
  return realLoad(chunk)
end

_G.loadstring = function(...)
  local chunk = ...
  record("loadstring", chunk)
  local n = select("#", ...)
  if n >= 2 then return realLoad(chunk, (select(2, ...))) end
  return realLoad(chunk)
end

_G.loadfile = function(...)
  local path = ...
  local c = readAll(path)
  record("loadfile", c)
  if c == nil then return nil end
  local n = select("#", ...)
  if n >= 3 then return realLoad(c, path, (select(2, ...)), (select(3, ...))) end
  if n >= 2 then return realLoad(c, path, (select(2, ...))) end
  return realLoad(c, path)
end

_G.dofile = function(path)
  local c = readAll(path)
  record("dofile", c)
  local fn = realLoad(c, path)
  if fn then return fn() end
end

-- 执行样本本身(用 realLoad,避免把样本本体也记进去)
local sampleSrc = readAll(samplePath)
local fn = realLoad(sampleSrc, samplePath)
if fn then fn() end

outFile:close()
if hits == 0 then
  io.stderr:write("[!] 未截获 load/loadstring/dofile 的 chunk。样本可能用了别的载入方式,或已自带解码。\n")
  os.exit(2)
end
print("[OK] 截获 " .. hits .. " 处,已写入 " .. outPath)
