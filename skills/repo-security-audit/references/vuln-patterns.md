# 漏洞模式速查

按 OWASP Top 10 (2021) 组织。每类给**典型 source(输入从哪来)**、**sink(危险点)**、**怎么确认**、**常见误报**。脚本的规则库是按这份表设计的,分诊时对着它判断。

核心思维方式:**source → 处理 → sink**。三者齐备才是漏洞,缺一环就是候选。

---

## A01 访问控制失效

### IDOR / 水平越权

- **source**:路由里的 `{id}`、`?user_id=`、请求体里的对象 id。
- **sink**:`findById(req.params.id)`、`get_object_or_404(Model, pk=id)`、`SELECT ... WHERE id = ?` 后直接返回。
- **确认**:查询条件里有没有带上"当前登录者/租户"?查出来后有没有校验归属?两者都没有 → 确认。
- **常见误报**:只读的公开资源(文章详情)、已经用租户中间件隔离的 ORM 查询。

### 垂直越权

- **确认**:管理接口有没有独立的权限注解 / 中间件?前端隐藏按钮不算。
- 关注:只在路由层做了校验、而 service 层可被别的入口绕过。

### 路径穿越

- **source**:文件名、路径、`file` 参数、上传后的名称。
- **sink**:`open()`、`readFileSync`、`file_get_contents`、`new File()`、`filepath.Join`、`res.sendFile`。
- **确认**:有没有 `realpath` / `Clean` 后校验前缀?只过滤 `../` 是不够的(`..%2f`、绝对路径、软链接)。
- **常见误报**:拼接的是固定常量目录 + 数字 ID。

### 开放重定向

- **source**:`next=`、`redirect=`、`returnUrl=`、Host 头。
- **sink**:`res.redirect()`、`sendRedirect()`、`header("Location:")`。
- **确认**:是否只允许相对路径 / 白名单域名?

### CSRF

- **确认**:状态变更接口有没有 CSRF token 或 SameSite Cookie?`@csrf_exempt` 是强信号。
- **常见误报**:纯 JSON API + 自定义头(`X-Requested-With`)+ 严格 CORS,通常已免疫。

### 目录浏览

- nginx `autoindex on`、Apache `Options +Indexes`、对象存储公开列举。

---

## A02 加密失败

- **弱算法**:`MD5` / `SHA1` 用于口令或签名(`PY-MD5-PASSWORD`、`JAVA-WEAK-CRYPTO`、`PHP-WEAK-HASH`)。
- **弱随机**:`Math.random` / `random.random` / `mt_rand` / `new Random` 用于 token、验证码、会话 id。
- **关闭校验**:`verify=False`、`InsecureSkipVerify`、`rejectUnauthorized: false`、`CURLOPT_SSL_VERIFYPEER false`。
- **确认**:这个值是不是安全敏感(token / 口令 / salt / 会话)?不是的话降级到 low 或不算。

---

## A03 注入

### SQL 注入

- **source**:请求参数、header、Cookie、任何外部输入。
- **sink**:字符串拼 SQL —— `.execute("... " + x)`、`f"SELECT {x}"`、`"... %s" % x`、`$sql . $u`、`fmt.Sprintf`、`createStatement()`。
- **确认**:有没有用占位符传参?ORM 的 `.raw()` / `.extra()` / `knex.raw()` 是重灾区。表名/列名不能用参数化 → 必须白名单。
- **常见误报**:拼接的是常量或白名单映射后的值。

### 命令注入

- **sink**:`os.system`、`subprocess(shell=True)`、`child_process.exec`、`Runtime.exec`、`exec.Command("sh","-c",...)`、PHP 的 `system/exec/shell_exec/passthru`。
- **确认**:参数是否含输入?有没有 `escapeshellarg`?能不能换成参数数组形式?
- **注意**:`exec.Command` 本身是安全的,危险在 `sh -c`。

### 代码注入

- **sink**:`eval`、`exec`、`Function()`、`vm.*`、PHP `assert`/`create_function`、`preg_replace` 的 `/e`。
- **确认**:输入是否真的可控?很多 `eval` 只用于内部配置解析。

### 模板注入(SSTI)

- **sink**:`render_template_string`、`Template(user_input)`、Twig / Freemarker / Velocity。
- **确认**:模板字符串是不是常量?变量只是被渲染,不进模板语法 → 不是漏洞。

### 表达式注入

- **sink**:SpEL、OGNL、MVEL、ScriptEngine。
- Java 生态里这类几乎等同 RCE。

---

## A04 不安全设计

- 文件上传:校验白名单扩展名 + 落盘目录不可执行。
- 竞态 / 超卖:余额库存的直接增减,确认有没有数据库条件更新或锁。
- 密码重置:令牌是否低熵、是否可预测、是否在响应里直接返回。

---

## A05 安全配置错误

- **调试开关**:Django `DEBUG=True`、Flask `app.run(debug=True)`、PHP `display_errors=On`。
- **CORS**:`Access-Control-Allow-Origin: *` 与 `credentials: true` 同时出现 → 任意站点可读带凭据的响应。
- **容器**:`privileged: true`、挂载 `docker.sock`、`USER root`、`network_mode: host`。
- **K8s**:`privileged`、`hostPath: /`、`runAsUser: 0`、自动挂载 SA token。
- **CI**:`pull_request_target` + checkout PR 代码(等于 RCE);`${{ github.event.*.title }}` 直接插进 `run`(脚本注入)。
- **管理端点**:Spring Actuator 全量暴露、`phpinfo` 页面。
- **确认**:这份配置是生产用的吗?示例 / 测试配置不算。

---

## A06 组件漏洞

- 走 `deps` 子命令查 OSV。**注意局限**:只判断版本是否命中 CVE,不判断该组件在你的代码里是否被调用、调用路径是否可达。
- 定级时叠加"是否暴露在入口"(框架、中间件、对外 SDK 高;仅供测试的库低)。

---

## A07 认证与会话失效

- **JWT**:`algorithms: ["none"]`、`verify_signature: False`、`ignoreExpiration: true`、密钥硬编码。
- **口令**:弱哈希、明文比较、魔法哈希(`md5(x) == "0e..."`)。
- **会话**:Cookie 缺 `HttpOnly` / `Secure` / `SameSite`;session id 可预测。
- **确认**:这段代码是认证路径上的吗?验证码 / 演示接口上的问题要降级。

---

## A08 软件与数据完整性失败

### 反序列化

- **sink**:`pickle.loads`、`yaml.load`(非 SafeLoader)、PHP `unserialize`、Java `ObjectInputStream`、`XMLDecoder`。
- **确认**:数据来自外部?能不能换 JSON?Java / PHP 的 gadget 链成熟,确认即 critical。

### XXE

- **sink**:`etree.parse`、`simplexml_load_string`、`DocumentBuilderFactory`、`SAXParserFactory`。
- **确认**:禁用了 DTD / 外部实体吗?PHP 8 默认较安全,但显式声明才算。

### 供应链

- CI 里 `curl ... | bash`、未固定 SHA 的第三方 action、不校验哈希的二进制下载。

---

## A09 日志与监控失败

- 日志里打印口令 / token / 身份证 / 银行卡(`WEB-SENSITIVE-LOG`)。
- 报错页面回显堆栈和 SQL。
- 关键操作(登录、改密、转账)没有审计日志 —— 靠读代码判断,脚本抓不到。

---

## A10 SSRF

- **source**:URL 参数、webhook 地址、图片代理、导入远程文件。
- **sink**:`requests.get(url)`、`urlopen`、`axios.get`、`curl_setopt(...CURLOPT_URL...)`、`HttpURLConnection`。
- **确认**:是否限制协议(`file://`、`gopher://` 常被漏掉)?是否校验解析后的 IP(而不是字符串)?能否访问内网与 `169.254.169.254`?
- **常见误报**:目标来自固定配置或白名单。

---

## 跨类:文件上传

- **确认**顺序很关键:①扩展名白名单 ②MIME 与魔数校验 ③重命名 ④落盘目录不可被 Web 解析 ⑤大小限制。
- 只做了前端校验 = 没做。

---

## 分诊通用清单

对每条候选,按顺序问:

1. **这段代码在生产路径上吗?** 测试、示例、死代码 → 直接排除。
2. **输入真的可控吗?** 常量、配置、内部生成 → 排除或降级。
3. **中间过滤了吗?** 找最近的一道校验(白名单 > 转义 > 黑名单 > 没有)。
4. **需要什么前提?** 登录、特定角色、内网可达 —— 写进报告的前置条件。
5. **影响是什么?** 能读到什么、写到什么、执行什么。想清楚再定级。
