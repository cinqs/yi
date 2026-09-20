# yi — 阿里云香港竞价实例 + VLESS 代理 规格说明

状态：草案 v0.1 · 待确认项见 §12

## 1. 一句话定义

一条命令在阿里云香港购买一台竞价（抢占式）ECS，自动安装 Xray-core 的 VLESS 服务端，并把可直接导入的客户端配置（Android + macOS）生成本地文件与二维码。

## 2. 目标

- `yi up` 从零到"可连接"≤ 5 分钟，全程无需登录控制台点选。
- 服务端零域名依赖：不需要备案、不需要域名、不需要 TLS 证书。
- 本地产出两类客户端配置：Android（v2rayNG / sing-box for Android）与 macOS（Clash Verge Rev / sing-box）。
- `yi down` 一条命令销毁全部计费资源，无残留。
- 竞价实例被阿里云回收后，能在分钟级重建并重新下发配置（自愈）。

## 3. 非目标

- 不做多用户计费、不做商业转售、不做面板（3x-ui 等）——除非后续单独提出。
- 不做 iOS / Windows / 路由器固件客户端。
- 不做对抗企业级深度包检测的专有混淆（REALITY 已足够，不堆叠额外伪装层）。
- 不做大陆境内落地的"合法跨境专线"合规方案。

## 4. 核心用户流程

1. 用户在本机安装 CLI，配置阿里云 RAM 子账号 AccessKey。
2. `yi up` → 创建安全组 + 抢占式 ECS → cloud-init 安装并启动 Xray → CLI 通过 SSH 回读服务端参数。
3. CLI 输出：`vless://` 链接、订阅文件、二维码（终端直接渲染）、macOS profile 文件路径。
4. 手机扫码导入 → 开启 VPN → 验证落地 IP 为香港。
5. mac 导入 profile → 开启系统代理或 TUN → 验证。
6. 用完 `yi down`，或留着长跑由 `yi watch` 守护。

## 5. 架构

```
┌─────────────── 本机 (macOS) ───────────────┐
│  yi (Python CLI)                       │
│   ├── aliyun 模块  ──OpenAPI──┐            │
│   ├── ssh 模块                │            │
│   ├── configgen 模块          │            │
│   └── state (本地 JSON)       │            │
│                                │            │
│  客户端: Clash Verge Rev / sing-box (TUN)   │
└────────────────────────────────┼───────────┘
                                 │
                  ┌──────────────▼──────────────┐
                  │ 阿里云 cn-hongkong           │
                  │  ├─ 安全组 (443, 22/32)      │
                  │  └─ ECS 抢占式实例            │
                  │      └─ Xray-core :443        │
                  │         VLESS/Vision/REALITY  │
                  └──────────────────────────────┘
                                 │
                            Android (v2rayNG)
```

组件职责：

| 组件 | 职责 | 明确不做 |
|---|---|---|
| `yi` | 资源生命周期、回读服务端参数、生成配置、状态管理 | 不做实时流量转发 |
| cloud-init + `bootstrap.sh` | 在实例内幂等安装并配置 Xray、写出 `server-info.json` | 不接触 AccessKey |
| 客户端 App | 建立隧道、按规则分流 | 不参与资源管理 |

## 6. 关键技术决策

### 6.1 服务端协议：Xray-core + VLESS + XTLS-Vision + REALITY

理由：

- REALITY 不需要域名与证书，省掉 ACME 续期这一整类故障源；抢占了实例被回收后重建时 `.acme.sh` 状态也不用迁移。
- Vision 流控在 VLESS 上解决 TLS-in-TLS 的额外开销，单核可跑数百 Mbps，2C2G 规格完全够用。
- 端口固定 443/TCP，`dest` 指向一个从香港可达且支持 TLS1.3 的站点（默认 `www.microsoft.com:443`，可配置）。

被否决的替代方案：

- **TLS + WebSocket + CDN**：需要域名、证书、CDN 配置，且香港落地走 CDN 反而增加一跳与故障面；作为可选扩展保留。
- **sing-box 服务端**：本身没问题，但客户端侧兼容性以 Xray 生态最广（v2rayNG 内置 Xray 核心），选它作为默认更省心。

### 6.2 云资源：抢占式实例 + 按流量计费

- 实例计费：`SpotAsPriceGo`（跟随市场价，免维护出价）作为默认，可选 `SpotWithPriceLimit`。
- 网络计费：`PayByTraffic`（按使用流量），峰值带宽 100 Mbps。代理场景流量费通常高于实例费，按流量更符合"用时才付费"。
- 保护期：创建时设置 1 小时保护期，避免刚买就被回收导致体验崩坏。
- 多规格 / 多可用区 fallback 列表（香港区库存经常性紧张），逐个尝试直到创建成功。

### 6.3 编排层：Python CLI 直连 OpenAPI

选 Python 的原因：官方 SDK 成熟、迭代最快；且**必须回读实例内生成的密钥**（UUID、REALITY keypair），Terraform 在这一步会很别扭。

- SDK：`alibabacloud_ecs20140526` + `alibabacloud_tea_openapi`
- SSH：`paramiko`
- 模板渲染：`jinja2`
- 二维码：`qrcode`（终端用 ASCII 输出）

### 6.4 客户端

| 平台 | 首选 | 备选 | 代理模式 |
|---|---|---|---|
| Android | v2rayNG（内置 Xray） | sing-box for Android | VpnService 全流量 TUN，支持分应用 |
| macOS | Clash Verge Rev（mihomo 内核） | sing-box（TUN） | 系统代理 或 TUN 全局 |

关于 macOS 的"开启 VPN"：

- **系统代理模式**：无需 root，成本低，但只有尊重系统代理的 App 生效；终端工具需要 `export https_proxy=...`。默认走这条。
- **TUN 模式**：真正全局接管，需要安装网络扩展/授权，首次要输管理员密码。作为可选路径，README 里给出逐步授权说明。

### 6.5 配置分发

- **MVP（默认）**：配置完全本地生成，不落地到公网。Android 扫码，mac 用本地 profile 文件。
- **v2（可选）**：在实例上跑一个只读订阅端点（HTTPS + 随机路径 token），配合自备域名与云解析 API 自动更新 A 记录，回收重建后客户端订阅地址不变。

## 7. 详细规格

### 7.1 CLI 接口

| 命令 | 说明 |
|---|---|
| `yi init` | 交互式写入配置（region / 规格 / 预算上限 / 域名，可选），校验 AccessKey 权限 |
| `yi up [--instance-type ...] [--price-limit N]` | 创建并完成端到端就绪，输出配置与二维码 |
| `yi status` | 列出实例状态、公网 IP、流量用量、健康检查结果 |
| `yi sub` | 重新生成并输出客户端配置与二维码（不重建实例） |
| `yi ssh` | 用已登记的密钥直连实例 |
| `yi watch` | 常驻守护：检测回收通知/实例消失，自动重建并重新下发 |
| `yi down` | 销毁实例 + 安全组 + 关联资源，清理状态 |
| `yi doctor` | 本地连通性诊断：直连 vs 代理 vs DNS 泄漏 |

约定：

- 退出码：0 成功；1 参数/配置错误；2 云 API 失败；3 超时；4 就绪校验失败。
- 所有命令支持 `--json` 供脚本消费；日志走 stderr，结构化产物走 stdout。
- 幂等：重复 `up` 若已有健康实例则直接返回现有配置，除非 `--force-recreate`。

### 7.2 云资源参数

| 项 | 值 |
|---|---|
| Region | `cn-hongkong` |
| 可用区 fallback | `cn-hongkong-b` → `-c` → `-d` |
| 规格 fallback | `ecs.e-c1m1.large`(2C2G) → `ecs.t6-c1m1.large` → `ecs.u1-c1m1.large` |
| 镜像 | Ubuntu 22.04 LTS 64bit（按 Alibaba Cloud 公共镜像 ID 动态查询，不硬编码） |
| 系统盘 | ESSD PL0，40 GiB，`DeleteWithInstance=true` |
| 带宽 | `InternetChargeType=PayByTraffic`，`InternetMaxBandwidthOut=100` |
| 公网 IP | 入方向带宽不传参（用阿里云默认值）+ 自动分配公网 IPv4。注意 `CreateInstance` 不接受 `InternetMaxBandwidthIn=-1`，那是 `RunInstances` 的写法 |
| 计费 | `SpotStrategy=SpotAsPriceGo`，保护期 1 小时 |
| 登录 | 仅 SSH 密钥（`key_pair`），禁用密码登录 |
| 安全组入方向 | 443/TCP 全开；22/TCP 仅当前出口 IP `/32`；8443/TCP 仅订阅 token 需要时开放 |
| 安全组出方向 | 全开 |
| 标签 | `project=yi`, `owner=<user>`, `ttl=<hours>` |

清理保证：创建过程失败时按"逆序回滚"，避免留下孤儿安全组与云盘（见 §10 R3）。

### 7.3 服务端 bootstrap（cloud-init → `/opt/yi/bootstrap.sh`）

幂等步骤：

1. 基础包：`curl`、`jq`、`unzip`。
2. 安装 Xray-core 官方脚本，**版本 pin 到具体 tag**（避免上游破坏性变更）。
3. 内核调优：`fq` + `bbr`、`net.ipv4.ip_forward=1`、提高 `somaxconn` 与 `file-max`。
4. 生成密钥：`uuidgen` 生成 UUID；`xray x25519` 生成 REALITY keypair；随机 `shortId`（16 位 hex）。
5. 渲染 `/usr/local/etc/xray/config.json`（模板见下），`systemctl enable --now xray`。
6. 主机层防火墙仅放行 443（云安全组为主，主机防火墙兜底）。
7. 写出 `/root/yi/server-info.json`（权限 600），内容含 protocol 参数与创建时间，**不含 AccessKey、不含服务器私钥**。
8. 自检：本地 `curl -x socks5://127.0.0.1:...` 不可行（服务端无出站代理），改为校验进程存活 + 端口监听 + `xray run -test -config` 通过，并写 `"ready": true`。

服务端 config 要点：

```json
{
  "inbounds": [{
    "listen": "0.0.0.0",
    "port": 443,
    "protocol": "vless",
    "settings": {
      "clients": [{ "id": "<UUID>", "flow": "xtls-rprx-vision" }],
      "decryption": "none"
    },
    "streamSettings": {
      "network": "tcp",
      "security": "reality",
      "realitySettings": {
        "dest": "www.microsoft.com:443",
        "serverNames": ["www.microsoft.com"],
        "privateKey": "<PRIV>",
        "shortIds": ["<SID>"]
      }
    },
    "sniffing": { "enabled": true, "destOverride": ["http", "tls", "quic"] }
  }],
  "outbounds": [
    { "protocol": "freedom", "tag": "direct" },
    { "protocol": "blackhole", "tag": "block" }
  ],
  "routing": {
    "domainStrategy": "IPIfNonMatch",
    "rules": [{ "type": "field", "ip": ["geoip:private"], "outboundTag": "direct" }]
  }
}
```

### 7.4 `server-info.json` 数据模型

```json
{
  "ready": true,
  "created_at": "2026-09-19T14:03:22Z",
  "xray_version": "1.8.x",
  "protocol": "vless",
  "address": "<public-ip>",
  "port": 443,
  "uuid": "<uuid>",
  "flow": "xtls-rprx-vision",
  "security": "reality",
  "sni": "www.microsoft.com",
  "fingerprint": "chrome",
  "public_key": "<pbk>",
  "short_id": "<sid>",
  "network": "tcp"
}
```

### 7.5 本地状态文件

位置 `~/.config/yi/`（权限 700）：

- `config.toml`：区域、规格 fallback、预算上限、域名（可选）等非敏感配置。
- `state.json`：`instance_id`、`security_group_id`、`region`、`public_ip`、`created_at`、`expires_at`、`ssh_key_path`。
- `profiles/`：生成的客户端配置文件与最新的 `vless://` 链接。

AccessKey 不写入本项目任何文件；从 `~/.aliyun/config.json` 的指定 profile 或环境变量读取。

### 7.6 客户端配置产物

每次 `up` / `sub` 产出：

1. `vless://<uuid>@<ip>:443?encryption=none&flow=xtls-rprx-vision&security=reality&sni=www.microsoft.com&fp=chrome&pbk=<pbk>&sid=<sid>&type=tcp&headerType=none#HK-Spot`
2. 终端二维码（Android 扫码即导入）。
3. `profiles/mihomo.yaml` — macOS Clash Verge Rev：`proxies` 含 vless+reality 节点、`proxy-groups` 提供 `PROXY`/`DIRECT`/`自动选择`、`rules` 采用 geoip/geosite 精简规则集。
4. `profiles/singbox.json` — sing-box 通用（含 macOS TUN 出站与 Android 备用导入）。
5. `profiles/v2rayng.json` — v2rayNG 批量导入格式。
6. `profiles/README.txt` — 两个平台的逐步操作说明（导入、授权、开启、验证、关闭）。

### 7.7 抢占回收与自愈

阿里云抢占式实例回收会**提前 5 分钟**通过元数据服务与事件通知告知（无保护期时几乎立即）。

- 实例内：`yi-heartbeat.timer` 每 60s 读 `http://100.100.100.200/latest/meta-data/instance/spot/termination-time`，写本地日志并向 CLI 注册的通知通道上报。
- CLI `yi watch`：轮询 `DescribeInstances`，检测到 `Stopped`/`Released` 或收到回收事件即触发重建，重建后重新生成配置并提示"需重新导入客户端配置"（若启用 v2 订阅则客户端自动更新）。
- 用户侧兜底：`yi doctor` 一键判断"是服务端没了还是本地网络问题"。

### 7.8 安全

- RAM 子账号最小权限策略：仅 `ecs:`（实例/安全组/密钥对/镜像查询）、`vpc:Describe*`，若启用 DNS 再加 `alidns:` 相关；禁止使用主账号 AccessKey。
- AccessKey 存放位置优先级：环境变量 > `~/.aliyun/config.json` profile > 交互式输入（不落盘）。
- SSH 私钥由 CLI 生成（`ed25519`），存 `~/.config/yi/id_ed25519`（600）。
- 安全组 22 端口默认只放行当前公网 IP；`yi init` 检测到 IP 变化时提示刷新规则。
- 客户端配置含 UUID，等同凭据：产物目录权限 700，二维码仅本地渲染，不上传任何第三方。
- 关闭 SSH 密码登录、`PermitRootLogin prohibit-password`；可选安装 `fail2ban`。

### 7.9 可观测性与成本闸门

- 实例内：`xray` 日志 `loglevel=warning` 落盘，日志轮转；`yi status` 拉取最近 20 条错误。
- 云侧：云监控对"公网出流量"设置日/月阈值告警（钉钉或邮件），超阈值即提示。
- 成本闸门：`config.toml` 中的 `budget.max_hours`（默认 720h）与 `budget.max_gb`（默认 300GB）；触及即 `yi watch` 停止重建并告警。
- 所有资源带 `ttl` 标签，便于对账与误留检查。

## 8. 仓库结构

```
.
├── SPEC.md
├── README.md                  # 5 分钟上手
├── pyproject.toml             # 依赖与 yi 入口
├── src/yi/
│   ├── cli.py                 # 命令解析与编排
│   ├── aliyun_ecs.py          # 实例/安全组/镜像查询
│   ├── aliyun_dns.py          # 可选：A 记录自动更新
│   ├── ssh.py                 # 等待就绪 + 回读 server-info
│   ├── configgen.py           # 渲染客户端产物 + 二维码
│   ├── state.py               # 本地状态与权限
│   ├── watcher.py             # 回收自愈
│   └── templates/
│       ├── xray.json.j2
│       ├── mihomo.yaml.j2
│       ├── singbox.json.j2
│       └── bootstrap.sh.j2
├── tests/
│   ├── test_configgen.py
│   ├── test_state.py
│   └── test_aliyun_mock.py
└── docs/
    ├── runbook.md             # 常见故障处置
    └── manual-first-run.md    # 先手动跑通的验证清单
```

## 9. 里程碑

| 阶段 | 内容 | 产出 | 估时 |
|---|---|---|---|
| M0 | 账号与前提确认（见 §12），RAM 子账号与密钥就绪 | 无代码 | 0.5d |
| M1 | **手动跑通**：控制台买竞价机 + 手动装 Xray + 两端连通 | `docs/manual-first-run.md` 实测记录 | 0.5–1d |
| M2 | CLI 核心：`init/up/status/down/ssh` | 可重复创建销毁 | 2–3d |
| M3 | 配置生成：mihomo / sing-box / 二维码 / README | `yi sub` 可用 | 1d |
| M4 | 自愈与告警：`watch` + 回收事件 + 预算闸门 | 回收后自动恢复 | 1d |
| M5 | 可选：订阅端点 + 域名自动解析 | 客户端免重新导入 | 1d |

M1 是关键前置：它把"协议能不能在你这条线路上跑通"和"工具链能不能自动化"两件事解耦，避免在自动化里排协议问题。

## 10. 验收标准

| 编号 | 场景 | 通过条件 |
|---|---|---|
| A1 | 干净环境 `yi up` | ≤5 分钟完成，退出码 0，产出 3 类配置文件 + 二维码 |
| A2 | Android 连通 | v2rayNG 导入即连，`ipinfo.io` 显示 HK，1080p 视频无卡顿，丢包 <1% |
| A3 | macOS 连通 | Clash Verge Rev 导入，系统代理与 TUN 两种模式均能访问外网，DNS 无泄漏 |
| A4 | 幂等 | 再次 `up` 复用现有实例，不产生第二台机器 |
| A5 | 销毁彻底 | `down` 后控制台无实例、无安全组、无云盘，无继续计费项 |
| A6 | 故障回滚 | 创建中途失败（模拟 API 报错/库存不足）后无孤儿资源 |
| A7 | 自愈 | 手动释放实例后 `watch` 在 5 分钟内重建并给出新配置 |
| A8 | 稳定性 | 连续 24h 后台挂机，服务端 CPU <30%、内存 <50%、无进程崩溃 |
| A9 | 权限收敛 | 使用只读 AccessKey 时 `up` 明确报错并提示缺少的 action |

## 11. 成本估算（量级参考，落单前以控制台计价为准）

| 项 | 估算 |
|---|---|
| 竞价实例 2C2G 香港 | 约 ¥0.03–0.15 / 小时（随市场波动） |
| 公网出流量 | 约 ¥0.5–0.8 / GB |
| 系统盘 40G ESSD | 约 ¥0.5–1 / 天量级 |

典型个人使用（每天 2 小时 + 20GB 流量/月）：实例费约 ¥10–20，流量费约 ¥10–20，合计**每月 ¥20–50 量级**。真正跑长视频的话流量费会成为主要成本，这也是 `budget.max_gb` 闸门存在的原因。

## 12. 待确认问题（含本规格采用的默认假设）

| # | 问题 | 默认假设 |
|---|---|---|
| Q1 | 阿里云账号是中国站（aliyun.com）还是国际站？ | 中国站，已完成实名认证 |
| Q2 | 是否已有可用域名？ | 无，MVP 走本地生成配置，不依赖域名 |
| Q3 | 月度预算上限与预期流量？ | 实例 ≤¥100/月，流量 ≤300GB/月 |
| Q4 | 需要几台设备同时在线？ | 单账号 UUID，2–3 台设备共享 |
| Q5 | macOS 是否接受 TUN 全局模式（需管理员授权）？ | 默认系统代理，TUN 作为可选 |
| Q6 | 是否允许本机保存 AccessKey？ | 允许，但仅限 RAM 子账号最小权限 |
| Q7 | 是否接受竞价实例随时被回收？ | 接受，并要求 5 分钟内自愈 |
| Q8 | 是否需要图形化管理面板？ | 不需要 |

## 13. 风险与缓解

| # | 风险 | 缓解 |
|---|---|---|
| R1 | 香港区竞价库存不足，创建失败 | 多规格 × 多可用区 fallback + 退避重试，失败给明确提示 |
| R2 | 实例被回收导致断网 | 1 小时保护期 + `watch` 自愈 + 客户端配置一键重导入 |
| R3 | 创建中途失败留下计费资源 | 逆序回滚 + `ttl` 标签 + `yi doctor --orphans` 扫描 |
| R4 | 流量费失控 | 按流量计费 + 云监控告警 + `budget.max_gb` 硬闸门 |
| R5 | 泄露 AccessKey 或 UUID | RAM 最小权限、凭据不落库、产物目录 700、文档明确"UUID 即密码" |
| R6 | 阿里云安全策略/风控对该用途有异议 | 控制单账号规模、只做个人用途、关注控制台通知 |
| R7 | 上游 Xray 安装脚本或协议参数变更 | 版本 pin + `xray run -test` 就绪校验 + 一条命令回滚重建 |
| R8 | 中国大陆跨境 VPN 服务受监管 | 仅限个人自有网络与合规用途，不对外提供服务、不做转售 |

## 14. 合规提示

中国大陆对未经许可对外提供跨境 VPN/代理服务有明确监管要求，且阿里云服务条款对该用途亦有约束。本规格的定位是**个人自有服务器的网络工具**，规模与用途均不涉及对外经营；实际部署前请自行确认所在地与账号所属地的适用规定。
