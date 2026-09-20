# 快速开始

目标：**5 分钟内**从零到能上网。

## 0. 前提

| 需要 | 说明 |
| --- | --- |
| 阿里云账号 | 已实名认证，且能购买**香港**区域的 ECS |
| RAM 子账号 | 不要用主账号 AccessKey。权限见下 |
| macOS 11+ | 或 Linux（命令行部分可用） |
| uv | `brew install uv` |

### RAM 最小权限

在阿里云控制台建一个 RAM 用户（勾选 OpenAPI 调用访问），授权这段自定义策略：

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ecs:DescribeRegions", "ecs:DescribeZones", "ecs:DescribeVpcs",
        "ecs:DescribeVSwitches", "ecs:DescribeImages", "ecs:DescribeKeyPairs",
        "ecs:ImportKeyPair", "ecs:DescribeSecurityGroups",
        "ecs:DescribeSecurityGroupAttribute", "ecs:CreateSecurityGroup",
        "ecs:AuthorizeSecurityGroup", "ecs:AuthorizeSecurityGroupEgress",
        "ecs:DeleteSecurityGroup", "ecs:CreateInstance",
        "ecs:DescribeInstances", "ecs:DescribeInstanceStatus",
        "ecs:DescribeInstanceMonitorData", "ecs:DescribeSpotPriceHistory",
        "ecs:DeleteInstance"
      ],
      "Resource": "*"
    }
  ]
}
```

## 1. 装好并写入凭据

```bash
git clone https://github.com/cinqs/yi && cd yi
make setup                   # uv + Python 3.12 + 虚拟环境
./tools/set-credentials.sh   # 交互式输入 AccessKey（不回显、不进 shell 历史）
```

AccessKey 只会写到 `~/.aliyun/config.json`（权限 600），**不会进入本项目的任何文件**。

## 2. 先体检（这一步不花钱）

```bash
./yi doctor --api
```

它按 `up` 的真实调用顺序逐项核对：

```
[ok] credentials                 LTAI****PsF1
[ok] ecs:DescribeRegions         共 32 个区域
[ok] ecs:DescribeZones           cn-hongkong 有 3 个可用区
[ok] ecs:DescribeVSwitches       vsw-xxx 在 cn-hongkong-b / vpc-xxx
[ok] sg-vpc-match                与交换机同 VPC
[ok] ecs:DescribeImages          ubuntu_22_04_x64_20G_alibase_xxx.vhd
[ok] ecs:DescribeSpotPriceHistory 市场价峰值 0.02 元/小时 -> 出价 0.030
```

**第一个红叉就是 `up` 会卡住的地方。**

## 3. 买机器

```bash
./yi up --dry-run    # 看它打算买什么
./yi up              # 真买，约 3 分钟
```

`up` 会依次做：创建安全组 → 创建竞价实例 → 等待运行（必要时显式启动）→
分配公网 IP → 等 cloud-init 装好 Xray → **回环自测**（自己当客户端连自己）→
生成客户端配置。

看到 `服务端自检: 通过` 才算真的成功。

## 4. 连接

```bash
./yi fetch-kernel    # 首次需要：取一份 mihomo 内核（约 15 MB，不入库）
./yi connect
```

它会：起 mihomo 内核 → 设置系统代理（**免密码**）→ 验证出口 IP 等于服务端 IP。

> **为什么内核要单独下载**：mihomo 有几十 MB、每个平台架构各一份，
> 放仓库里会让项目胖几十倍，还得跟着上游升级。所以改成用到时才取。
> 直连 GitHub 不通时用 `--url` 指一个镜像地址（报错信息里会给出完整示例）。

也可以用图形界面：

```bash
make app && open dist/Yi.app
```

## 5. 客户端

配置在 `~/.config/yi/profiles/`：

**Android（v2rayNG）**

```bash
./yi android                                    # 取官方 APK（GitHub 国内常常打不开）
pbcopy < ~/.config/yi/profiles/v2rayng-subscription.txt
```

把 `~/Downloads/yi-android/` 里的 APK 传到手机装上，然后
v2rayNG → 右下 `+` → **从剪贴板导入** → 回主页点连接 → 允许 VPN 授权。

> **为什么是 v2rayNG 而不是我们自己的 App**：Android 端要做的是
> `VpnService` + 内核集成 + 证书管理一整套，自己重写只会更差，也违背本项目
> "不自己造协议、用成熟开源实现"的原则。所以 Android 端我们只负责
> **把客户端交到你手上**（取 APK + 生成订阅），不重复造一个。

**macOS（Clash Verge Rev）**

```bash
pbcopy < ~/.config/yi/profiles/mihomo.yaml
```

Clash Verge Rev → **订阅** 页 → 右上角蓝色 **新建** → 类型选 **Local** →
**选择文件** → 选 `~/.config/yi/profiles/mihomo.yaml` → 保存 → 点一下卡片选中 →
**首页** 打开**系统代理**。

**手机订阅（不用传文件）**

界面「连接」页底部有订阅地址与二维码。手机和电脑在同一 Wi-Fi 下，
v2rayNG → 订阅 → 添加订阅 → 粘贴地址。以后机器换 IP 时订阅内容自动更新。

## 6. 收工

```bash
./yi status        # 看状态、运行时长、流量
./yi watch         # 常驻守护：机器被回收自动重买
./yi disconnect    # 断开并还原系统代理
./yi down          # 销毁，停止计费
```

> 竞价实例按量计费，不用了记得 `down`。
