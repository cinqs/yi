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
| `direct_domains` | 一长串国内域名 | 直连的域名后缀，见下 |
| `ruleset_enabled` | `true` | 是否使用社区规则集，见下 |
| `ruleset_mirrors` | 内置顺序 | 规则集的下载镜像，见下 |
| `want_connected` | 自动 | **期望状态**：`true` 时调和循环会保证"应该连着"。你点连接/断开时由界面写入 |
| `watch_enabled` | `true` | 后台守护：实例被回收后自动重建。**默认开**，这是这套方案值钱的地方 |
| `sub_token` | 自动生成 | 手机订阅端点的随机 token，首次需要时生成 |

### 分流规则

生成的 `mihomo.yaml` 里规则是**有顺序**的，顺序本身就是逻辑：

```
局域网/本机  →  直连        （192.168/10/172.16、链路本地、组播）
联网探测域名 →  直连        （captive.apple.com 等）
国内域名     →  直连        （.cn + direct_domains 里的那一串）
其余国内 IP  →  直连        （GEOIP,CN）
剩下的       →  走代理      （MATCH,PROXY）
```

**为什么局域网那几条必须自己列出来**：`GEOIP` 对私有地址返回空，只靠
`GEOIP,CN` 兜不住。少了这一层，访问 NAS、打印机、路由器管理页会被最后的
`MATCH,PROXY` 抓走，绕到香港再回来——结果是打不开，而且把内网地址交给了代理。

**为什么国内域名要单独列一份**：`GEOIP,CN` 也管用，但它得先把域名解析成真实
IP 才能判断；在 fake-ip 模式下每个新域名都要多一次解析，首包明显变慢。
域名规则是字符串比对，直接命中。所以两者配合：名单管常用的，GEOIP 兜剩下的。

```toml
direct_domains = ["qq.com", "bilibili.com", "example.com"]
```

想加就加（写后缀，不用带 `.` 前缀）；置空数组就是"只要 `.cn` 和 GEOIP 那层"。
改完跑 `./yi sub` 重新生成 profile。

> ⚠️ 直接改 `~/.config/yi/profiles/mihomo.yaml` 是没用的，下次 `yi sub`
> 会把它覆盖掉。规则要改就改配置里的 `direct_domains`，或者改代码里的
> `configgen.mihomo_rules()`。

### 社区规则集

上面那份手写清单覆盖不到长尾——`.cn` 之外还有一大批国内站点，而且每天都在变。
所以默认还会挂上社区维护的规则集（[Loyalsoldier/clash-rules](
https://github.com/Loyalsoldier/clash-rules)，12 个集合、30 多万条规则）：

| 规则集 | 作用 |
|---|---|
| `reject` | 广告 / 追踪，直接拒掉 |
| `private` `lancidr` | 局域网与本机 |
| `icloud` `apple` | 苹果服务直连（否则推送、iCloud 会绕一圈） |
| `google` `proxy` | 需要走代理的 |
| `direct` | 明确该直连的 |
| `cncidr` | 中国大陆 IP 段 |
| `gfw` `greatfire` | 明确被墙的 |
| `telegramcidr` | Telegram 的 IP 段（它不用域名） |

```bash
./yi rules              # 看状态：每个集合多少条、什么时候更新的
./yi rules --update     # 一键更新（自动挑可用镜像，必要时代理出去取）
```

规则集在 `up` 成功之后会自动取一次，之后每 24 小时由内核自己更新；
本地缓存超过 7 天没用上，后台守护会在连接可用时补一次。

```toml
ruleset_enabled = true          # 关掉就只剩内置基础规则（排查用）
ruleset_interval_hours = 24     # 内核自己的更新周期
ruleset_max_age_hours = 168     # 本地缓存多久算过期
ruleset_mirrors = []            # 留空 = 用实测过的内置顺序
```

**为什么默认走镜像**：这些规则集挂在 GitHub 上，`raw.githubusercontent.com`
实测直接超时。内置顺序是 cdn.jsdelivr.net → testingcf.jsdelivr.net →
ghproxy.net → gh-proxy.com → 上游本体，逐个试到成功为止。
国外或者连着代理时，上游本体反而最快，所以它排在最后而不是被删掉。

**一个必须知道的行为**：规则集**取不到时内核不会报错**——它照样启动，只是
那几条 `RULE-SET` 一条都匹配不上（实测确认）。好处是"规则集挂了"不会变成
"你断网"；坏处是失败完全静默。所以状态由 `./yi rules` 自己说，别靠猜。

### 我自己的规则

App 的**「规则」页**可以直接加自己的分流规则，也能看到社区规则集的状态
（每个集合多少条、什么时候更新的、缓存放在哪）。命令行等价写法是改配置：

```toml
custom_rules = [
  "DOMAIN-SUFFIX,corp.example,DIRECT",
  "DOMAIN-KEYWORD,doubleclick,REJECT",
  "IP-CIDR,203.0.113.0/24,PROXY",
]
```

写法就是 mihomo 原生的 `类型,值,动作`：

| 类型 | 说明 |
|---|---|
| `DOMAIN` | 精确匹配域名 |
| `DOMAIN-SUFFIX` | 匹配域名后缀 |
| `DOMAIN-KEYWORD` | 匹配域名关键字 |
| `IP-CIDR` | 匹配 IP 网段 |
| `GEOIP` | 匹配国家/地区，如 `GEOIP,CN,DIRECT` |

| 动作 | 说明 |
|---|---|
| `DIRECT` | 直连 |
| `PROXY` | 走代理 |
| `REJECT` | 拦截 |

几个细节：

- **优先级最高**，只排在"局域网直连"后面。你明确说了要拦掉/要走代理的域名，
  不会被任何社区列表盖掉。
- 加的是**你自己的规则**，不会往社区规则集里塞东西——那些是上游维护的，
  混进去下次更新就被覆盖，只会变成"我明明加了却不生效"。
- `IP-CIDR` 配 `DIRECT` 时，会自动补上 `no-resolve`：不补的话内核会为了匹配
  多做一次 DNS 解析，在 fake-ip 下解析到的是假地址，纯属白费。
- 域名一律小写归一化，重复的会被拒掉。
- 改完会立刻重新生成配置并让内核重读（用的仍是当前端口）。

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
