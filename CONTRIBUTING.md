# 贡献指南

感谢你愿意花时间。这个项目的目标是**把"抢占式实例上跑代理"这件事做到可靠**，
所以对代码的要求偏向"能解释清楚为什么这么做"，而不是"能跑就行"。

## 环境

```bash
make setup     # uv + Python 3.12 + 虚拟环境
make check     # 提交前必须全绿：脱敏 + 文档链接 + 界面语法 + ruff + 单元测试
```

需要：macOS 11+ 或 Linux、`uv`、`node`（只用来检查界面脚本语法）。
没有 `uv` 但仓库里已经有 `.venv/` 时，`make check` 会自动用它。

## 提交前

```bash
make check
```

它会依次跑：

1. **脱敏检查** `tools/check-no-secrets.sh` —— 拦 AccessKey、私钥、真实云资源 ID
2. **文档链接** `tools/check-doc-links.sh` —— 文档里的本地链接必须能落地
3. 界面脚本语法检查（`node -e "new Function(...)"`）
4. `ruff check` + `ruff format --check`
5. `python -m unittest discover -s tests`（134 个用例）

**改了界面就一定要真的打开看一眼**——GUI 的失败模式是"静默中断"：
一处异常会让后面全部不执行，而单元测试完全看不见。
项目里装了 `playwright` skill，可以直接截图验证：

```bash
PWCLI=~/.codex/skills/playwright/scripts/playwright_cli.sh
"$PWCLI" open http://127.0.0.1:8765/ && "$PWCLI" screenshot --filename /tmp/ui.png --full-page
```

## 代码约定

- **不要提交真实凭据与真实云资源 ID**。测试和文档里用 `vsw-00000…` 这类占位值；
  第 1 步的脱敏检查会自动拦。AccessKey 只能放在 `~/.aliyun/config.json` 或环境变量里。

- **测试先行（或至少同时）**：每个真机 bug 修复都要配一个回归测试。
  参考 `tests/test_reconcile.py::PortTests`——那里锁的是一个**让整台机器断网**的 bug。
- **不要静默降级安全属性**。参数冲突要么报错让用户选，要么按更安全的一侧升级并在日志里说明。
  历史上就是静默忽略"保护期"导致实例被秒回收。
- **不要相信单次 API 返回**。阿里云不同 POP 之间数据会不一致（实测：按 ID 过滤查不到、
  裸列表查得到；`DeleteInstance` 报 NotFound 但实例仍在计费）。
  判断"资源还在不在"必须用裸列表 + 本地过滤，并且要连续多次确认。
- **日志一律 `%` 风格**：`log.info("x %s", v)`，不要 f-string（那是即时求值）。
  `tests/test_logging_hygiene.py` 会扫这个。
- **注释写"为什么"**，不写"是什么"。一个反直觉的分支旁边必须有一句话解释它防的是什么。
- 中文文案，英文标识符。

## 提交信息

用 [Conventional Commits](https://www.conventionalcommits.org/)：

```
fix(reconcile): 内核在跑时不要重新探测端口
feat(ui): 状态光环按安装阶段推进
docs: 补上 REALITY 伪装目标的证书链约束
```

## Pull Request

- 一个 PR 只做一件事；行为变更要在 `CHANGELOG.md` 的 `Unreleased` 下加一行。
- 说明**你验证了什么**、**怎么验证的**（命令 + 输出），比"我测过了"有用得多。
- 涉及界面的改动请附截图。

## 文档放哪儿

| 你改的是 | 放这里 | 为什么 |
|---|---|---|
| 使用者会查阅的事实（配置项、成本、FAQ） | `docs/` | 和代码同一个 PR 里 review，不会漂 |
| "赶紧用起来"的短说明 | `wiki/` | 见下 |
| 踩坑的提炼 | `docs/lessons.md` | 附根因和回归测试的链接 |
| 某一步当时到底发生了什么 | `docs/history.md` | 按时间顺序，不压缩成结论 |
| 面向使用者的版本变更 | `CHANGELOG.md` | 写"我能做什么了"，不是"我重构了哪" |

### Wiki 怎么改

GitHub 的 wiki 是**另一个仓库**，直接在网页上改就会脱离 review。
所以 wiki 的源文件在 `wiki/` 目录里，跟着代码一起走、一起 review：

```bash
./tools/sync-wiki.sh          # 演练：拉下来、覆盖、给你看 diff，不推送
./tools/sync-wiki.sh --push   # 确认后推上去
```

文件名即 URL：`Getting-Started.md` → `.../wiki/Getting-Started`，
所以**英文文件名、中文标题**，改名等于改 URL。

## 安全问题

不要开公开 issue，见 [SECURITY.md](SECURITY.md)。

## 许可

提交即表示你同意以 [MIT](LICENSE) 授权你的贡献。
