# 语言与运行时备注

通用层在 `checklist.md`。这里只写**各语言特有的优化手段**(附生效前提)和**审查时容易误判的陷阱**。
只在目标项目用到了该语言时读对应小节,不要通读。

## PHP

- **OPcache**:生产必需。要看 `opcache.enable`、`validate_timestamps`(生产关掉免 stat)、`opcache.memory_consumption`(编译缓存体积可能撑爆)。
- **preload**:`opcache.preload` 能省类加载,但要求常驻进程(FPM 需 `opcache.preload_user`),且预加载的类改动要重启。
- **JIT**:只对 CPU 密集代码有意义;IO/框架型代码收益通常很小,别把它当卖点。
- **自动加载**:PSR-4 每次类解析要 stat;`composer dump-autoload -o`(classmap)能显著减少。审查时看部署是否用了 `--optimize-autoloader`。
- **反射缓存**:`ReflectionClass`/`ReflectionMethod` 应进程级缓存;只缓存对象、仍每次遍历属性/方法的,收益有限。
- **数组**:COW,显式 `array_merge`/深拷贝在热路径上贵;`isset` 比 `array_key_exists` 快。
- **字符串**:循环里 `.=` 拼接在极端规模会退化;`sprintf`/数组 `implode` 更稳。
- **SQL**:`ATTR_EMULATE_PREPARES=true` 省一次往返,但语句缓存收益也随之变化,两种模式要分别评估;`ATTR_PERSISTENT` 在 FPM 下有用。
- **Swoole/常驻**:协程内不能用阻塞 IO;连接池必须按协程/请求隔离;静态变量跨请求存活是最大陷阱。
- **陷阱**:`realpath_cache`、`include` 大文件、`serialize/unserialize`(慢且危险)、`@` 抑制(吞错误也吞掉诊断)。

## Python

- **GIL**:CPU 密集用多进程(`multiprocessing`/`ProcessPoolExecutor`);IO 密集用线程或 asyncio。单进程多线程不会加速 CPU 计算。
- **分配**:`__slots__` 减少实例字典;避免热循环里的属性查找(提到局部变量);`dataclass(slots=True)`。
- **缓存**:`functools.lru_cache` 要确认键可哈希、内存有界;`functools.cache` 无上限,长跑会涨。
- **导入**:启动期导入大量模块是常见固定税;`-X importtime` 可观测,懒加载能省。
- **序列化**:`json` 是纯 Python 实现,大对象慢;`orjson`/`msgspec` 可替;`pickle` 慢且不安全。
- **正则**:`re.compile` 一次、复用 pattern;避免在循环里编译。
- **ORM**:Django `select_related/prefetch_related`、SQLAlchemy `joinedload/selectinload`;批量 `bulk_create`。
- **陷阱**:`+` 拼字符串、列表推导里的函数调用、`copy.deepcopy` 在热路径。

## JavaScript / TypeScript

- **事件循环**:同步 IO、大 `JSON.parse`、长计算会阻塞全部请求;这是 Node 服务最常见的性能事故。
- **V8 隐藏类**:对象形状频繁变化、`delete`、动态加属性会退化;构造时一次定型。
- **流式**:大响应/大文件不要整体读入内存;`stream.pipeline`。
- **打包**:tree-shaking、避免把整个库打进 bundle;服务端可跳过打包。
- **连接池**:`pg`/`mysql2` 池要在进程级创建,不要每请求建。
- **陷阱**:`await` 在循环里串行(可 `Promise.all`)、`Array.prototype` 链式操作产生中间数组、闭包持有大对象。

## Java / Kotlin

- **JIT 预热**:冷启动与稳态性能差异大,基准必须预热;报告里别拿冷启动数据说事。
- **GC**:对象分配率高会推高 GC;关注新生代大小与晋升。`StringBuilder` 替 `+`。
- **Stream**:链式 Stream 有装箱与 lambda 开销,热路径上普通循环可能更快。
- **反射/注解扫描**:启动期扫 classpath 是固定税;能换编译期处理(注解处理器)就换。
- **连接池**:HikariCP 池大小与超时;别在请求里建池。
- **陷阱**:自动装箱、`Optional` 滥用、日志字符串拼接未做惰性判断。

## Go

- **逃逸分析**:热路径上的小对象尽量留在栈;`sync.Pool` 复用缓冲。
- **切片**:预分配 `make([]T, 0, n)`;避免反复 `append` 扩容。
- **字符串与字节**:`[]byte`/`string` 互转有拷贝;`strings.Builder`。
- **JSON**:`encoding/json` 反射开销大,高频可用代码生成(`easyjson`)或 `jsoniter`。
- **并发**:goroutine 泄漏(无退出路径)、无界队列、锁竞争;`-race` 与 pprof 是主要观测手段。
- **陷阱**:热循环里的 `defer`(有开销,Go 1.14 后降低但非零)、大对象按值传参。

## Rust

- **分配**:`Vec::with_capacity`、`&str` 优先于 `String`、`Cow` 按需克隆。
- **异步**:阻塞调用放进 async runtime 会卡住整个 worker;用 `spawn_blocking`。
- **锁**:`Mutex` 粒度、`RwLock` 读多场景;避免持锁跨 await。
- **陷阱**:无意的 `clone()`、`format!` 在热路径、trait object 动态分发。

## C# / .NET

- **分配**:`Span<T>`/`Memory<T>`、`ArrayPool<T>`、`ValueTask`。
- **LINQ**:链式查询有分配与委托开销,热路径用循环。
- **启动**:ReadyToRun/AOT 影响冷启动;服务常驻时影响小。
- **陷阱**:async 状态机开销、闭包捕获、字符串插值在热循环。

## 跨语言通用判断

- **语言层的优化手段要落到"这个项目用没用"**,不是罗列语言特性。没用的写在"建议"里,用了的写进"已做优化清单"。
- **运行时行为不能靠静态读代码确定**:JIT、GC、OPcache 命中率、逃逸分析都要实测。报告里属【推测】。
- **同一手段在不同模式下的收益不同**(preload/JIT/池化),按 `runtime-modes.md` 分开写。
