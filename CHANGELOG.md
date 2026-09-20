# 变更日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 变更
- **默认配置不再写死 vSwitch / 安全组 ID**（改为留空 = 自动发现或自动创建）。
  这两个键本来就是"如果你有现成资源就复用"，把某个人账号下的资源当成默认值
  既会让新用户直接失败，也没有道理。填了 ID 的老配置不受影响。

### 新增
- **分流规则补全**：原来只有 3 条（`.cn` / `GEOIP,CN` / 兜底），漏了局域网直连 ——
  访问 NAS、打印机、路由器管理页会被兜底规则抓走绕到香港。现在按
  「局域网 → 联网探测 → 国内域名 → GEOIP → 代理」分层，并补了 40 多个
  常见国内域名（`.cn` 之外的那些，如 qq.com / bilibili.com / taobao.com），
  可用 `direct_domains` 自行增删。同时加了 `fake-ip-filter`，避免 Bonjour /
  AirDrop / 打印机这类本地名字解析被 fake-ip 吃掉。
- **`yi fetch-kernel`**：代理内核（mihomo）不入库（几十 MB × 每个平台），
  改成用到时自己取。直连 GitHub 不通时 `--url` 可指定镜像地址。
  这是新用户此前唯一需要手工准备的东西
- 文档：[配置参考](docs/configuration.md)、[发版检查单](docs/RELEASE.md)、
  [开发纪事](docs/history.md)，以及一套 11 页的 Wiki（`wiki/` 目录，用
  `tools/sync-wiki.sh` 同步到 GitHub Wiki）
- 三个守门脚本，都已接进 `make check` 与 CI：
  `tools/check-no-secrets.sh`（凭据/真实资源 ID 脱敏）、
  `tools/check-doc-links.sh`（文档本地链接）、
  `tools/make-social-preview.sh`（社交预览图可复现生成）
- CI 新增脱敏检查、macOS 打包验证两个 job；App 打包失败不再等到发版才发现
- `make check` 在缺少 `uv` 时会自动退回仓库里的 `.venv`

### 计划中
- 抗墙自动处置：端口被封换端口、SNI 被封换伪装目标、IP 被封换 IP
- 手机订阅的异地稳定入口（对象存储）

## [0.1.0] - 2026-09-21

首个可用版本：从零到能上网，一条命令。

### 新增
- **一键创建**：阿里云香港抢占式实例 + 保护期，出价自动按市场价 × 1.5 计算；
  多规格 × 多可用区 fallback；创建失败自动回滚，不留计费资源
- **服务端**：Xray-core + VLESS + XTLS-Vision + REALITY；
  伪装目标按候选列表逐个**实测**选择（证书链过长的站点会直接排除）
- **回环自检**：在服务器上自己当客户端连自己，确认"装完了"等于"真的能代理"
- **声明式期望状态 + 调和循环**：内核崩溃、服务端换地址、系统代理被外部关闭，
  8 秒内自动纠正；升级时不会误断用户连接
- **竞价回收自愈**：守护进程发现实例消失后自动重建（实测约 2 分钟恢复）
- **凭据持久化**：UUID + REALITY 密钥对落在本地并在重建时注入，
  客户端不需要重新导入配置
- **macOS 应用**（不到 1 MB，Swift + WKWebView）：连接开关、机器管理、
  真实安装进度、实时流量曲线、手机订阅二维码
- **手机订阅**：本机只读端点 + 随机 token，机器换 IP 时内容自动更新，
  不需要域名与对象存储
- **诊断**：`yi doctor` 按真实调用顺序逐项体检；`yi selftest` 服务端回环自测

### 安全
- 不把系统代理指向未监听的端口（内核起不来时自动回退直连）
- SSH 主机密钥变更时清除旧记录并重试（阿里云会回收并重分配 IP）
- RAM 最小权限策略、SSH 仅密钥、本地敏感文件 0600

[Unreleased]: https://github.com/cinqsme/yi/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/cinqsme/yi/releases/tag/v0.1.0
