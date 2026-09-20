# 开发纪事

这个文件是**按时间顺序的工程日志**，记录每一步做了什么、踩了什么、为什么这么改。
它从 `AGENTS.md` 里拆出来单独存放，原因有两个：一是 `AGENTS.md` 是给协作者/AI
助手的**操作手册**，需要短而准；二是这份日志的价值恰恰在于"当时到底发生了什么"，
不该被压缩成结论。

想要**提炼过的结论**，看 [踩坑记录](lessons.md)；想要**现在的设计**，看
[架构与设计决策](architecture.md)。

---

## 实跑验收记录（M1：从零到能上网）

`yi up` → 竞价实例（`ecs.e-c1m1.large` @ `cn-hongkong-b`，出价 0.030 = 市场价
0.020 × 1.5，含 1 小时保护期）→ 服务端回环自检通过（dest 自动选中
`www.cloudflare.com`）→ **从本机经该服务器访问外网：`generate_204 = 204`，
ipinfo 显示 Hong Kong / Alibaba**。

随后在 Clash Verge Rev 上独立验证通过（开系统代理 → 浏览器显示香港 → 用完关掉）。

## 实跑验收记录（M4：竞价回收自愈）

`watch --interval 20` → 手动删除实例模拟竞价回收 → 20 秒后第 1 次查不到 →
再 20 秒第 2 次 → 判定回收 → 自动重建（新实例 + 自动 StartInstance +
AllocatePublicIpAddress）→ cloud-init → 服务端自检 ok → 新配置写盘 →
**用新配置实测 `generate_204 = 204`，出口为新 IP**。从删除到新服务器可用约 2 分钟。

---

## 变更日志

- 2026-09-19 初始化：SPEC.md + CLI 骨架 + 24 个单测通过。
- 2026-09-19 核对客户端兼容性（mihomo 官方文档实拉）；新增 `vless_flow` 降级开关与
  客户端-服务端参数契约测试；本文件建立。
- 2026-09-19 M1 离线收口：`find_image` 改为 `ImageName=<前缀>*` 查询 + 多过滤器 fallback；
  新增 `image_name_filters`；新增 `tests/test_ecs_request.py`（把
  SpotStrategy / 计费 / 磁盘 / VSwitchId / UserData / 安全组规则 / 镜像选择全部离线锁死）；
  单测 36 个全过。仍待实跑：真实创建、`xray x25519` 输出格式、`xray run -test`。
- 2026-09-19 接入用户提供的 vSwitch + 安全组：新增 `_zone_candidates` /
  `_resolve_security_group`（vSwitch 钉死可用区、安全组走"缺啥补啥"且标记为借用）、
  `state.json` 加 `security_group_owned`、`down`/回滚不再删借用资源、
  `--orphans` 清理时跳过配置里的自有安全组；新增 `tests/test_reuse_resources.py`，
  单测 45 个全过。
- 2026-09-19 **事故复盘**：用户实跑报错，但既没有 state.json 也没有终端输出可查。
  三处改动：(1) `main()` 增加文件日志 + 顶层异常兜底（原来未预期异常会直接抛栈且不留痕）；
  (2) `doctor --api` 改为按 `up` 的真实调用顺序做 preflight（区域 → 可用区 → 交换机 →
  安全组含规则/同 VPC → 镜像 → 密钥对），第一个红叉即卡点；
  (3) 修 README 最小权限策略缺 `ecs:DescribeSecurityGroupAttribute` —— 这是本次
  复用安全组新引入的调用，旧策略会导致 `Forbidden.RAM`，**是本次报错的头号嫌疑**。
  新增 `tests/test_doctor.py`，单测 50 个全过。
- 2026-09-19 **第一次真机失败的完整根因（用户贴了日志）**，两个 bug，前者把后者盖住了：
  1. `log.info("安全组 {} 已满足要求", existing)` 把 `{}` 占位符和 %-参数混用，
     logging 在 emit 时抛 `TypeError` 并打印一大坨 "Logging error" 堆栈，
     把真正的报错淹没了。已修，并新增 `tests/test_logging_hygiene.py`（AST 静态扫描，
     全包禁止这种混用，附"扫描器有效性"自检）。教训：中文日志里也只用 %-style。
  2. 真正的错：`InvalidParam.SourceCidrIp: The specified parameter SourceCidrIp is not valid`。
     根因是自动探测到的公网 IP 可能是 **IPv6**（macOS 双栈优先走 IPv6，
     `ifconfig.me/ip` 就会回 IPv6），而阿里云 `SourceCidrIp` **只接受 IPv4**，
     `240e:.../32` 直接非法。三处修复：
     - `_extract_ipv4()` 用正则 + `ipaddress` 过滤，只认公网 IPv4（顺带拒绝私网/环回/保留）；
     - `IP_ECHO_URLS` 顺序按"中国大陆实际可达"重排，并实测过：
       `api.ipify.org` 在用户机器上 **HTTP=000 直接失败**，
       `members.3322.org/dyndns/getip` / `myip.ipip.net` / `ifconfig.me` / `api.ip.sb` 均 200；
       另加 DNS 兜底（opendns / google / akamai）；
     - 新增 `_normalize_cidr()`，在调 API 之前校验并归一化，IPv6 直接给出可读的中文解释。
       `--ssh-from` 也走同一条校验。
  3. 顺带：失败发生在创建实例之前，`_rollback` 现在会明说"本次没有需要清理的资源"，
     不再打出令人误解的"清理本次创建的资源"。
  单测 67 个全过。
- 2026-09-19 **第二次真机失败**：`InvalidParameter: The specified parameter
  "InternetMaxBandwidthIn" is not valid.`（action=CreateInstance）。
  根因：`InternetMaxBandwidthIn=-1`（不限速）是 **RunInstances 的写法**，
  `CreateInstance` 不接受；而按流量计费本来就不对入方向计费，压根不需要传。
  修复：默认**不传**该参数，新增 `bandwidth_in` 配置项供显式指定（0 = 不传）。
  同时删掉 `DeletionProtection`（同样是 RunInstances 时代的参数，且会挡住我们自己的
  `down --force`）。另外 `AliyunError.hint()` 现在能从报错里提取被拒的参数名，
  直接告诉用户"哪个参数不合法"。给 `HostName` 加了字符归一化（它和 `InstanceName`
  的规则不同，含空格/下划线会被拒）。
  第二次失败前的**成功链路已被日志证实**：IP 探测拿到 IPv4 `203.0.113.20`（走 3322）、
  镜像命中、密钥对已存在、vSwitch/安全组复用与规则补全都正常。
  单测 68 个全过。
- 2026-09-19 **第三次真机尝试：买到了，但 6 秒后 `Stopped`**。
  实例 `i-00000000000000000002`，状态 `Pending` → `Stopped`，
  当时 `wait_running` 只能干等 240s 超时，用户看到的就是"卡住"。
  根因是**我自己的一个静默妥协**：配置默认 `spot_duration = 1`（想要 1 小时保护期），
  但阿里云不接受 `SpotAsPriceGo` + 保护期，我就在代码里把它"忽略"了，只留一行日志。
  结果丢了保护期，实例可以被瞬间回收 —— 日志里那句
  "SpotAsPriceGo 下忽略 spot_duration=1" 就是这个隐患的现场。
  **教训：宁可让用户看到冲突并做选择，也不要静默降级一个安全属性。**
  修复：
  - 新增 `EcsClient.spot_price()`（`DescribeSpotPriceHistory`，取近期**峰值**做参考）；
  - 新增 `cli._resolve_bid()`：默认 `SpotWithPriceLimit`，出价 = 市场价峰值 × 1.15；
    拿不到价格才降级到 `SpotAsPriceGo`，**并且大声警告**；
  - `_plan()` 里"要保护期"直接蕴含"用限价策略"，不再静默忽略；
  - 默认配置改成 `spot_strategy = SpotWithPriceLimit`。用户磁盘上的旧 config.toml
    写的是 `SpotAsPriceGo`，但因为 `_plan()` 会依据 `spot_duration > 0` 升级策略，
    **旧配置同样能拿到保护期**（已用他的真实 config.toml 跑 dry-run 验证：
    `SpotWithPriceLimit：出价上限 自动（市场价峰值 × 1.15），1 小时保护期`）；
  - `wait_running` 遇到 `Stopped/Stopping` **立即失败**且带上 `StoppedMode` 与锁定原因
    （新增 `instance_details()`），不再让用户盯着卡住的提示；
    新增错误码 `InstanceStoppedEarly` 与对应 hint（抢占回收 / 欠费两种可能）；
  - 实例创建成功后**立刻落盘 state.json**（`ready: false`）再开始等待。
    此前若在等待阶段崩溃，实例会"存在但无记录"，只能靠 `down --orphans` 兜底；
  - `doctor --api` 增加 `ecs:DescribeSpotPriceHistory` 体检项，README 最小权限策略同步补上；
  - 新增 `tests/test_spot_bidding.py`。
- 2026-09-19 测试隔离：新增 `tests/sandbox.py`，**所有碰 `state` 的测试文件必须先
  `import sandbox`**。此前这些测试会把开发者本机真实的
  `~/.config/vpnctl/config.toml` 合并进来（正是它让"默认策略已改"的断言失败），
  典型的"本地过、换台机器就挂"。单测 82 个全过。
- 2026-09-19 新增**服务端回环自测**（由用户那句"你能不能拿到 AK/SK 直接建机器测试"引出）：
  bootstrap 多写一份**客户端**配置 `/root/vpnctl/client-test.json`
  （vnext 指向 `127.0.0.1:<port>`，REALITY 复用同一组 pbk/sid/sni），
  再由 `/opt/vpnctl/selftest.sh` 起一个临时 Xray，通过它的 socks5 端口
  `curl https://www.gstatic.com/generate_204` 要一个 204。结果写入
  `server-info.json` 的 `selftest` 字段；`_finish_up` 显式打印"通过/未通过"，
  并新增 `vpnctl selftest` 子命令随时复跑。
  "就绪"第一次有了真正的含义：**不只是装完了，而是真的能代理**。
  测试在 `tests/test_bootstrap.py` 里按 bash 展开规则替换 heredoc 后 `json.loads`，
  确保 Xray 收到的是合法 JSON（这类拼装最容易悄悄写坏）。单测 84 个全过。
- 2026-09-19 用户提出授权我直接操作阿里云账号（给 RAM AK/SK）。我的立场：
  **不给也能推进**（他跑、我读日志）；要给就必须写进 `~/.aliyun/config.json`
  而不是贴进聊天，且用**一次性 RAM 子用户、用完即删**。
  我的运行时状态放 `workspace/d/.vpnctl-run/`，不碰他真实的 `~/.config/vpnctl/`
  （那里面有他的密钥和配置）。
- 2026-09-20 **M4 实跑通过：`vpnctl watch` 自愈**。
  过程：`watch --interval 20` → 手动删除实例模拟竞价回收 → 20 秒后第 1 次查不到
  → 再 20 秒第 2 次 → 判定回收 → 自动重建（`i-00000000000000000003`，IP `203.0.113.30`）
  → 自动 StartInstance + AllocatePublicIpAddress → cloud-init → 服务端自检 ok
  → 新配置写盘 → **从用户 Mac 用新配置实测 `generate_204 = 204`，出口为新 IP**。
  从删除到新服务器可用约 2 分钟。
  过程中修掉两个真问题：
  1. **watch 不能拿 `DescribeInstanceStatus` 做决策**——它可能忽略过滤条件返回别的
     实例的状态。改为用裸列表（`find_in_listing`）判断存活，且**连续 2 次查不到**
     才动手，避免单次 API 抖动误重建。
  2. **守护进程里 `print()` 是块缓冲的**：输出到管道时"=== 就绪 ==="和新配置会一直
     卡在缓冲区不显示（实跑就是这么被坑的，日志里只有轮询行）。已在 `_setup_logging()`
     把 stdout/stderr 设为行缓冲。
  另新增 `_recreate_safely()`：重建失败只记日志、等下一轮，不让守护退出
  （竞价被回收是常态，重试才是它的职责）。
  新增 `tests/test_watch.py`，单测 106 个全过。
- 2026-09-20 **待改进（等用户决定）**：每次重建都会换一整套 UUID/REALITY 密钥，
  所以客户端**必须重新导入配置**。想免掉这一步，就要把 UUID + shortId + REALITY 私钥
  持久化到本地并注入新实例的 userdata——代价是私钥落盘在本机。未实现。
- 2026-09-20 **A4+A5 交付并实测（用户要求"给一个能用的客户端"）**：
  - 新增 `src/vpnctl/proxy.py`：用 mihomo 内核连接。**协议/内核不自己写**，
    我们只管生成配置、起停内核、开关系统代理、校验出口。
  - 新增 CLI：`connect` / `disconnect` / `proxy-status`。
  - 新增 `src/vpnctl/identity.py`：凭据持久化（UUID + REALITY 密钥对 →
    `~/.config/vpnctl/identity.json` 0600），重建时注入新实例，**客户端不再需要重导配置**。
    密钥用 openssl 生成（实测与 xray 完全兼容）。
  - 新增 `app/build-macos.sh`：用 **swiftc + WKWebView** 打包成 `dist/vpnctl.app`
    （CommandLineTools 就够，不需要装 Xcode）。壳只做三件事：起 agent、开窗口、装界面。
  - **实测记录**：`up` → `connect` → 出口 IP = 服务器 IP；
    然后**直接删掉实例模拟竞价回收** → agent 的守护在 ~30s 内发现 →
    自动重建（新实例 + 自动起停 + 自动分配 IP）→ ~2 分钟后服务端就绪 →
    **代理自动切到新 IP，全程零操作**。
  - 顺带三个真 bug：
    1. **SSH 主机密钥**：阿里云会把回收的 IP 再分配，`accept-new` 对变更过的密钥是拒绝的，
       `wait_ready` 于是重试到超时（实测卡了 8 分钟）。已改成识别该错误 →
       清掉 known_hosts 里那条 → 重试一次。
    2. **`server-info.json` 里的 `address` 为空**（阿里云元数据 `/public-ipv4` 返回 404），
       只有 `up` 补了这一处，`sub --refresh` 没补 → 生成的链接是 `@:443`。
       已抽成 `_normalize_server_info()` 统一处理。
    3. **`networksetup` 在管理员账户下免密码就能改系统代理**（实测退出码 0）——
       原来一律走 osascript 弹授权框是过度设计，已改成"先免密，失败才要密码"。
  - 关窗口**不**停 agent（否则合上窗口就没人管竞价回收了）。
    要停：`pkill -f app/agent.py`。
- 2026-09-21 **GUI 层连环 bug（用户截图才发现，单元测试全抓不到）**：
  1. **`window.confirm()` 在 WKWebView 里静默失效**——宿主没实现 `WKUIDelegate`
     时既不弹窗也不返回，JS 直接卡住，按钮看起来"点了没反应"。修：宿主实现
     `WKUIDelegate`（alert/confirm/prompt 都接到原生弹窗）**并且** UI 改用页面内
     确认框，不依赖宿主。
  2. **CSS 特异性坑**：`.hide { display:none }` 写在 `.overlay { display:flex }`
     前面，后者胜 → 那个空对话框一直挂在页面上（用户截图里那个悬空灰方块）。
     修：加 `.overlay.hide { display:none }`。教训：**同类选择器靠顺序决定胜负，
     别把通用工具类写在组件类前面**。
  3. **连接按钮压根没接线**：HTML 里是写死的 `disabled` + 占位文案
     "连接管理开发中（A4）"，JS 里也没有它的 onclick——后端 `/api/connect` 早就好了。
     修：按钮文案由状态驱动（连接/断开/还没有可用的服务端），并接上 onclick。
  4. **`down` 不清理本地**：机器删了、mihomo 还在跑、系统代理还开着指向死端口
     → 用户的网静默断掉，还以为是别的问题。修：`down` 先断开本地（停内核 + 还原系统代理）。
  5. **agent 是长驻进程**：改了后端代码必须重启 agent 才生效——验证时踩过一次，
     差点误判修复无效。App 重启会拉起新 agent；命令行起的要 `pkill -f app/agent.py`。
  6. **verify 瞬时失败会掐断后续步骤**：`/api/connect` 里先做出口校验再武装自动重连，
     校验一失败（内核刚起来时常见）守护就没武装上。修：**先武装守护，再校验**，
     并且校验失败重试一次。
  **教训**：这一轮 6 个 bug 全在 GUI/胶水层，`unittest` 一个都抓不到。
  以后改界面必须"从界面真点一遍"，光跑测试不算验证。
- 2026-09-21 **让整机断网的严重 bug（用户报"联网失败"）**：
  日志显示同一次启动里 `resolve_port()` 被调了两次——`start()` 把内核起在 7898，
  紧接着 `reconcile()` 再调一次，把**内核自己占的 7898** 当成"端口冲突"换成 7899，
  于是系统代理被指向 7899 这个没人监听的端口 → **用户整台机器的浏览器全部连不上**。
  修法与教训：
  1. `resolve_port()`：**内核在跑就直接用它记录的端口，绝不再去探测**（`applied.json`
     里同时存 fingerprint 和 port）。
  2. `set_system_proxy(on)` 里加硬保险：**端口没在监听就拒绝设置**，直接抛错。
     ——这类失误的代价是整机断网，宁可不连也不能指错。
  3. `reconcile()`：内核起不来时，**把系统代理关掉**（fail-open 到直连），
     而不是把用户挂在死端口上。
  4. `proxy.status()/verify()/exit_ip()` 的默认端口不能用写死的常量，
     要用 `resolve_port()`（界面之前显示 7897、实际在 7899，排查时会一头雾水）。
  回归测试在 `tests/test_reconcile.py::PortTests`。
- 2026-09-21 **可靠性重构（用户要求"从稳定性、状态控制、边缘问题考虑"）**：
  - 新增 `src/vpnctl/status.py`：**状态的唯一来源**。以前 CLI/agent/UI 各算一套，
    自然会互相矛盾。现在是一个状态机（`no_machine / busy / installing /
    install_failed / ready / connected / degraded / reclaimed`）+ 一个 `snapshot()`。
  - **声明式期望状态 + 调和循环**：`want_connected` 是期望，`proxy.reconcile()`
    每 8 秒把实际收敛过去——内核崩了、服务端换地址了、系统代理被人手动关了，
    都会自己纠正。升级安全：老配置没有 `want_connected` 时以"内核是否在跑"为准，
    否则升级上来一次调和就会把用户的连接掐掉。
  - 端口自动避让（7897 是 Clash Verge 的默认口，很容易撞）、agent 端口被占自动换、
    `install_failed` 状态（装太久没就绪 → 界面提示重建而不是无限转圈）、
    HTTP 服务的 ConnectionReset 噪音静音、守护可开关（界面上的"自动重建"）。
- 2026-09-21 **装了 `playwright` + `screenshot` skill** —— 这是转折点：
  **我终于能自己打开界面、点击、截图**（`view_image` 看渲染结果）。
  之前 6 个 GUI bug 全是因为"我看不见"。用法：
  `PWCLI=~/.codex/skills/playwright/scripts/playwright_cli.sh; "$PWCLI" open http://127.0.0.1:8765/;
   "$PWCLI" screenshot --filename /tmp/shots/x.png --full-page`
- 2026-09-21 **界面重写（用户要求"友好性、响应速度、过程动画"）**：
  按状态驱动渲染（8 个状态各有图标/颜色/文案）、动画进度条+步骤脉冲、
  按钮忙碌态转圈、非阻塞 toast 取代弹窗（破坏性操作仍弹页面内确认框）、
  顶部错误横幅、后台守护面板（自动重建开关 + 自愈开关 + 内核/系统代理/端口实时状态）。
- 2026-09-21 **收尾三项**：
  1. **自动重建默认开**（`watch_enabled` 落在配置里，重启记得住）——这是这个方案值钱的地方，
     不该让用户自己去开。
  2. **实时流量/连接数**：mihomo 配置里加了 `external-controller-unix`（用 unix socket，
     不占端口不会冲突），agent 每 2 秒采样一次算速率；控制接口在 `proxy.stat()/traffic()`。
     **注意**：加控制接口后旧内核要重启才会带上，否则 `traffic.available=false`。
  3. **手机订阅入口**：agent 直接提供 `http://<本机局域网IP>:8765/sub/<token>`，
     base64 的 vless 订阅；机器换 IP 时内容自动跟着变，**不需要域名、不需要对象存储、零成本**。
     另有 `/api/sub-qr.png` 出二维码（用纯 Python 的 PyPNG 后端，不依赖 Pillow）。
     局限要说清楚：手机得和电脑在同一个 Wi-Fi 下；要在蜂窝网也自动更新，还是得上 OSS。
  顺带修的两个坑：
  - **同一作用域里两次 `const sub`**（解构 + 订阅变量）→ 语法错误导致**整个脚本不执行**，
    界面永远停在"正在读取状态…"。`playwright` 的 console 一下就指出来了。
  - 浏览器**缓存了旧的坏页面**，改完必须用 `?v=时间戳` 强刷才看得到效果。
  - 给页面加了内联 SVG favicon，消掉 `favicon.ico 404` 的噪音。
- 2026-09-21 **App 图标 + 界面收尾**：
  - 图标用**代码生成**（`app/make-icon.py`，纯 Python + pypng，按距离场算覆盖度做抗锯齿）：
    深色圆角方 + 祖母绿地球。没走 imagegen，因为 ① 本会话没有内置 `image_gen` 工具，
    ② skill 自己也说"简单几何图标更适合代码确定性生成"。
    `build-macos.sh` 会用它生成 `.icns`（sips + iconutil）并写进 `CFBundleIconFile`。
  - 界面加了：标题栏图标、**流量曲线**（最近 40 次采样的下载速率）、
    **骨架屏**（首次拿到状态前不再闪一排 "—"）、空状态引导（没机器时给"去购买"按钮）、
    出口 IP 一键复制。
  - **打包时新增界面脚本语法检查**（node `new Function`）——这类错误单元测试完全看不见。
  - 又踩了两次同一个坑：**改 DOM 结构却没同步改 JS 引用**（把 header 的状态点换成图标，
    但 render() 还在给 `$("dot")` 赋值 → 每 3 秒一次 TypeError、渲染中断）。
    现在 render 内所有元素访问都走 `need()`（元素缺失返回哑对象），一处缺失不再拖垮整页。
    教训：**GUI 的失败模式是"静默中断"，任何一处异常都会让后面全部不执行**，
    所以要么加保护，要么每次改完都从界面真跑一遍。
- 2026-09-21 **界面重做：从"工具"到"作品"（用户指出像大学生作业）**。
  用户的原话是对的，诊断出的病根按严重程度：
  1. **用 emoji 当图标**（🛡️💤📦⚠️）——这是"作业感"的头号来源。全部换成**自绘 SVG 图标集**
     （24×24 网格、1.7 线宽、`<symbol>` + `<use>`，15 个图标）。
  2. **满屏同宽卡片堆叠**，没有节奏 → 改成一屏一个"主视觉" + 下方**双栏网格**。
  3. **没有视觉主角** → 新增**状态光环**：SVG 圆环（进度弧）+ 品牌色图形（盾牌/地球/服务器），
     连接时发光晕、安装时扫光、忙碌时脉冲。Emoji 全部退场。
  4. **没有字阶** → 加了 `.micro`（11px / 字距 .09em / 大写）做小标签，
     数值统一等宽 + 右对齐（tabular-nums），主角文案 27px/680 字重/负字距。
  5. **色板散乱** → 令牌化：只在一层定义颜色，组件里不出现裸色值；
     品牌色沿用图标的祖母绿（#3ddc97），警告/危险/信息各一色，
     靠"表面明度"（surface → surface-2 → surface-3）而不是描边来分层。
  6. 主按钮改成祖母绿渐变 + 内高光 + 彩色投影；导航改成**分段控件**（胶囊）；
     卡片加 1px 发丝线 + 极淡的顶部内高光 + 分层阴影。
  7. 细节：骨架屏、曲线上渐变填充 + 末点、toast 带图标、
     空状态给"去购买"按钮、机器页补了"预估花费"。
  顺手修了一个逻辑错：机器就绪后"安装进度"仍显示全部待办 → 现在就绪即全绿。
  **验证方式**：`playwright` 截图逐页看（连接/机器/设置），控制台 0 错误。
  教训：**"好看"不是审美问题，是信息层级问题**——emoji、同权重的卡片、
  没有字阶，这三样凑齐就一定是作业感。
- 2026-09-21 **响应速度优化（用户要求）+ 命名（驿）**。先量基线再改：
  - 基线：`/api/state` 平均 **18ms**；界面**每 3 秒盲轮询**一次
    （所以状态变化最多 3 秒才上屏——这才是"感觉慢"的真正原因）。
  - **① 推送取代轮询**：agent 每秒算一次「便宜的状态指纹」（只读缓存，不打阿里云），
    变了就通过已有的 SSE 发一条 `state` 事件；界面收到后 120ms 防抖刷新。
    实测**变化后 187ms 到达前端**（原 3000ms 上限，快 16 倍）。
    客户端轮询降级为 15 秒兜底（推送断了也能自愈）。
  - **② 砍掉每次请求的固定开销**：
    `system_proxy_enabled()` 每次要起一个 `scutil` 子进程（十几毫秒），
    而状态接口每秒都可能被问一次 → 加 2 秒缓存（写操作会立刻失效缓存）。
    配置/状态文件的读取按 mtime 缓存（一次请求里 `load_config()` 会被调用好几次）。
    实测 `/api/state`：**18ms → 1.8ms**。
  - **③ 别每 3 秒重建 DOM**：安装步骤列表按内容指纹跳过重建，
    否则会把正在播放的脉冲动画打断。
  - 顺带：同一次动作会同时收到「接口回执」和「推送」两条一样的提示 → toast 去重。
