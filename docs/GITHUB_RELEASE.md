# GitHub 上传与验证记录

记录时间：2026-11-02T11:26:50+08:00。

公开仓库：[Wxy3494/travel-agent-lab](https://github.com/Wxy3494/travel-agent-lab)，默认分支 `main`。本次上传保留已有源码提交历史，并补充 README 项目介绍、通用克隆安装说明，以及原有两份本地评审文档。仓库简介和主题已配置。当前未选用开源许可证。

## 本次本机验证

| 检查 | 结果 | 适用范围 |
|---|---|---|
| pytest | 335 passed，45.25 秒 | 现有 Windows Python 3.12 虚拟环境；含规则、Replay、MockTransport 和 AppTest |
| Ruff | 通过 | app、scripts、tests |
| pip check | 通过 | 当前虚拟环境依赖 |
| 离线评测 v3 | 30/30 | 人工样例与已知契约，模型调用为零 |
| 扩展评测 v2 | 10/10 | 人工样例与已知契约，模型调用为零 |
| 发布前历史扫描 | 276 个历史 blob，未发现匹配项 | 检查本地已配置密钥、常见 GitHub/模型令牌和私钥标记，以及禁止上传的运行文件路径 |

首次回归因旧系统临时目录权限冲突未完成；改用项目内独立临时目录后全量通过。未修改业务代码或系统权限。

上传范围包含源码、测试、评测样例、配置样例、界面资源、文档及 GitHub Actions 配置。`.env`、运行数据库、个人行程和对话、虚拟环境、缓存、原始验收产物及本地日志均未纳入 Git。

## 验证边界

本次没有调用付费模型、地图或美团接口，没有新增真实旅行端到端、Docker、移动端或负载验收。功能与历史真实小样本的范围仍以 [2026-11-01 验收摘要](ACCEPTANCE.md) 为准；该摘要的“无外部推送”等内容描述其记录时的历史状态。

GitHub 云端检查须查看 [Actions](https://github.com/Wxy3494/travel-agent-lab/actions) 的实际结果。本机测试结果不能替代云端运行记录。扫描未发现匹配项仅是上述检查范围内的结论。

## 首次云端检查与跨平台发布修复

首次 [GitHub Actions 运行](https://github.com/Wxy3494/travel-agent-lab/actions/runs/41383186733) 在 Ubuntu / Python 3.12.15 上完成安装、依赖检查、Ruff 和 335 项测试（46.76 秒），随后冻结样例字节哈希检查失败。

原因是 Git 的全局文本换行规范将原本按 CRLF 冻结的 `cases-v3.json` 转换成 LF，另外两份样例没有发生字节变化。发布修复在 `.gitattributes` 中为 `evals/cases.json`、`evals/cases-v3.json`、`evals/extensions-v2.json` 设置 `-text`，使 Git 保存原始字节。使用重新规范化暂存将 v3 样例原始字节纳入 Git，并逐份核对暂存区：三份文件均与既有清单所记录的 SHA256 一致；JSON 数据、既有哈希清单、评测协议与业务代码均不变。

