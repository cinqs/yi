# 故障排查

先跑 `./yi doctor --api`（云端逐项体检）和 `./yi proxy-status`（连接与出口）。
日志在 `~/.config/yi/logs/`，`agent.log` 与 `yi-YYYYMMDD.log` 最有价值。

## 创建阶段

**`InstanceType.StockNotEnough` / `Zone.NotOnSale`**
香港区库存波动大。在 `~/.config/yi/config.toml` 的多放几个 `instance_types` 与 `zones`，
CLI 会自己轮换；实在没有就换个时段。

**`InvalidSpotDuration`**
保护期必须配 `SpotWithPriceLimit` + 正的出价上限。两种改法：
`./yi up --price-limit 0.20`，或把 `spot_duration` 改成 0（无保护期）。

**`Forbidden.RAM`**
按 [quickstart](quickstart.md) 的最小权限策略补齐 action。

**创建中途失败留下资源**

```bash
./yi down --orphans
```

按资源名前缀清理，不依赖本地状态文件。

## 安装阶段

**卡在"等待服务端就绪"**

```bash
./yi ssh 'tail -n 80 /var/log/yi-bootstrap.log'
```

常见原因：

- **SSH 主机密钥变更**：阿里云会把回收的 IP 再分配，旧密钥还在 `known_hosts`。
  `yi` 会自动清掉并重试；如果仍有问题，`ssh-keygen -R <IP> -f ~/.config/yi/known_hosts`。
- **cloud-init 还没跑完**：正常 1–3 分钟；超过 8 分钟界面会显示"安装失败"并建议重建。
- **GitHub 拉不动 Xray**：服务器在境外，一般没问题；`/var/log/yi-bootstrap.log` 会有明确报错。

## 连接阶段

**客户端报 `REALITY: received real certificate`**

服务端没认出你的客户端，把它当成普通访客转发给了伪装站点。两个已知原因：

1. **伪装目标证书链太长**。REALITY 要把真实证书链抓下来替换签名，链一长装不下。
   `www.microsoft.com` 实测必挂。工具会按 `reality_dests` 列表逐个自测，用第一个通的。
2. **服务端 Xray 版本低于客户端**。REALITY 会校验客户端版本，所以服务端默认装最新。

改 `config.toml` 后 `./yi up --force-recreate`。

**`ERR_PROXY_CONNECTION_FAILED`（浏览器报）**

系统代理被指向了一个没人监听的端口。正常情况下不会发生——`yi` 在设置前会检查端口是否在监听，
内核起不来时会自动回退直连。如果真遇到：

```bash
./yi proxy-status      # 看内核是否在跑、端口是否监听
./yi disconnect        # 先还原，恢复直连
```

**端口被占用**

7897 是 Clash Verge 的默认混合端口，容易撞。`yi` 会自动换端口并把实际端口记下来
（界面上显示的"本地端口"就是它）。

## 客户端侧

**Android 连不上**

- 先确认服务端活着：`./yi status` 与 `./yi selftest`
- 换网络试（4G ↔ Wi-Fi）：有些运营商对特定 IP 的 443 有干扰
- v2rayNG 的日志在设置里，能看到具体握手失败原因

**macOS 系统代理开了但浏览器不走代理**

Clash Verge Rev 的「订阅」页那一步要选 **Local** 类型并**选择文件**（不能粘贴内容）。
导入后必须**点一下卡片选中**，再去首页开系统代理。

## 计费

```bash
./yi status     # 已运行时长、累计流量
```

- 阿里云按流量计费**只统计出方向**
- 流量异常高先怀疑"UUID 被人白嫖"：`./yi up --force-recreate` 换一套凭据
- 彻底不用了：`./yi down`，然后确认控制台里没有残留实例与云盘
