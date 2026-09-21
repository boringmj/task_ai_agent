# task-ai-agent

一个跑在本机的命令行 AI agent。**主 agent 跟你对话、派活、验收;子 agent 埋头干活、
把结论交回来。** 两边都能在沙箱里跑代码、能按需加载技能、能跨会话记事。
模型后端走 DeepSeek(OpenAI 兼容协议),默认 `deepseek-flash`。

它和「给对话框套一层壳」的 CLI agent 差别主要在**成本**和**隔离**这两件事上:

- **成本**:子 agent 有**自己独立的上下文和账号** —— 一次安全审计读几十个文件的过程
  (动辄十几万 token)不会堆进主 agent 的记忆,只回一段结构化结论。提示词按共享程度排布,
  主 agent 和所有子 agent 逐字相同的那一段放在**最前面**,于是主 agent 的请求顺手就把
  cache 给后面的子 agent 预热了(实测子 agent 缓存命中率 95%+);上下文到 90% 自动压缩,
  压缩请求特意复用原对话前缀,好让整段历史仍按缓存价计费。
- **隔离**:三层,由强到弱 —— 一次性 **Docker 容器**(默认的 `run_python` / `run_command`)、
  **独立内核的 Alpine 虚拟机**(`vm_*`,可选)、以及**工作区**本身。子 agent 的写权限按目录
  划,在容器里由**挂载**强制(授权之外的部分挂成只读),不是靠它自觉。

## 目录

- [特性](#特性)
- [快速开始](#快速开始)
- [目录结构](#目录结构)
- [它怎么工作](#它怎么工作)
- [工具](#工具50-个)
- [技能](#技能9-个)
- [终端指令](#终端指令)
- [配置](#配置)
- [测试](#测试)
- [虚拟机(可选)](#虚拟机可选)
- [已知限制](#已知限制)

## 特性

| | |
| --- | --- |
| **子 agent 编排** | 派活(`dispatch_task`)、挂起提问(`suspend`)、验收 / 打回 / 续跑。每批可带缓存预热闸门;并发上限默认 10 |
| **三层沙箱** | Docker 容器(默认)、QEMU/Alpine 虚拟机(按需)、工作区；VM 里还有 vmserver 提供命令执行与端口转发 |
| **技能系统** | 三级加载:元数据常驻、正文按需、资源用到才读。技能再多也只按每个约 100 词占常驻空间 |
| **长期记忆** | `workspace/.agent/memory.md`,启动时注入;agent 会自己判断该记什么 |
| **会话** | 每段会话有自己的对话历史**和自己的虚拟机磁盘**,互不干扰;可切换、可恢复 |
| **成本可见** | `/tokens` 看上下文 / 本轮 / 本会话三段用量与缓存命中;子 agent 的账单独记 |
| **能改文件** | 行区间读取、行级编辑、写作前有体积护栏、删除进回收站可还原 |
| **能看屏幕** | 截图、点击、键入、拖拽(直接作用于真实桌面,默认最克制的一类) |
| **联网** | 网页抓取、搜索(博查 / Tavily / DuckDuckGo)、下载、git |

## 快速开始

**环境要求**

| | |
| --- | --- |
| 操作系统 | **Windows**。桌面类工具基于 pyautogui、启动脚本是 `.bat`;其余部分理论上跨平台,但没在别的系统上验过 |
| Python | **3.10+**(开发与测试环境 3.10.7) |
| Docker | 容器工具(`run_python` / `run_command`)需要 Docker Desktop 在跑。**没装也能用**,只是这两个工具会直接报错,其余功能不受影响 |
| DeepSeek key | 必填。缺了启动即报错 |
| QEMU + 镜像 | 可选,只有 `vm_*` 那组工具需要 —— 见[虚拟机(可选)](#虚拟机可选) |

**安装**

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

然后编辑 `.env`,至少填上:

```
DEEPSEEK_API_KEY=sk-xxxxxxxx
MAX_CONTEXT_TOKENS=1000000      # 可选但建议:DeepSeek 现在的模型都是 1M
                                # 不设的话按代码里的默认值 128000,会更早触发压缩
```

**运行**

```bat
agent.bat
:: 等价于
.venv\Scripts\python.exe agent.py
```

启动后会:接回这个工作区**最后跑过的那段会话**(首次运行则新建一段),把最近几条对话回显
出来,然后等你输入。空行提交输入,所以粘贴多行没问题;直接敲 `/` 开头的是终端指令。

## 目录结构

```
agent/                  agent 本体(包)
  cli.py                终端入口:输入循环、指令分发、启动恢复
  loop.py               主循环:模型 ↔ 工具 往返;上下文压缩
  llm.py                流式调用 + 用量统计(含缓存命中)
  ctx.py                每个 agent 自己的状态(ContextVar 隔离)+ 权限范围 FsGrant
  core.py               路径、常量、模型客户端、容器与工作区
  session.py            会话持久化(逐行追加的 .jsonl)
  tasks.py              子 agent:派活、挂起、验收、用量累加
  skills.py             技能发现与三级加载
  prompts.py            提示词装载(变量替换不做 str.format)
  tools/                50 个工具,一个领域一个文件
  commands/             终端指令(/tokens、/subtasks …)
prompts/                所有写给模型的文本,一个用途一个 .md
skills/                 技能(一个目录一份 SKILL.md)
tests/                  pytest
vmserver.py             guest 里的服务端:执行命令 + 按需代理端口
vm/                     QEMU 本体与虚拟机镜像(不进版本库)
workspace/              agent 的工作区(自成一个 git 仓库,不进版本库)
sessions/               会话数据:对话历史 + 每会话一块虚拟机磁盘(不进版本库)
```

工作区里还有几处由程序管理的地方:`.agent/memory.md`(长期记忆)、`.trash/`(删除的
文件,可还原)、`.pylibs/`(`pip install --target` 装进容器的库)、`.tmp/<会话>/<谁>/`
(给技能脚本产出中间文件的草稿纸)。

## 它怎么工作

**一轮对话。** 用户输入 → 模型 → 若它要调工具就执行、把结果塞回历史 → 再问模型……
直到它不再要工具。主 agent 单轮上限 50 步(子 agent 60),触顶会被强制中止,所以提示词里
要求它「一次能拿全的信息别拆成好几趟」。

**子 agent 不是嵌套调用,是会话里独立、可恢复的单元。** 它有自己的一份上下文、自己的存储
(`sessions/<会话>/tasks/<任务号>/` 下完整落盘)、自己的 token 账号。它交回来的东西是**有限
且结构化**的:结论 / 产出 / 没做成的 / 存疑。**倒回整个上下文就等于白派了** —— 省上下文这件
事会在交回的那一刻全部还回去。

它要权限、或要用户拍板时,用 `suspend` **把自己停下**,把问题交给主 agent;主 agent 拿到
原话,能答就答、该问用户就问用户,然后 `resume_task` 让它带着原上下文接着干。干完由主 agent
`finish_task` 验收(accept / rework / stop)。

**提示词怎么排,直接决定缓存命中率。** 主 agent 和子 agent 的提示词前缀**逐字相同的那一段
放在最前面**(通用规则、工具指南),角色相关的放在后面。原因是前缀缓存按 512 token 的
「缓存单元」匹配:一旦中途分叉,后面全部失效。这么排之后,主 agent 的请求会替它后面派出去的
子 agent 预热同一段。同理,每个 agent 的提示词前缀是**只追加**的 —— 往里插内容会把后面整段
缓存打掉,所以连「本次是恢复的会话」这种提示也是追加在末尾,而不是塞进系统提示词。

**上下文压缩。** 到模型上限的 90% 自动压一次(也可手动 `/compact`):把较早的对话换成一份
摘要,开头几条 system(系统提示词与长期记忆)和最近几条消息保留不动。

**权限不是「能写 / 不能写」,是「能写哪儿」。** 几个子 agent 并排跑时改的是同一批文件,
给整片工作区写权限等于让它们互相踩 —— 而且踩了**不报错**,只是结果对不上、事后查不出是谁
改的。所以写范围要划开、几个并排的活不许重叠,重叠直接拒。删除**单独**一个开关(删掉的
东西改不回来,不该跟「写」共用权限)。范围之外它改不了:容器挂载与路径检查一起兜住。

## 工具(50 个)

`主+子` = 两种角色都能调;`仅主` = 只有主 agent 有(危险的、管终端的、管子 agent 的)。

| 领域 | 工具 |
| --- | --- |
| **文件** (11) | `read_file` `write_file` `append_file` `edit_lines` `insert_lines` `move_file` `list_files` `find_files` `grep_files` `get_current_directory` `get_current_time` |
| **容器** (2) | `run_python` `run_command` —— 一次性容器,执行完即销毁 |
| **虚拟机** (10) | `vm_start` `vm_status` `vm_run` `vm_push` `vm_pull` `vm_fetch` `vm_tcp` `vm_tunnel` `vm_tunnel_stop` `vm_ssh_login` |
| **子 agent** (5) | `dispatch_task` `task_status` `finish_task` `resume_task`(仅主)、`suspend`(仅子) |
| **回收站** (4) | `delete_file` `delete_dir` `restore_file` `purge_trash`(仅主) |
| **联网** (3) | `fetch_url` `web_search` `download` |
| **记忆** (2) | `remember` `read_memory`(仅主) |
| **桌面** (8) | `click` `type_text` `press_keys` `drag` `scroll` `move_mouse` `clear_marker` `read_clipboard`(全部仅主) |
| **图片与屏幕** (2) | `img`、`screen`(仅主) |
| **其它** (3) | `git`(仅主)、`check_markdown`、`load_skill` |

工具表是**全局固定**发给模型的;`仅主 / 仅子` 的判定发生在**调用时** —— 授权是动态的,
工具表不能是。

## 技能(9 个)

技能 = 按需加载的说明书。平时只在上下文里占一行元数据,判断用得上才 `load_skill` 把正文读进来。
目录本身**不在工作区里**,被只读挂载进容器,所以 agent 改不了它、也不会误当资料读。

| 技能 | 什么时候用 | 角色 |
| --- | --- | --- |
| `repo-security-audit` | 安全审计、漏洞扫描、利用链分析(Python/JS/Java/Go/PHP 有专项规则) | 子 |
| `code-quality-check` | 微观代码质量:复杂度、嵌套、命名、错误处理、资源管理、重复代码 | 子 |
| `code-risk-check` | 判断「跑起来会做什么」:行为清单 + 安全等级 L0~L4 + 类型标签 | 子 |
| `repo-structure-analysis` | 仓库结构:目录组织、模块划分、依赖方向、循环依赖、上帝模块 | 子 |
| `repo-architecture-design` | 架构设计 / 重新设计 / 重构方案(先跟用户对齐再出方案) | 主+子 |
| `architecture-migration` | 跨架构形态/技术栈/部署方式的迁移,出 Go/No-Go 与分阶段计划 | 主+子 |
| `code-deobfuscation` | 还原被混淆/加密的代码(JS / PHP / Lua),含 VM 内动态脱壳 | 子 |
| `task-orchestration` | 明显多步、步骤有先后依赖、可能跨多轮的任务 | 主 |
| `writing-skills` | 想新增/修改技能,或把一套固定流程沉淀下来复用 | 主 |

技能可以在 frontmatter 里声明 `requires`(硬依赖)和 `optional`(可选依赖),依赖项只认
`skill`(看技能目录在不在)和 `pip`(看有没有被 `pip --target` 装进 `/workspace/.pylibs`)——
因为只有这两样能被**确切**回答「装没装」。

## 终端指令

| 指令 | 作用 |
| --- | --- |
| `/help [指令]` | 列出全部指令;带名字看那一条的详细用法 |
| `/tokens` | 上下文占用、本轮用量、本会话累计(含缓存命中) |
| `/subtasks [任务号] [enter\|kill\|log\|all\|off]` | 看子 agent 在干什么 / 接管它 / 叫停它 |
| `/sessions` | 列出这个工作区的全部会话,标明当前是哪个 |
| `/switch [会话id\|new]` | 切换会话(连带换它自己的虚拟机磁盘) |
| `/compact` | 手动压缩对话历史,释放上下文 |
| `/reset`(别名 `/new`) | 丢掉对话历史重新开始(工作区文件、记忆、VM 磁盘都不动) |
| `/vmreset` | 重置虚拟机:删掉本会话的磁盘,用基础镜像重来(不可撤销) |
| `/exit`(别名 `/quit`) | 退出(裸敲 `exit` / `quit` 等价) |

## 配置

全部走 `.env`(或环境变量)。常用的:

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 必填 | API key |
| `MODEL` | `deepseek-flash` | 模型名 |
| `MAX_CONTEXT_TOKENS` | `128000` | 模型上下文上限,自动压缩按它的比例算 |
| `AUTO_COMPACT_RATIO` | `0.9` | 用到多少就自动压一次 |
| `MAX_STEPS` | `50` | 主 agent 单轮步数上限 |
| `AGENT_WORKSPACE` | `workspace` | 工作区目录,必须是项目内的子目录(多实例各自跑不同工作区时设它) |
| `SEARCH_PROVIDER` / `SEARCH_API_KEY` | 无 | `bocha` / `tavily` / `duckduckgo`;不配则 `web_search` 直接报错,其余功能不受影响 |
| `DOCKER_IMAGE` | `python:3.11-slim` | 容器镜像 |
| `DOCKER_CMD_TIMEOUT` | `60` | 容器里一条命令的时限(秒) |
| `VM_CMD_TIMEOUT` / `VM_LONG_TIMEOUT` | `60` / `600` | 虚拟机里一条命令的默认 / 长任务时限(秒)。**这是「用户愿意等多久」而不是「命令能跑多久」**:等的时候整个会话都停在那儿,再长的活该丢后台 + 轮询日志 |
| `VM_AUTOSTART` | `0` | 会话启动时是否顺手把虚拟机拉起来 |
| `VM_ACCEL` | `whpx` | 加速方式;没开 WHPX 就设 `tcg`(慢但通用) |
| `SUBAGENT_MAX_CONCURRENT` | `10` | 同时在跑的子 agent 上限 |
| `SUBAGENT_MAX_STEPS` | `60` | 子 agent 单轮步数上限 |
| `SUBAGENT_WAIT_MAX` | `1800` | 主 agent `wait=true` 最多等多久(秒) |
| `GIT_TIMEOUT` | `120` | 单条 git 命令的硬超时(秒) |
| `FETCH_TIMEOUT` | `10.0` | 单次网页请求超时(秒) |
| `MAX_WRITE_BYTES` | `3145728` | 单次写入护栏(3MB) |
| `TRASH_MAX_AGE_DAYS` / `SCRATCH_MAX_AGE_DAYS` | `7` | 回收站与临时区的保留天数 |

其余都是细调项(各类输出上限、`-R` 递归上限、重定向次数……),在用到的那处代码里就近定义,
搜 `os.environ` 即可。`.env.example` 里对 VM 那一组有更详细的注释。

## 测试

```bat
.venv\Scripts\python.exe -m pytest tests/ -q
```

测试**全部使用假模型**(只替换 `loop.stream_model`,底下的生产代码照跑),所以不花钱、
可以随时跑。覆盖:权限与隔离、会话存储与恢复、提示词装配(含「占位符不许漏替换」这类护栏)、
子 agent 生命周期、批量派发与缓存预热、临时区、用量统计、技能加载。

看覆盖率:

```bat
.venv\Scripts\python.exe -m coverage run --source=agent -m pytest tests/ -q
.venv\Scripts\python.exe -m coverage report -m
```

## 虚拟机(可选)

`vm_*` 那组工具要一台 QEMU 虚拟机。**`vm/` 不进版本库**(QEMU 本体 500M+、镜像各约 300M),
所以刚克隆下来是没有的 —— 需要本地准备:

```
vm/qemu/                  QEMU 本体(qemu-system-x86_64.exe、qemu-img.exe 及依赖的
                          DLL 与 share/ 固件 —— share/ 必须挨着 exe,QEMU 靠它找 BIOS)
vm/alpine-vmserver.qcow2  基础盘,预装 vmserver,全体共享、只读
vm/base-clean.qcow2       干净 Alpine 底本,留着以便重建基础盘
```

没准备好时:`vm_status` 会告诉你状态,其余功能**完全不受影响**(VM 默认也不随会话启动)。
虚拟机是每会话一块 overlay 磁盘,所以重置 / 丢弃某个会话不会影响别的会话。

另有一个 Windows 上的注意点:**别改 guest 的 root 密码**。程序靠串口登录进去注入 token,
改了密码虚拟机就起不来,而且报错看不出原因。

## 已知限制

- **看不到「这场会话一共花了多少」。** 真正的 token 用量只落在子任务的
  `meta.json.usage` 里,主会话的用量**没有落盘**;`/tokens` 报的是当前进程内存里的数,
  退出即失、重启也接不上。
- **等待中的阻塞调用按不动 Ctrl+C。** Windows 上 Ctrl+C 只会在主线程回到 Python 字节码时
  抛出,所以 `vm_run` 等长任务、以及模型请求(默认 600 秒超时 × 最多 3 次尝试)期间,
  按 Ctrl+C 没有任何反应 —— 按几次都一样。提示符和子 agent 等待是轮询的,那两处正常。
- **虚拟机镜像不在仓库里**,也没有烤制脚本(见上一节)。
- `read_file` 单次上限 3MB;碰上超大文件仍可能把一次请求撑得很难受。
- 桌面类工具直接作用于真实桌面,没有隔离;紧急中止方式是**把鼠标甩到屏幕任意一角**。
