# 安全策略

## 报告漏洞

**请不要开公开 issue。** 用 GitHub 的
[私密漏洞报告](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
功能，或直接联系维护者。

请附上：影响范围、复现步骤、你看到的实际行为。48 小时内会收到回复。

## 这个项目的威胁模型

理解它有助于判断什么算漏洞：

| 资产 | 在哪 | 泄露的后果 |
|---|---|---|
| 阿里云 AccessKey | 你本机（`~/.aliyun/config.json` 或环境变量） | 别人能操作你的 ECS 资源 |
| SSH 私钥 | 你本机 `~/.config/yi/id_ed25519` | 别人能登录你的机器 |
| 代理凭据（UUID + REALITY 密钥对） | 你本机 `~/.config/yi/identity.json` 与客户端配置 | 别人能白嫖你的代理（UUID 即密码） |
| 服务端 REALITY 私钥 | 只存在于服务器 | 别人能冒充你的服务器 |

**默认设计**：

- RAM 子账号最小权限；主账号 AccessKey 不用
- SSH 只允许密钥登录，且安全组只放行**你当时的公网 IP**
- 本地所有敏感文件 0600，目录 0700
- agent 只监听 `127.0.0.1`，不对外暴露
- 手机订阅端点带随机 token，且只读

**已知的、可接受的取舍**（不算漏洞，但你要知道）：

- 凭据持久化意味着 **REALITY 私钥会落在你本机**。这是"重建后客户端不用重配"的代价。
  不想接受就把 `~/.config/yi/identity.json` 删掉（下次 `up` 会重新生成，客户端需重导）。
- 手机订阅走明文 HTTP（同一 Wi-Fi 内）。内容里含节点凭据，别在公共 Wi-Fi 上刷新。

## 支持范围

只维护最新版本。修复会发在 GitHub Releases，并在 `CHANGELOG.md` 里注明。
