# wiki/ —— wiki 的源文件

GitHub 的 wiki 是**另一个独立的 git 仓库**（`<repo>.wiki.git`），
不在主仓库里。如果直接在网页上编辑 wiki，内容就和主仓库脱钩了：
没人 review、没人跟着版本走、改代码时也不会想起来同步。

所以这里的做法是：**wiki 的源文件放主仓库的这个目录里**，跟着代码一起 review、
一起进 `main`；再用一条命令推到 wiki 仓库。

## 发布

```bash
./tools/sync-wiki.sh          # 先预览要改什么
./tools/sync-wiki.sh --push   # 确认后真的推上去
```

## 约定

- 文件名即 URL：`Getting-Started.md` → `.../wiki/Getting-Started`
  —— 所以**用英文文件名、中文标题**，改名等于改 URL
- `_Sidebar.md` 是左侧导航，`_Footer.md` 是页脚，GitHub 会自动带上
- 页面之间用 `[文字](页面名)` 互链，**不要带 `.md`**
- 指向主仓库文件的链接要写全 `https://github.com/cinqsme/yi/blob/main/...`

## 和 `docs/` 的分工

| | 给谁看 | 特点 |
|---|---|---|
| `docs/` | 想懂原理、想改代码的人 | 完整、有取舍分析、有踩坑根因 |
| `wiki/` | 想赶紧用起来的人 | 短、按问题组织、指向 `docs/` 要深度 |

两边都会出现的内容（比如配置项）**以 `docs/configuration.md` 为准**，
wiki 里只列最常用的几项，避免两处维护、越改越不一致。
