# 发版检查单

发版是这个项目里**唯一会把东西交给别人**的动作，所以流程写死在下面，
不靠记忆。目标：任何人（包括半年后的你）照着做都不会漏步。

## 一、发布前

### 1. 代码

```bash
make check              # 脱敏 + 界面语法 + ruff + 全部单测
./yi doctor --api       # 真机体检：凭据有没有过期、权限有没有被改
```

### 2. 真机走一遍

自动化测试覆盖不到 GUI 和云 API。发布前**至少**：

```bash
./yi up                 # 真买一台，确认能买到、能装好、自检通过
./yi connect            # 确认能连上并且真的走了代理
./yi status             # 确认出口 IP 是服务器 IP
./yi down               # 确认能干净销毁
```

界面改动还要用 playwright 打开 `dist/Yi.app` 里的页面截图确认
（见 `AGENTS.md` 的「怎么验证」）。

### 3. 版本号

三处必须一致，**漏一处就会发出一个自称版本号不对的包**：

- `pyproject.toml` 的 `version`
- `src/yi/__init__.py` 的 `__version__`
- `CHANGELOG.md` 的标题

```bash
grep -rn '0\.1\.0' pyproject.toml src/yi/__init__.py CHANGELOG.md
```

### 4. CHANGELOG

把 `## [Unreleased]` 里的内容挪到一个新的 `## [x.y.z] - YYYY-MM-DD` 下，
并补上底部的 compare 链接。**面向用户写**：他关心的是"我现在能做什么了"，
不是"我重构了哪个模块"。

## 二、打 tag 发版

```bash
git add -A && git commit -m "release: v0.2.0"
git tag -a v0.2.0 -m "v0.2.0"
git push origin main --follow-tags
```

push tag 会触发 [`.github/workflows/release.yml`](../.github/workflows/release.yml)：
在 macOS runner 上打包 `Yi.app` → 压缩成 `Yi-v0.2.0.zip` → 生成 SHA-256 →
自动起草 release notes 并发布。

### tag 推错了怎么办

```bash
git tag -d v0.2.0                        # 删本地
git push origin :refs/tags/v0.2.0        # 删远端
```

如果 release 已经发出去了，**不要复用这个版本号**（下游可能已经下载过），
直接发一个 `v0.2.1`。

## 三、发布后

- [ ] Release 页面里 `.zip` 和 `.sha256` 都在，且 download 能下
- [ ] 下载 zip 试用一次：解压 → 双击 → 能开窗、能连
  > 未签名的 App 在别人的机器上会被 Gatekeeper 拦下。首次打开需要
  > 右键 → 打开，或 `xattr -dr com.apple.quarantine Yi.app`。
  > 这是**已知限制**，写在 Release notes 里，别让用户自己猜。
- [ ] GitHub Pages 站点还能打开（`docs/index.html` 有改动时）
- [ ] README 的徽章是绿的（CI 通过了）
- [ ] 需要的话，把这次的踩坑补进 `docs/lessons.md`

## 四、第一次开源（一次性，不是每次发版）

### 1. 建仓库并推上去

```bash
# 仓库名建议就叫 yi；不要勾选 "Add README"（本地已经有了）
git remote add origin git@github.com:cinqs/yi.git
git push -u origin main
```

> 这个仓库已经按 `cinqs` 配好了。**如果你是 fork 过来自己用**，跑
> `./tools/set-owner.sh <你的用户名>` 一次性换掉 README 徽章、文档链接、
> issue 模板、CODEOWNERS、CHANGELOG 的 compare 链接和 `pyproject` 里的项目主页
> —— 它会自动从 `pyproject.toml` 认出当前的旧名字，不用你手填。
> LICENSE 里的署名可以用第二个参数单独指定：
> `./tools/set-owner.sh <用户名> "<署名>"`。

### 2. 把 Wiki 推上去

```bash
./tools/sync-wiki.sh --push     # 会推到 <repo>.wiki.git
```

第一次推之前要先在 Settings 里勾上 **Wikis**，否则那个 wiki 仓库还不存在。

### 3. 设置（漏一个就是一个坏掉的徽章或链接）

| 位置 | 设置 |
|---|---|
| Settings → General → Features | 勾 **Wikis**、**Discussions** |
| Settings → Pages | Source = **GitHub Actions** |
| Settings → Actions → General | Workflow permissions = **Read and write**（release 要写） |
| Settings → Branches | 保护 `main`：要求 PR、要求 CI 绿 |
| Settings → Security | 打开 **Private vulnerability reporting**（SECURITY.md 靠它） |
| 仓库首页 | Social preview 用 `docs/assets/social-preview.png` |
| Topics | `proxy` `vless` `reality` `xray` `aliyun` `spot-instance` `macos` |

### 4. 打第一个 tag

```bash
git tag -a v0.1.0 -m "v0.1.0" && git push origin v0.1.0
```

会触发 Release workflow，产出 `Yi-v0.1.0.zip` + 校验和。

> **下载试用一次**。zip 里的 App 自带 agent/界面/源码，但它仍然需要机器上有
> Python 3.11+（`/usr/bin/python3` 在 macOS 上是 3.9，不够）。这一条要写进
> Release notes，否则用户会以为是 App 坏了。

## 五、签名与公证（还没做）

当前 macOS App **未签名、未公证**，用户首次打开需要右键 → 打开。
要做正式签名需要 Apple Developer 账号（$99/年），与项目"极便宜"的定位冲突，
所以**暂时不做**。真要做的话：

1. 把 Developer ID 证书导出成 base64 存进仓库 secrets
2. 在 `release.yml` 里 `codesign --deep --force --options runtime`
3. `xcrun notarytool submit --wait` 公证，再 `stapler staple`
