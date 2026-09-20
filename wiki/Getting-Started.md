# 快速开始

目标：**从零到能上网**，大约 10 分钟，其中 3 分钟在等云 API。

## 0. 你需要什么

- 阿里云账号（已实名认证）
- 一个 **RAM 子账号**的 AccessKey（**别用主账号的**）
- macOS 11+，以及 [uv](https://docs.astral.sh/uv/)

### 建 RAM 子账号

控制台 → 访问控制 RAM → 用户 → 创建用户 → **勾选"使用永久 AccessKey 访问"**。

然后给它一条**最小权限**策略（只够跑这个项目，不是 AdministratorAccess）：

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ecs:DescribeRegions",
        "ecs:DescribeZones",
        "ecs:DescribeVpcs",
        "ecs:DescribeVSwitches",
        "ecs:DescribeSecurityGroups",
        "ecs:DescribeSecurityGroupAttribute",
        "ecs:AuthorizeSecurityGroup",
        "ecs:CreateSecurityGroup",
        "ecs:DescribeImages",
        "ecs:DescribeKeyPairs",
        "ecs:ImportKeyPair",
        "ecs:DescribeSpotPriceHistory",
        "ecs:DescribeInstances",
        "ecs:DescribeInstanceStatus",
        "ecs:DescribeInstanceMonitorData",
        "ecs:CreateInstance",
        "ecs:StartInstance",
        "ecs:AllocatePublicIpAddress",
        "ecs:DeleteInstance"
      ],
      "Resource": "*"
    }
  ]
}
```

> 如果你打算用 RDS/DNS 那些可选功能，再按需加。**默认路径只需要上面这些。**

## 1. 装好并写入凭据

```bash
git clone https://github.com/cinqs/yi && cd yi
make setup
./tools/set-credentials.sh
```

`set-credentials.sh` 是**交互式**的：不回显、不进 shell 历史，
写进 `~/.aliyun/config.json`（0600）。

## 2. 先体检，不花钱

```bash
./yi doctor --api
```

它按 `up` 的**真实调用顺序**逐项核对：凭据 → 权限 → 可用区 → 交换机 →
安全组（含规则）→ 镜像 → 密钥对 → 竞价价格。
**第一个红叉就是 `up` 会卡在哪**，不用等三分钟才知道。

```bash
./yi up --dry-run      # 看它打算买什么规格、出价多少
```

## 3. 开干

```bash
./yi up
```

大约 3 分钟：买机器 → cloud-init 装 Xray → 服务端**回环自检** → 生成客户端配置。

"就绪"的含义是**服务端自己当客户端连自己，拿到了 204**——
不是"脚本跑完了"这种弱信号。

## 4. 连接

```bash
./yi fetch-kernel      # 首次需要：取一份 mihomo 内核（约 15 MB）
./yi connect
./yi status            # 确认出口 IP 是服务器 IP
```

内核（mihomo）不随仓库分发——它有几十 MB、每个平台各一份，放 git 里会让项目
胖几十倍。所以第一次连接前先取一份。直连 GitHub 不通时，报错信息里会给出
可用的镜像地址写法，照抄 `--url` 即可。

或者用图形界面：

```bash
make app && open dist/Yi.app
```

## 5. 手机（Android）

先取客户端：

```bash
./yi android      # v2rayNG 的官方 APK，下到 ~/Downloads/yi-android/
```

GitHub Releases 国内常常打不开，这一步就是替你跨过去。传到手机装好后：

`up` 之后，本机会起一个只读订阅端点。手机和电脑连**同一个 Wi-Fi**，
在界面「连接」页找到**手机订阅**，扫码或复制地址，然后在 v2rayNG 里
`+` → 从剪贴板导入。以后机器换 IP，手机端刷新订阅即可，**不用重新配置**。

## 6. 用完记得省钱

```bash
./yi disconnect        # 断开代理（机器还在跑，继续计费）
./yi down              # 销毁机器，停止计费
```

> 忘了也没关系：所有资源都带 `ttl` 标签，`./yi status` 会显示已运行时长和累计流量。

## 下一步

- 想调参数（规格、端口、伪装目标）→ [配置参考](Configuration)
- 卡住了 → [故障排查](Troubleshooting)
- 想懂原理 → [架构](Architecture)
