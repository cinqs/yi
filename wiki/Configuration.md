# 配置参考

配置文件：`~/.config/yi/config.toml`。第一次运行 `./yi up` 或 `./yi doctor`
会自动生成，之后 `yi` 只在需要时补键，**不覆盖你改过的值**。

**最常被改的六项**：

| 键 | 默认 | 什么时候改 |
|---|---|---|
| `instance_types` | 三个 2C2G 档 | 买不到机器时，往上加规格（多给几个更容易买到） |
| `vless_flow` | `xtls-rprx-vision` | 怀疑 Vision 不兼容时**置空字符串**一键降级 |
| `spot_bid_multiplier` | `1.5` | 老是被回收就调大（出价高一点，仍然很便宜） |
| `xray_port` | `443` | 端口被封时换一个（同时要改安全组） |
| `reality_dests` | 4 个候选 | 想换伪装目标；候选会**逐个实测**选第一个通的 |
| `direct_domains` | 一长串国内域名 | 哪些域名走直连。默认已含 40 多个常见国内站点，可自行增删 |
| `custom_rules` | 空 | 你自己的分流规则，`类型,值,动作`。App 的「规则」页可以直接增删 |
| `watch_enabled` | `true` | 不想让它自动重买就设 `false` |

改完先体检，不花钱：

```bash
./yi doctor --api
./yi up --dry-run
```

**完整参考（每一项都解释了"为什么是这个默认值"）**：
[docs/configuration.md](https://github.com/cinqs/yi/blob/main/docs/configuration.md)

## 命令行覆盖

有些项不用改文件，直接传参数：

```bash
./yi up --zone cn-hongkong-c
./yi up --instance-type ecs.e-c1m2.large
./yi up --price-limit 0.05
./yi up --ssh-from 1.2.3.4
./yi up --force-recreate
```

## 三条别踩的线

1. **`spot_strategy` 别改成 `SpotAsPriceGo`**。阿里云不允许它和保护期共存，
   改了就拿不到保护期，实例可能刚建好就被回收。
2. **`xray_version` 别 pin 旧版本**。REALITY 要求服务端版本 ≥ 客户端版本，
   而客户端版本由 v2rayNG / mihomo 自带，你控制不了。
3. **`vswitch_id` / `security_group_id` 填了就是借用你的资产**。
   `yi` 只会读和补规则，`down` 时**永远不会删它们**。
