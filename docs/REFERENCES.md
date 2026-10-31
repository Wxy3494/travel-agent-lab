# 参考项目核验与取舍

核验日期：2026-10-14。先用用户指定的 GitHub 插件搜索、读取README/源码/依赖/提交与目录树；官方网页用于补充AutoGen、高德与DeepSeek文档。没有运行第三方仓库，因此“参考价值”不等于“已复现可运行”。完整目录核验摘录在 `reference_inventory.json`。

原始蓝图：[AgentGuide/02-travel-agent](https://github.com/adongwanai/AgentGuide/tree/663d91056d20823e2a2084b8e6a88c7706e8aa85/projects/02-travel-agent)。本次在commit `663d91056d20823e2a2084b8e6a88c7706e8aa85` 的目录API重新确认只有 `README.md`，blob `3643935a838782e1b4102927b9c63373227bc72d`；这是一份蓝图，并无可直接运行实现。借鉴其约束收集、来源、区间估算与只读规划目标；本项目使用用户选择的AutoGen与国内高德数据。

## 主要设计来源

### 1. microsoft/autogen 官方旅行示例与AgentChat实现

- [仓库固定版本](https://github.com/microsoft/autogen/tree/python-v0.7.5)，`python-v0.7.5`；版本更新提交 `83afbf5857aac683340d4c692194e548b1e8edda`（2025-09-29）。
- 阅读 [`_assistant_agent.py`](https://github.com/microsoft/autogen/blob/python-v0.7.5/python/packages/autogen-agentchat/src/autogen_agentchat/agents/_assistant_agent.py)、[Agent文档](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/agents.html)、[旅行示例](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/examples/travel-planning.html)。
- 价值：现代AgentChat的AssistantAgent、工具执行事件、反思与角色协作。采用0.7.5实际API和官方Replay客户端进行本项目测试，代码层显式限制迭代。
- 取舍：官方旅行示例帮助理解分工，不能当作国内实时旅游质量证明；我们增加严格业务状态、版本、Python算术和独立审计。
- 许可：代码是 [`LICENSE-CODE` 的MIT](https://github.com/microsoft/autogen/blob/python-v0.7.5/LICENSE-CODE)；文档属于根 [`LICENSE` 的CC BY 4.0](https://github.com/microsoft/autogen/blob/python-v0.7.5/LICENSE)。重用代码/文档应保留相应许可与归属。本项目使用依赖与自行编写代码，并通过链接标明参考。

### 2. OSU-NLP-Group/TravelPlanner

- [固定commit](https://github.com/OSU-NLP-Group/TravelPlanner/tree/e52c87f4ac348a3410c46dc3553c519db5ec5e23)，最新检索提交2026-05-24。
- 实际阅读 [`evaluation/commonsense_constraint.py`](https://github.com/OSU-NLP-Group/TravelPlanner/blob/e52c87f4ac348a3410c46dc3553c519db5ec5e23/evaluation/commonsense_constraint.py)、README、requirements与MIT许可证；目录包含 `hard_constraint.py`、`eval.py` 和独立训练/验证/测试资料。
- 分类：研究评测基准，有论文与可检查的约束评测代码，不是本地国内旅游产品。
- 价值：将资料依据、常识一致性、硬约束分开评估，区分固定资料规划与工具搜集。我们据此设计固定工具模型对照、独立预算/城市/日期审计和冻结协议。
- 不采用：美国静态数据库、旧LangChain/OpenAI依赖与其得分。不会将它的成功率套到高德实时查询，也不会把我们的回归集分数写成官方TravelPlanner成绩。
- 许可证：仓库MIT，复制实质代码需要保留版权与许可；外部数据有独立来源和使用条件。本项目未复制数据或核心实现。候选依赖含LangChain0.1.4 / OpenAI0.27.2等，与本项目SDK不直接兼容。

### 3. redis-developer/amr-autogen-travel-agent

- [固定commit](https://github.com/redis-developer/amr-autogen-travel-agent/tree/c7330fe1eb10947cbcc765d758de5171fb9bada2)，最新检索提交2025-09-04。
- 实际阅读 [`agent.py`](https://github.com/redis-developer/amr-autogen-travel-agent/blob/c7330fe1eb10947cbcc765d758de5171fb9bada2/agent.py)、Gradio入口、pyproject与uv.lock。锁文件AutoGen AgentChat/Core/Ext为0.7.2，pyproject未固定AutoGen版本。
- 分类：旅行对话与记忆演示，包含Tavily只读搜索、用户上下文和日历导出。有实际工具/模型代码；完整目录树未发现测试目录或许可证文件。
- 价值：用户上下文隔离、聊天交互和持久化业务的职责划分。我们采用每请求隔离角色上下文与可读版本，聊天入口支持继续补充和修改。
- 不采用：Redis/Mem0长期记忆、用户聊天实例缓存、自动打开本地日历、Tavily作为国内路线来源。首版没有需要向量搜索的长记忆，加入这些组件会增加费用和验收难度。
- 许可证：所核验完整树未找到LICENSE，不推断已允许复制。只借鉴设计，不复制实现或样式。不把README关于配置连通性或实时性的描述写成我们的实测。

## 已审核但不作为实现基础的候选

| 项目与版本 | 核心检查 | 借鉴与不采用 |
|---|---|---|
| [zinyando/autogen-multi-agent-travel-planner](https://github.com/zinyando/autogen-multi-agent-travel-planner/tree/8a5e8269488574c5c5adbb32a4daebd3d5479f08)；2025-07-19 | `travel-planner.py`使用ConversableAgent/GroupChat/GroupChatManager；pyproject为AG2 ^0.2.35。完整树只有少量教程文件，无测试/许可证 | 可读角色分工；API不是本项目0.7.5，主要依赖文字协商，没有本项目所需来源/版本/硬预算审计，不复制旧API |
| [bcefghj/multi-agent-travel-planner](https://github.com/bcefghj/multi-agent-travel-planner/tree/8132dfcded4539a4ca28b1c1fc316b93001c0b08)；2026-04-06 | 阅读Python `orchestrator/pipeline.py`、`api/app.py`、`tests/test_agents.py`、requirements；存在预算循环、并行编排与测试；完整树无LICENSE。其他语言README明确说明Mock | 借鉴有界返工、错误状态和模块拆分；不采用预设目的地/航班酒店Mock数据、默认补偏好和泛化简历数字。不转用其自定义Agent框架，不引用README的67%性能提升 |

另外检索了`autogen travel planner`及`travel planner streamlit agent`，返回包括vinayk9490/AI-Travel-Planner与若干CrewAI项目；没有完成它们的核心代码/许可审查，未将其列为优质或采用项目。选择上述三项主要来源依据是API权威性、真实约束评测代码和可检视的旅行对话实现，并保留两个反例帮助避免误用。

## 外部接口文档

- [高德路线2.0](https://developer.amap.com/api/webservice/guide/api/newroute)：公交使用citycode；日期参数可选，但不代表指定日车票可售能力。路线费用缺失时明确估算，驾驶路费不当作含油费停车的全价。
- [高德天气](https://developer.amap.com/api/webservice/guide/api/weatherinfo)：实际接入仍需账户验证；只接受返回日期的预报，不用短期预报推断整段远期行程。
- [DeepSeek模型与价格](https://api-docs.deepseek.com/quick_start/pricing/)：本次页面列出deepseek-flash及JSON/工具支持。报价会变化，当前网页不是账户已开通能力的证明。
- [DeepSeek JSON输出](https://api-docs.deepseek.com/guides/json_mode/)：采用JSON模式同时保留Schema校验与有限格式修复；服务兼容必须通过实际请求。

本项目没有复制第三方核心源码、测试集或网页样式。第三方资料是设计证据，不会覆盖产品需求、技术栈选择或只读权限范围。


## 新增界面参考：LvBanGPT

按用户2026-10-14提供的[旅行规划助手截图](https://github.com/yaosenJ/LvBanGPT/blob/main/img/%E6%88%90%E5%8A%9F%E9%A1%B5%E9%9D%A2.png)借鉴输入框、可展开偏好和下方结果的结构。GitHub插件查询到commit `ead1e3e3f34cba05732a6560776c4bb924ace435` 的app.py使用Gradio Textbox与Accordion。用户确认六项基础字段全部显示、保留下方文字修改，因此本项目采用该交互结构，继续使用现有Streamlit与AutoGen。本轮未复制该项目代码、图像、样式或引入它的依赖，也未将它列为已复现的高质量架构来源。
