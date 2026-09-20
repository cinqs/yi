# AGENTS.md

给**协作者和 AI 编码助手**看的操作手册。要求：**短、准、可执行**。
历史流水与踩坑全过程不在这个文件里，见文末「文档地图」。

## 项目

**驿 · Yi** —— 一条命令买下阿里云香港**抢占式（竞价）实例**，自动装好
`VLESS + XTLS-Vision + REALITY` 服务端，并在本机拉起代理内核、开系统代理。

名字取自古代**邮驿**制度：文书传递靠沿途驿站换马续行，**马换、路不换**。
这里换的是被回收的竞价实例——**机器换了，客户端配置不换**。

```bash
./yi up          # 买机器 → 装服务端 → 回环自检 → 生成客户端配置
./yi connect     # 起 mihomo 内核 + 开系统代理
./yi status      # 状态与出口 IP
./yi down        # 销毁，停止计费
```

### 架构一句话

`Yi.app`（Swift + WKWebView，只负责显示）← HTTP + SSE，仅监听 `127.0.0.1` →
`agent.py`（Python 3.12，**唯一的大脑**）→ `mihomo`（**成熟开源内核，代理协议不自己写**）。

## 环境与命令

Python 3.12 + [uv](https://docs.astral.sh/uv/)。没有 `uv` 但仓库里有 `.venv/` 时，
Makefile 会自动退回用 `.venv`。

| 命令 | 作用 |
|---|---|
| `make check` | **提交前必跑**：脱敏 → 文档链接 → 界面脚本语法 → shell 语法 → ruff → 全部单测 |
| `make test` / `make lint` / `make fmt` | 单项 |
| `make app` | 打包 `dist/Yi.app`（`swiftc` 直编，**不需要 Xcode**） |
| `make site` | 本地预览 GitHub Pages 站点（`docs/`） |
| `./yi doctor --api` | 凭据 / 权限 / 交换机 / 安全组 / 镜像 / 竞价价格逐项体检 |
| `./yi up --dry-run` | 只打印它打算买什么，不花钱 |
| `./yi fetch-kernel` | 下载代理内核（不入库；`--url` 可绕墙、可指定版本） |

数据目录 `~/.config/yi`（0700/0600）。老版本的 `~/.config/vpnctl` 会**自动迁移**，
所以 `state.py` 里那个旧路径常量是**故意保留**的，删了迁移就废。

## 不可动摇的约束

改动前先确认没有违反其中任何一条。这些都不是风格偏好，是踩过坑之后立的规矩。

1. **不自己实现代理协议**。内核一律用成熟开源实现（本机 `mihomo`，服务端 `Xray-core`）。
   本项目只负责"生成配置、拉起内核、开关系统代理、校验出口"。
2. **两端客户端必须同时可用**：Android = v2rayNG，macOS = Clash Verge Rev(mihomo)。
   任何服务端参数改动都要同时检查两份客户端配置，并跑 `tests/test_client_contract.py`。
3. **服务端只跑一份配置模板**。实例内的 `config.json` 与本地生成的客户端配置必须来自
   同一组参数（UUID / flow / security / sni / pbk / sid / port），不允许手工分叉。
4. **私钥不出机**。REALITY 私钥与 SSH 私钥都不离开实例/本机；客户端只拿公钥。
5. **只用竞价实例**。`CreateInstance` 是唯一支持 `SpotStrategy` 的接口，
   别改成 `RunInstances`（会直接丢掉竞价能力）。
6. **运行时零第三方依赖**。只用标准库，`requires-python = ">=3.12"`（要用 `tomllib`）。
   不要再为 3.8 写兼容代码。
7. **别人的资源不许删**。用户自己填进配置的 vSwitch / 安全组属于用户资产，
   `down` 与失败回滚一律不删，判断依据是 `state.json` 的 `security_group_owned`。
8. **绝不静默降级安全属性**。参数冲突（例如保护期 vs 出价模式）要么报错让用户选，
   要么按更安全的一侧升级并大声记日志。历史上就是静默忽略保护期导致实例被秒回收。
9. **绝不把系统代理指向没有监听的端口**。这类失误的代价是**用户整机断网**，
   宁可不连。`set_system_proxy()` 里有硬保险，别绕过它。
10. **不自研 Android 客户端**。Android 端要做的是 `VpnService` + 内核集成 +
   证书管理一整套，自己重写只会更差。用 v2rayNG，我们只负责**把客户端交到
   用户手上**（`yi android` 取官方 APK + 生成订阅）。

## 代码地图

| 路径 | 职责 |
|---|---|
| `src/yi/aliyun.py` | 纯标准库阿里云 RPC 客户端：HMAC-SHA1 签名、重试、错误码中文提示 |
| `src/yi/bootstrap.py` | cloud-init：装 Xray、开 BBR、生成密钥、**服务端回环自检** |
| `src/yi/configgen.py` | 生成 vless 链接 / v2rayNG 订阅 / mihomo.yaml / singbox.json |
| `src/yi/cli.py` | 命令编排与资源回滚 |
| `src/yi/proxy.py` | mihomo 内核的获取与生命周期、系统代理开关、出口校验、流量采样 |
| `src/yi/identity.py` | 凭据持久化（UUID + REALITY 密钥对）→ 重建后客户端不用重配 |
| `src/yi/android.py` | 取 v2rayNG 官方 APK（**不自研 Android 客户端**，理由见下） |
| `src/yi/status.py` | **状态的唯一来源**：8 状态机 + `snapshot()` |
| `src/yi/state.py` | `~/.config/yi` 下的配置与状态（含旧目录自动迁移） |
| `app/agent.py` | 本地 HTTP + SSE 服务，界面唯一的后端 |
| `app/ui/index.html` | 单文件界面（自绘 SVG 图标、状态光环、流量曲线） |
| `app/macos/main.swift` | 原生外壳（WKWebView + `WKUIDelegate`） |

## 客户端兼容性

| 客户端 | 平台 | VLESS | Vision flow | REALITY |
|---|---|---|---|---|
| v2rayNG | Android | ✅ | ✅ | ✅ |
| Clash Verge Rev (mihomo) | macOS | ✅ | ✅ | ✅ |
| sing-box | — | ✅ | ⚠️ 未实跑确认 | ✅ |

由此有三条硬规则：

**① `flow` 必须是一键可关的开关**。配置项 `vless_flow` 置空时，服务端与所有客户端
同时去掉 `flow`，用于任何客户端不兼容 Vision 时降级（牺牲性能换连通性）。

**② REALITY 的伪装目标（dest）不能硬编码**。证书链超过服务端缓冲上限就装不完，
客户端会报 `received real certificate` 然后连不上。所以 `reality_dests` 是**候选列表**，
bootstrap 逐个写配置 + 起服务 + 回环自检，用第一个真正通的。实测
`www.microsoft.com` 证书链 8273 字节直接失败，`www.cloudflare.com` / `www.bing.com` /
`www.apple.com` 可用。

**③ 服务端 Xray 版本必须 ≥ 客户端版本**。REALITY 会把客户端版本写进握手，服务端按
`maxClientVer` 校验。客户端版本我们控制不了，所以服务端默认装 `latest`。

## 怎么验证

**单元测试全绿 ≠ 没问题。** 这个项目出过 6 个 GUI 层的 bug，`unittest` 一个都抓不到。

| 改动类型 | 必须做的验证 |
|---|---|
| 任何改动 | `make check` |
| 界面（`app/ui/index.html`） | **用 playwright 真打开、真点、真截图**，并看 console 有没有报错 |
| 生成的 mihomo 配置 | **字符串断言证明不了内核认它。** 有内核时 `tests/test_mihomo_config.py` 会真跑 `mihomo -t`；改规则后确认它没被 skip |
| bootstrap 脚本 | 渲染后 `bash -n`（CI 里有这一步） |
| 服务端参数 | `./yi selftest`（在服务器上自己当客户端连自己） |
| 竞价/生命周期 | `./yi up --dry-run`，必要时真跑一次再 `./yi down` |

界面验证的关键能力（**这是转折点，之前 6 个 GUI bug 全是因为"看不见"**）：

```bash
PWCLI=~/.codex/skills/playwright/scripts/playwright_cli.sh
"$PWCLI" open http://127.0.0.1:8765/
"$PWCLI" screenshot --filename /tmp/shots/x.png --full-page
"$PWCLI" console          # 页面异常一定要看这个
```

改完界面**必须用 `?v=时间戳` 强刷**，否则浏览器缓存会让你看到旧页面而误判修复无效。

## 已知深坑（复发过的）

1. **agent 是长驻进程**。改了后端代码必须重启才生效：
   `pkill -f app/agent.py`（App 重启会自己拉起新的）。
2. **改 DOM 必须同步改 JS 引用**。漏一处 → 抛异常 → **整个脚本静默中断**，
   界面永远停在"正在读取状态…"。`render()` 里的元素访问一律走 `need()`。
3. **同一作用域里别重复 `const` 声明**（`const sub` 撞名两次就够让整页脚本不执行）。
4. **CSS `:hover:not(:disabled)` 的权重会反超单个修饰类**。
   给 `.primary` 加了渐变，一悬停就被通用规则盖成灰的（但投影还在，看起来像"灰按钮+绿光"）。
   变体必须自己声明 `:hover` / `:active`。
5. **`resolve_port()` 不能把自己内核占的端口当冲突**，否则系统代理指向死端口。
   内核在跑就直接用它记录的端口，不再探测。
6. **日志不要混用 `{}` 占位符和 `%` 参数**。会抛 `TypeError`，把真正的报错淹掉。
7. **不要相信单次 API 返回**。阿里云不同 POP 数据会不一致（按 ID 查不到、裸列表查得到）。
   判断"资源还在不在"要用裸列表 + 本地过滤，且**连续 2–3 次**确认才能判定消失。
8. **批量改名会改到启动器自己**。`perl -pi -e 's/vpnctl/yi/g'` 把
   `exec "$HERE/vpnctl"` 改成 `exec "$HERE/yi"` → 自己 exec 自己、无限循环，
   而进程数一直是 1，`ps` 看着完全正常。
9. **`xray run -test -config` 按文件扩展名判断格式**，临时配置必须以 `.json` 结尾。
10. **别用"本机装了包"的解释器验证打包布局**：开发机跑过 `pip install -e .`，
   `import yi` 永远成功，会把"打包后路径算错"盖住。`tests/test_app_bundle.py`
   用 `python -S` 起干净解释器 —— 去掉 `-S` 这个测试就永远绿。

## 约定

- 提交前跑 `make check`（脱敏 → 文档链接 → 界面语法 → shell 语法 → ruff → 134 个单测）。

  改 `app/ui/index.html` 要顺带跑 `node` 那步（`make check` 里有）；
  打包 App 时 `build-macos.sh` 也会再查一次界面脚本语法。**这类错误单测抓不到。**
- **任何 import `state` 的测试文件，第一件事是 `import sandbox`** ——
  它把 `YI_HOME` 指到临时目录。否则测试会把你本机真实的
  `~/.config/yi/config.toml` 合并进来，典型的"本地过、换台机器就挂"。
- **输出文案用中文；代码注释与标识符用英文。**
- 任何"我猜的"外部事实，必须标成 `待实跑确认`，不要写成结论。
- 所有日志落盘到 `~/.config/yi/logs/`，因为终端回滚抓不到。
  CLI 顶层对所有异常都打完整堆栈并回报日志路径。
- 每给用户新增一个 API 调用，同步三处：README 的最小权限策略、
  `_api_preflight()` 的体检项、本文件的清单。
- **不要提交真实凭据与真实云资源 ID**。`tools/check-no-secrets.sh` 会自动拦
  （`make check` 已包含）。测试与文档里用 `vsw-00000…` 这类占位值。

## 文档地图

| 文件 | 内容 |
|---|---|
| [docs/quickstart.md](docs/quickstart.md) | 从零跑起来 |
| [docs/architecture.md](docs/architecture.md) | 三个进程、状态机、期望状态与调和循环 |
| [docs/lessons.md](docs/lessons.md) | **踩坑提炼**：真实世界的坑与我方代码的锅 |
| [docs/history.md](docs/history.md) | 按时间顺序的工程日志（含实跑验收记录） |
| [docs/design.md](docs/design.md) | 界面设计系统（令牌、字阶、状态光环、动效） |
| [docs/troubleshooting.md](docs/troubleshooting.md) | 连不上时按症状查 |
| [docs/faq.md](docs/faq.md) | 常见问题 |
| [docs/roadmap.md](docs/roadmap.md) | 路线图与**明确不做**的事 |
| [docs/RELEASE.md](docs/RELEASE.md) | 发版检查单 |
| [CHANGELOG.md](CHANGELOG.md) | 面向用户的版本变更 |
| [SPEC.md](SPEC.md) / [app/SPEC.md](app/SPEC.md) | 需求与设计规格 |
