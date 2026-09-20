# 故障排查

先跑这两条，大多数问题能自己现形：

```bash
./yi doctor            # 本地与云端逐项体检
./yi status            # 机器 / 连接 / 出口 IP 一眼看全
```

日志在 `~/.config/yi/logs/`（**终端回滚抓不到的东西都在这里**）。

## 界面点"连接"没反应

1. 看看是不是**机器根本没买**——`./yi status`。没机器时连接按钮是灰的，这是对的。
2. 界面顶部如果有一条橙色横幅写着"后台未响应"，说明 App 调不到本地 agent。
   agent 是长驻进程，改过代码或它崩了都需要重启：

   ```bash
   pkill -f app/agent.py
   open dist/Yi.app
   ```

## 连上了但打不开网页

**先确认出口，别只看开关状态。**

```bash
./yi proxy-status                       # 内核在不在、系统代理开没开、端口是多少
curl -sS -x http://127.0.0.1:<端口> https://api.ip.sb/ip   # 走代理的出口
curl -sS https://api.ip.sb/ip                              # 直连的出口
```

两个出口**不一样**才说明代理真的生效了。如果代理出口也不是服务器 IP，
说明流量没进内核。

## 报"找不到代理内核 mihomo"

内核是单独下载的，不在仓库里：

```bash
./yi fetch-kernel
```

直连 GitHub 不通（国内常见）时，报错信息里会给出带镜像的完整命令，
形如 `./yi fetch-kernel --url https://ghproxy.net/https://github.com/...`。
已经有一份 mihomo 的话，也可以设 `YI_MIHOMO=/path/to/mihomo` 指过去。

## 报 `received real certificate`

这是客户端在和**起点不是你的服务器**握手，或者服务端的 REALITY 没协商成功。
两种常见原因：

1. **伪装目标的证书链太长**——服务端在启动自检时就会发现，看 `./yi selftest`。
   换一个 `reality_dests` 候选即可（`www.cloudflare.com` / `www.bing.com` 是实测可用的）。
2. **服务端 Xray 版本低于客户端**——REALITY 会把客户端版本写进握手校验。
   把 `xray_version` 设回 `latest` 重建。

## SSH 连不上 / 提示主机密钥变了

阿里云会把回收的 IP **重新分配给别人**，所以你连到的可能已经是另一台机器。
`yi` 会自动清掉 `known_hosts` 里的旧记录并重试一次。如果还不行，
多半是安全组没放行你**当前**的公网 IP（换网络了）：

```bash
./yi doctor --api          # 会指出是安全组还是别的问题
```

## 老是买不到机器 / 刚建好就被回收

- 调大 `spot_bid_multiplier`（出价高一点，绝对金额仍然很小）
- 给 `instance_types` 多加几个规格，给 `zones` 多加几个可用区
  （注意：填了 `vswitch_id` 就只剩多规格 fallback 了，因为交换机钉死可用区）

> 别忘了 `spot_duration`——保护期是**防秒回收**的关键。它要求用
> `SpotWithPriceLimit`；`SpotAsPriceGo` 拿不到保护期。

## 控制台里还有实例，`yi` 却说没有

阿里云不同接入点的数据会不一致（按 ID 查不到、裸列表查得到）。
`yi` 的处理是：先用裸列表 + 本地过滤兜底，并且**连续 2–3 次**都查不到才判定消失。

实在乱了就清场：

```bash
./yi down --orphans
```

它会列出并清理带 `ttl` 标签、但 `state.json` 里已经不认的实例。

## 更多

[docs/troubleshooting.md](https://github.com/cinqs/yi/blob/main/docs/troubleshooting.md)
里有更细的分支，[docs/lessons.md](https://github.com/cinqs/yi/blob/main/docs/lessons.md)
里有这些坑的完整根因。
