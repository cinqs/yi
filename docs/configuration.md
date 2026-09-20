# 配置参考

配置文件在 `~/.config/yi/config.toml`（目录 0700、文件 0600）。
**第一次运行 `./yi up` 或 `./yi doctor` 会自动生成**，只写入默认值；
之后 `yi` 只在需要时补键，不会覆盖你改过的值。

改完不需要重装任何东西，下一条命令就会读到新值。

```bash
./yi doctor --api     # 改完先体检，不花钱
./yi up --dry-run     # 看它打算买什么
```

## 云资源

| 键 | 默认 | 说明 |
|---|---|---|
| `region` | `cn-hongkong` | 地域。香港的优势是免备案、到大陆延迟低 |
| `zones` | `cn-hongkong-b/c/d` | 可用区 fallback 顺序。**填了 `vswitch_id` 就会被它钉死**，因为一个交换机只属于一个可用区 |
| `instance_types` | 三个 2C2G 档 | 规格 fallback 顺序。竞价下不同可用区的库存不一样，给多个更买得到 |
| `image_name_filters` | `ubuntu_22_04_x64` 等 | 镜像名前缀，按顺序找第一个命中的 |
| `disk_category` / `disk_size` | `cloud_essd` / 40 | 系统盘。20G 也够跑，40G 更省心 |
| `bandwidth_out` | `100` | 出方向峰值带宽（Mbps），按流量计费 |
| `bandwidth_in` | `0` | 入方向带宽。**`0` = 不传这个参数**。`CreateInstance` 不接受 `-1`，而且按流量计费本来就不对入方向收费 |
| `instance_name` | `yi-hk` | 实例显示名 |
| `key_pair_name` | `yi` | 阿里云密钥对名。不存在会自动导入本机公钥 |

## 竞价

| 键 | 默认 | 说明 |
|---|---|---|
| `spot_strategy` | `SpotWithPriceLimit` | **别改成 `SpotAsPriceGo`**：阿里云不允许它和保护期共存，改了就拿不到保护期，实例可能刚建好就被回收 |
| `spot_duration` | `1` | 保护期小时数。`>0` 时 `up` 会强制用限价策略 |
| `spot_price_limit` | `0.0` | `0` = 自动，取近期市场价峰值 × `spot_bid_multiplier` |
| `spot_bid_multiplier` | `1.5` | 自动出价的上浮倍数。出价只是"能不能拿到/保住"的门槛，留余量很便宜 |

```bash
./yi up --price-limit 0.05     # 手动指定出价上限
```

## 服务端

| 键 | 默认 | 说明 |
|---|---|---|
| `xray_port` | `443` | 服务端监听端口 |
| `xray_version` | `latest` | **别 pin 旧版本**：REALITY 要求服务端版本 ≥ 客户端版本，而客户端版本由 v2rayNG / mihomo 自带，你控制不了 |
| `vless_flow` | `xtls-rprx-vision` | **置空字符串可一键降级**，服务端与所有客户端同时去掉 `flow`。用来排查"是不是 Vision 不兼容" |
| `reality_dests` | 4 个候选 | 伪装目标候选列表。bootstrap **逐个实测**，用第一个真正通的（证书链过长的站点会失败） |
| `reality_fingerprint` | `chrome` | 客户端 TLS 指纹 |

### 关于 `reality_dests`

这一项是**候选列表**而不是"就用第一个"，因为 REALITY 要把伪装目标的真实证书链抓下来
重新签名；证书链长度超过服务端缓冲上限就装不完，客户端会看到真实证书然后拒绝连接。
实测 `www.microsoft.com` 的证书链 8273 字节直接失败。

想加候选，只要满足三条：从香港可达、支持 TLS 1.3 与 HTTP/2、
**并且在中国大陆不是被墙的站点**（被墙的站点做 SNI 会很扎眼）。

## SSH 与网络

| 键 | 默认 | 说明 |
|---|---|---|
| `allow_ssh_from` | `auto` | `auto` = 用"买机器那一刻"的本机公网 IP（`/32`）。换网络后要补规则，或写成固定 IP |
| `ssh_user` | `root` | |
| `vswitch_id` | 空 | **留空 = 自动发现默认 VPC 的交换机**。填上就复用它，且 `yi` 永远不会删它 |
| `security_group_id` | 空 | 留空 = 自动创建一个。填上就复用，按"缺啥补啥"补规则，且 `yi` 永远不会删它 |

```bash
./yi up --ssh-from 1.2.3.4      # 覆盖 allow_ssh_from
./yi up --zone cn-hongkong-c    # 覆盖可用区（与 vswitch 冲突会直接报错）
```

> ⚠️ 复用"默认安全组"意味着你补的规则对该 VPC 里**所有**实例生效。
> 想隔离就单独建一个安全组再把 ID 填进来。

## 本地代理

| 键 | 默认 | 说明 |
|---|---|---|
| `mixed_port` | 自动 | 内核的 HTTP/SOCKS 混合端口。默认自动避让（7897 是 Clash Verge 的常用口，容易撞） |
| `want_connected` | 自动 | **期望状态**：`true` 时调和循环会保证"应该连着"。你点连接/断开时由界面写入 |
| `watch_enabled` | `true` | 后台守护：实例被回收后自动重建。**默认开**，这是这套方案值钱的地方 |
| `sub_token` | 自动生成 | 手机订阅端点的随机 token，首次需要时生成 |

## 预算护栏

```toml
[budget]
max_hours = 720     # 累计运行小时上限
max_gb = 300        # 累计流量上限
```

超过阈值时 `yi status` 会告警。它**不会**替你关机——自动关机听起来聪明，
真出事的时候你只会觉得"网莫名断了"。护栏负责提醒，决定权留给你。

## 可选：用自己的域名

**不需要域名**，默认方案零域名成本。但如果你本来就有域名托管在阿里云 DNS，
填上这两项后，每次机器换 IP 都会自动更新一条 A 记录，手机订阅就有了稳定入口：

```toml
domain = "example.com"       # 主域名
subdomain = "yi"             # 记录名 → yi.example.com
```

这需要额外的 `alidns:*` 权限。不填就完全不会碰 DNS。

## 状态文件

同目录下还有几个**别手动改**的文件，出问题想重来就删掉（详见表）。

| 文件 | 内容 | 删掉的后果 |
|---|---|---|
| `state.json` | 当前实例 ID、IP、已创建的云资源 | `yi` 找不到自己的机器。先用 `./yi down --orphans` 清理，再删 |
| `identity.json` | VLESS UUID + REALITY 密钥对 | 下次 `up` 会重新生成，**客户端需要重新导入配置** |
| `id_ed25519` | SSH 私钥 | 旧机器登不上，得重建 |
| `server-info.json` | 最近一次成功部署的服务端信息 | 下次 `sub` 会重新拉 |
| `profiles/` | 生成的客户端配置 | 用 `./yi sub` 重新生成 |
| `logs/` | 日志 | 随便删，排查问题时最有用 |
