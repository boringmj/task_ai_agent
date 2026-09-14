# 常见架构模式识别清单

判断仓库属于哪类模式时对照下表。**识别信号**看目录名和依赖方向;**常见问题**是这类模式容易出的毛病,可以直接用来写报告里的「风险与改进点」。

## 分层架构(Layered / N-tier)

- **信号**:`controller` / `service` / `repository|dao` / `model|entity`
  之类的目录,依赖单向向下。
- **常见问题**:层次被穿透(controller 直接查库);`service` 变成堆积业务的巨层;实体对象在各层间被随意改造。

## 六边形 / 端口-适配器(Hexagonal / Clean / Onion)

- **信号**:`domain|core` 内层不依赖框架;`adapter|infrastructure|gateway` 在外层;
  内层靠接口(端口)反转依赖。
- **常见问题**:内层偷偷 import 了框架或数据库;端口划分过细或过粗;DTO 与领域对象混用。

## MVC / MVVM(多见于 Web 与客户端)

- **信号**:`models` / `views` / `controllers`(或 `viewmodels`),由框架约定驱动。
- **常见问题**:Controller 太胖;Model 里混业务逻辑;视图与数据绑定纠缠,难以测试。

## 插件化 / 注册表(Plugin / Registry)

- **信号**:`plugins/` 目录、注册函数、钩子(`hook` / `entry_points` / 装饰器注册)、按名字动态查找实现。
- **常见问题**:插件接口不稳定;加载顺序与依赖没约束;失败一个插件拖垮整体。

## 事件驱动 / 消息(Event-driven)

- **信号**:`events` / `handlers` / `listeners` / `subscribers`;
  引用 Kafka / RabbitMQ / Redis 之类的中间件;发布-订阅语义。
- **常见问题**:事件流难以追踪;处理器重复消费、幂等没做好;没有统一的事件契约。

## 管道 / 过滤器(Pipeline)

- **信号**:一串按顺序执行的处理步骤(middleware、chain、stage),数据流经每步被转换。
- **常见问题**:步骤耦合、无法单独测试;错误处理散落在每步里。

## 微服务 / 多模块(Monorepo / Multi-service)

- **信号**:仓库里有多个各自带入口或构建文件的子项目;`services/`、`apps/`、`packages/` 布局。
- **常见问题**:共享代码靠复制;服务间边界模糊;本地无法整体启动。

## 单体(Monolith)

- **信号**:单一入口、单一部署单元,业务模块仍在一个进程内。
- **常见问题**:模块间无边界、互相 import;改动一处牵动全局;难以拆分。

## 库 / SDK

- **信号**:没有应用入口,核心是公开 API(`__init__` 的导出、`index` 的 re-export),配大量示例与测试。
- **常见问题**:公开接口不稳定;内部实现被外部依赖;文档滞后于代码。
