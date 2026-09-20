# 文档索引

这个目录里是**权威文档**：和代码在同一个仓库、同一个 PR 里 review，
所以不会像外部 wiki 那样悄悄过期。

想"赶紧用起来"的话，[Wiki](https://github.com/cinqsme/yi/wiki) 更顺手；
想懂原理、想改代码，就在这儿。

## 上手

| 文档 | 什么时候看 |
|---|---|
| [quickstart.md](quickstart.md) | 第一次装，从零到能上网 |
| [configuration.md](configuration.md) | 想改规格 / 端口 / 伪装目标 / 预算 |
| [faq.md](faq.md) | "要不要域名？""多少钱？""被回收了怎么办？" |
| [troubleshooting.md](troubleshooting.md) | 连不上、连上了打不开网页 |
| [runbook.md](runbook.md) | 运维手册：按**错误码**查怎么处理 |

## 理解设计

| 文档 | 内容 |
|---|---|
| [architecture.md](architecture.md) | 三个进程、状态机、期望状态与调和循环、为什么是竞价实例 |
| [design.md](design.md) | 界面设计系统：令牌、字阶、状态光环、动效原则、CSS 陷阱 |
| [lessons.md](lessons.md) | **踩坑提炼**：真实世界的坑（阿里云会重发 IP、不同 POP 数据不一致）与我方代码的锅 |
| [history.md](history.md) | 按时间顺序的工程日志，含两次实跑验收记录 |

## 参与

| 文档 | 内容 |
|---|---|
| [RELEASE.md](RELEASE.md) | 发版检查单（含仓库设置、签名与公证的现状） |
| [manual-first-run.md](manual-first-run.md) | 最早那次的**纯手工**流程记录。现在不用这么做，留着是为了出问题时能分清"是协议的问题"还是"是脚本的问题" |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | 环境、约定、PR 要求 |
| [../AGENTS.md](../AGENTS.md) | 给 AI 编码助手的操作手册：约束、代码地图、怎么验证 |

## 规格

| 文档 | 内容 |
|---|---|
| [../SPEC.md](../SPEC.md) | 项目总体规格 |
| [../app/SPEC.md](../app/SPEC.md) | 图形界面与 agent 的规格（含抗墙管理的设计草案） |
