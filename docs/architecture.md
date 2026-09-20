# 架构与设计决策

## 一句话

**薄编排层 + 成熟内核**：不自己写代理协议，只负责"买机器、装服务端、
把状态收敛到期望的样子、让界面看得见"。

## 三个进程

```
驿.app（Swift + WKWebView，不到 1 MB）       ← 只负责显示与点击
   │ HTTP + SSE on 127.0.0.1
agent.py（Python 3.12，零第三方依赖）      ← 唯一的"大脑"
   ├── 调阿里云 OpenAPI（自己实现签名）
   ├── 通过 SSH 引导服务器（cloud-init）
   └── 管 mihomo 子进程
mihomo（成熟开源内核）                     ← 真正的代理
```

**为什么 agent 和界面分开**：agent 可以被 CLI、手机端、任何界面复用；
界面用什么技术都不影响后端。今天的外壳是 Swift + 系统 WebView，
哪天想换壳也只是换壳。

**为什么用系统 WebView 而不是 Electron/Tauri**：macOS 自带 WebKit，拿它当渲染层，
应用不到 1 MB（Electron 约 200 MB、Tauri 约 10–20 MB），启动也最快。
壳里没有任何网络逻辑，所以它几乎不可能出安全问题。

## 状态的唯一来源

`src/yi/status.py` 把"现在到底什么情况"收敛成一个状态机 + 一个快照函数。
CLI、agent、界面读**同一份**（早期版本各算一套，于是界面会撒谎）。

```
no_machine ──up──▶ installing ──就绪──▶ ready ──connect──▶ connected
     ▲                 │                  ▲                    │
     │             安装超时               │            内核挂了/出口不对
     │                 ▼                  │                    ▼
     └──down──▶ install_failed           └──────────────▶ degraded

机器被回收：reclaimed ──watch 自动重建──▶ installing
有操作在跑：busy（provisioning / connecting / destroying …）
```

判断顺序很重要：**busy > reclaimed > install_failed > installing > connected >
degraded > ready > no_machine**。"正在装"必须盖过"已就绪"，"被回收"必须盖过"已连接"。

## 期望状态 + 调和循环

可靠性的核心。**不要写"点一下做一串动作"**——那一串里任何一步失败就会烂在原地。

改成声明式：用户点"连接"只写一个期望 `want_connected = true`；
后台每 8 秒跑一次 `proxy.reconcile()`，把实际状态收敛到期望状态。

它处理的四种偏差：

| 偏差 | 动作 |
| --- | --- |
| 期望连、内核没跑（崩了/被杀） | 重新拉起 |
| 期望连、内核配置指向旧服务端（机器被重建过） | 用新配置重启 |
| 期望连、系统代理被外部关掉 | 重新打开 |
| 期望断、内核还在跑 | 停掉并还原系统代理 |

**升级安全**：老配置没有 `want_connected` 字段时，以"内核是否在跑"为准，
否则升级上来第一次调和就会把用户的连接掐掉。

## 竞价实例：为什么是它，以及代价

抢占式实例便宜（实测香港 2C2G 约 0.02 元/小时），但**随时可能被回收**。
这个项目几乎全部复杂度都来自这一条。

| 措施 | 说明 |
| --- | --- |
| 保护期 | 用 `SpotWithPriceLimit` + `SpotDuration=1` 买 1 小时保护期；出价 = 市场价峰值 × 1.5 |
| 凭据持久化 | UUID + REALITY 密钥对存本地，重建时注入新实例，客户端不用重配 |
| 守护自愈 | 每 60 秒核对实例是否还在，连续 2 次查不到才判定回收并重建 |
| 调和重连 | 服务端地址一变，内核自动换配置重启（实测变化后 187ms 内前端可见） |

代价：**REALITY 私钥会落在你本机**（见 [SECURITY.md](../SECURITY.md)）。
不想接受就删掉 `~/.config/yi/identity.json`。

## 服务端协议选型

**Xray-core + VLESS + XTLS-Vision + REALITY**。

- REALITY 不需要域名与证书，竞价实例反复重建时少一整类故障源（ACME 续期）
- Vision 解决 TLS-in-TLS 的额外开销，2C2G 单核可跑数百 Mbps
- 客户端生态最广：v2rayNG 内置 Xray 核心；mihomo（Clash Verge Rev）原生支持
  `reality-opts` 与 `xtls-rprx-vision`

**两条硬约束**（都是实测撞出来的，见 [lessons.md](lessons.md)）：

1. **伪装目标的证书链不能太长**。REALITY 要把真实证书链抓下来替换签名，链一长就装不下。
   `www.microsoft.com` 的链有 8273 字节，实测必挂。所以伪装目标是一个候选列表，
   **逐个起服务 + 回环自测**，用第一个真正通的。
2. **服务端 Xray 版本必须 ≥ 客户端**。REALITY 会校验客户端版本，而客户端版本我们控制不了
   （v2rayNG / mihomo 自带内核），所以服务端默认装最新。

## 云资源流程

```
创建安全组（入：443 全开 / 22 仅你的 IP；出：全开）
   ↓
CreateInstance（竞价；VPC 下必须给 VSwitchId）
   ↓
若状态是 Stopped → StartInstance         ← 旧接口建 VPC 实例有时会建成停止态
   ↓
若没有公网 IP → AllocatePublicIpAddress
   ↓
轮询 /root/yi/server-info.json 直到 ready=true
   ↓
生成客户端配置（vless 链接 / mihomo / sing-box / v2rayNG 订阅）
```

任何一步失败都**逆序回滚**（删实例、删安全组），不留计费资源。
注意 `DeleteInstance` 在实例 `Initializing` 期间会失败，必须重试——
早期版本只试一次，把一台计费中的机器留在了账户里。

## 安全边界

- agent 只监听 `127.0.0.1`，不对外暴露任何端口
- 本地敏感文件全部 0600，目录 0700
- SSH 只允许密钥；安全组 22 只放行**当时的**公网 IP
- 手机订阅端点带随机 token、只读，机器换 IP 时内容自动更新
- **绝不把系统代理指向没人监听的端口**：内核起不来就自动回退直连

## 目录结构

```
src/yi/
  aliyun.py     阿里云 OpenAPI（自己实现 RPC 签名与重试）
  bootstrap.py  cloud-init 脚本渲染（装 Xray、开 BBR、生成密钥、回环自测）
  cli.py        命令编排
  configgen.py  客户端配置生成
  identity.py   凭据持久化
  proxy.py      mihomo 生命周期 + 系统代理 + 调和
  status.py     状态的唯一来源
  ssh.py        SSH（含主机密钥变更处理）
  state.py      本地配置与状态（TOML/JSON，0600）
app/
  agent.py      本地 HTTP + SSE 后端
  ui/           单文件界面（无构建步骤）
  macos/        Swift 外壳
  make-icon.py  代码画图标（确定性、可复现）
```
