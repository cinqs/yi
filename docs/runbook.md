# Runbook

## 创建失败

**`InstanceType.StockNotEnough` / `Zone.NotOnSale`**
香港区库存波动大。`config.toml` 里多放几个 `instance_types` 和 `zones`，CLI 会自己轮询。实在买不到就换时段再试。

**`InvalidSpotDuration`**
保护期（`spot_duration = 1`）必须配合 `SpotWithPriceLimit` + 正的 `spot_price_limit`。两种改法：

```bash
./yi up --price-limit 0.20     # 保留 1 小时保护期
# 或把 config.toml 的 spot_duration 改成 0（无保护期，可能刚买就被回收）
```

**`Forbidden.RAM`**
按 README 里的最小权限策略补齐 action。

**创建中途失败留下资源**

```bash
./yi down --orphans
```

按实例名/安全组名前缀清理，不需要状态文件。

## 连接失败

分三步定位：

1. 服务端活着吗 —— `./yi status`。显示 `Gone` 说明竞价实例被回收了。
2. 端口通吗 —— `./yi doctor`，看 `server-port` 这一行。
3. 服务端进程正常吗 —— `./yi ssh 'systemctl status xray --no-pager; tail -n 50 /var/log/yi-bootstrap.log'`。

**本地能 telnet 443 但客户端握手失败**
多半是本地到该 IP 的 443 被中间设备干扰。换网络、换 4G 试；仍然不行就把 `xray_port` 改成 8443 并同步改安全组（`yi up --force-recreate` 会自动按新端口开规则）。

**云安全组没问题但 22 连不上**
`allow_ssh_from` 记的是买机器那一刻的本机公网 IP。换网络后需要手动加规则，或在控制台放行新的 `/32`。

## 竞价被回收

```bash
./yi watch
```

检测到实例消失会自动重建，重建后在 `~/.config/yi/profiles/` 覆盖新配置，重新导一次客户端即可。

回收前阿里云会通过元数据服务提前 5 分钟通知：

```bash
./yi ssh 'curl -s http://100.100.100.200/latest/meta-data/instance/spot/termination-time'
```

## 流量费异常

```bash
./yi status            # 看已用出流量
```

阿里云按流量计费只统计**出方向**。如果账单远超预期：

- 检查是不是有人拿到了你的 UUID（UUID 即密码，泄露后可以被白嫖）
- 用 `./yi ssh 'journalctl -u xray -n 200 --no-pager'` 看有没有异常连接
- 怀疑泄露就 `./yi up --force-recreate` 换全套凭据

## 想彻底重来

```bash
./yi down --yes
rm -rf ~/.config/yi        # 连本地状态和密钥一起清掉（不可恢复）
./yi init && ./yi up
```

## 保密提醒

- `~/.config/yi/` 目录权限 700，里面的文件 600。别往 iCloud/网盘同步。
- `server-info.json` 里的 UUID + REALITY 公钥就是访问凭据，发给别人等于把代理送出去。
- AccessKey 只存在环境变量或 `~/.aliyun/config.json`，yi 自己的文件里没有。
