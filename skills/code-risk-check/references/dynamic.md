# 动态观察(只在 VM 内做)

静态看不懂、或要确认「它到底有没有往外发、改了哪些文件」时,在沙箱虚拟机里真跑一次并观察。

**绝不在宿主或容器里执行待检样本。** 这一节所有命令都在 VM 里跑。

## 前提

虚拟机看不到技能目录(`skills/` 只挂载进容器),所以先把脚本经容器拷进工作区,再推进 VM:

```bash
# 容器里执行:把脚本拷到工作区(技能目录只读,/workspace 可写)
#   <本技能目录> 用加载本技能时给出的实际位置(它会随资源清单一并列出)
mkdir -p .risktool && cp <本技能目录>/*.py .risktool/
```

然后用 `vm_push` 把 `.risktool/fs_snapshot.py`、`.risktool/probe_http.py` 推到 guest 的 `/root/tools/`。

确认 guest 里有样本需要的运行时(`python3` / `node` / `php` / `lua`)。**缺了就停下来问用户**要不要装,不要自作主张 `apk add` 一堆东西。`vm_status` 确认虚拟机就绪,样本 `vm_push` 到 `/root/sample/`。

## 步骤

### 1. 记录基线

```bash
python3 /root/tools/fs_snapshot.py snap /root/before.json
```

### 2. 布置观察点

把静态扫描发现的硬编码域名 / IP 写进 hosts 指到本地,再起记录型 HTTP 服务,让样本"发得出去"但发不到真目的地 —— 这样能安全地看到它想发什么:

```bash
# 域名从静态扫描结果里拿(risk_scan.py 的 net-url / net-hardcoded-ip 命中)
echo "127.0.0.1 evil.example.com" >> /etc/hosts
python3 /root/tools/probe_http.py --port 80 --log /root/http.log &
```

注意两点:

- `probe_http.py` 没有 TLS。样本若强制 https,握手会失败 —— **失败本身就是信号**(证明它想连外网),内容则看不到。
- 端口要对上样本实际用的;一次可多起几个(80、8080、443 不放)。

### 3. 挂上连接记录

另开一轮采样,记下样本尝试连接的目标(排除本地回环):

```bash
( for i in $(seq 1 120); do date +%T; netstat -tunp 2>/dev/null | grep -v -e "127.0.0.1" -e "::1" -e "10\.0\.2\." ; sleep 1; done ) > /root/net.log 2>&1 &
```

`netstat` 不可用时改用 `/proc/net/tcp`(十六进制地址,需换算)或 `ss -tanp`。
`10.0.2.*` 这类是虚拟机自身与宿主的控制通道,不是样本行为,所以排掉;`-p` 能带上进程名,便于归因。

### 4. 执行样本

带超时跑,输出留档:

```bash
cd /root/sample && timeout 60 python3 ./x.py > /root/run.log 2>&1; echo "exit=$?"
```

- 样本要参数就用它自带的 `--help` 先看,别乱传。
- 需要 GUI / 交互的样本,在无终端环境里可能起不来 —— 记下"未能运行",不要为它改系统。
- 时间给够:部分样本会 `sleep` 很久才动作,超时可以放到 120~300 秒。

### 5. 比对与收割

```bash
python3 /root/tools/fs_snapshot.py diff /root/before.json /root/after.json
cat /root/http.log      # 记录型服务收到的请求(方法 / 路径 / 头 / 体)
cat /root/net.log       # 尝试建立的连接
cat /root/run.log       # 样本输出与报错
```

再看一眼进程与自启项有没有被动过:

```bash
ps aux | head -50
crontab -l 2>/dev/null; ls -la /etc/cron* /etc/init.d 2>/dev/null
```

**残留物核对**(报告的"副作用清单"就靠这一步填):

- diff 里的**新增**项就是运行期产生的东西 —— 逐条判断:是样本的产物,还是观察工具自己的日志(见下面常见坑)。
- 样本已经退出了,`after.json` 里**仍然存在**的条目就是**残留**;本该自清理的临时文件还躺在那儿,也是残留。
- 每条都记**具体路径**,不要只写"生成了缓存文件" —— 写成 `~/.cache/mytool/state.db`。
- 样本带清理逻辑(`atexit`、`finally`、`TemporaryDirectory`)时,再正常跑一次确认它真清掉了;有的只在正常退出时清理,被杀掉或抛异常就留下。

### 6. 收尾

把观察结果整理进报告(出站目标、携带字段、文件改动、进程),然后清掉观察点:

```bash
pkill -f probe_http.py; sed -i '/evil.example.com/d' /etc/hosts
```

样本留在 VM 里即可(VM 是沙箱),**不要** `vm_pull` 回工作区。

## 常见坑

- `/tmp` 上的噪声:系统自己也写 `/tmp`,比对结果里出现无关文件属正常,按路径和内容判断相关性。
- **观察工具自己的产物会被记进 diff**:`before.json` / `after.json` / `http.log` / `net.log` / `run.log` 都放在 `/root` 下,比对时必然出现;还有你为劫持 hosts 加的那一行。这些不是样本行为,别误报。
- 样本没网络就静默跳过外发:可能在 `try/except` 里吞掉了异常 —— 看 `run.log` 的报错和 `net.log` 的连接尝试,别只看有没有成功上传。VM 本身不通外网时,`connect` 会立刻失败,采样未必抓得到那一瞬 —— 此时"报错信息"就是证据。
- 有的样本要求"先装依赖":装依赖本身是 L1 行为,记录即可,但要注意 `postinstall` 脚本。
- 样本因缺依赖直接失败(`ModuleNotFoundError`、`command not found: ffmpeg`)时,别当“没跑成”就算了 —— 这正是**运行时依赖信息**,抄进依赖表。
