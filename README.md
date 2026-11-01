# 行旅 · 多 Agent 旅行规划系统

面向国内自由行的本地旅行规划助手。首页直接显示出发地、目的地、日期、天数、人数和全团预算六项输入框，点击生成后在表单下方展示行程，同时保留文字修改入口。结果包括每日安排、跨城交通、住宿晚数、全团预算区间、风险、来源和地图链接；每次修改保存新版本，可导出 Markdown。

**当前状态（2026-11-01）：以[统一验收摘要](docs/ACCEPTANCE.md)及其[源码哈希快照](docs/acceptance_snapshot.json)为准。** 本轮历史列表和资料整合含本机 Chrome 交互实测；美团只读试查取得回复，酒店可靠房价未通过核实。已有2026-10-15广州真实两日小样本和10-16武汉酒店定位记录，本轮未重跑真实模型或地图全链路。Docker、云端CI、其他路线及真实价格仍未完成验收。

## 本机启动

唯一项目目录为 `D:\zuoye\AI学习\多 Agent 旅行规划系统`，现有 `.venv` 已可运行，不需要重建项目。

**日常使用：在项目文件夹中双击 `启动行旅.cmd`。** 启动器会检查并在后台启动API与网页，服务就绪后自动打开默认浏览器，无需输入命令。已有服务会直接复用。无需安装窗口组件；启动失败会弹出提示，日志保存在已忽略的 `.local/`。关闭网页后后台服务仍可复用。

以下终端方式供开发、配置与排错使用：

两个 PowerShell 终端分别执行：

```powershell
cd 'D:\zuoye\AI学习\多 Agent 旅行规划系统'
.\scripts\start.ps1 api
```

```powershell
cd 'D:\zuoye\AI学习\多 Agent 旅行规划系统'
.\scripts\start.ps1 ui
```

打开 [旅行规划页面](http://127.0.0.1:8501) 或 [API 文档](http://127.0.0.1:8000/docs)。已运行时直接访问，避免重复启动；终端 `Ctrl+C` 停止对应服务。默认仅绑定本机地址，无登录鉴权，适用于个人本地演示。更新源码或模式后需重启API和UI，以加载新模型结构；直接复用旧进程不会保证加载新增字段。

启动脚本会检测对应端口的健康服务。若提示 `already running`，直接打开页面即可，脚本不会重复启动进程。`Port 8501 is not available` 或8000端口绑定错误通常表示已经有服务监听该端口。

从干净的 Python 3.12 环境复现（项目声明支持 3.11～3.13，当前实测只有 Windows Python 3.12.14）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m scripts.doctor
```

`requirements.lock` 是当前已测试环境的完整固定版本清单，未包含安装包哈希；它锁定版本，不保证所有平台均可安装。AutoGen AgentChat / Ext 固定为 **0.7.5**。

## 填表生成，文字修改

填写六项基础信息：出发地广州、目的地广州和深圳（按顺序）、未来日期、5天、2人、全团预算6000元。兴趣、必去/排除、交通、住宿与费用预算收在“添加旅行偏好与详细要求”中。住宿、餐饮和门票各填一个金额，留空则自动估算；修改已有行程时留空沿用当前估算。

填写有误时，具体原因直接显示在对应输入框下方，修正后自动清除；可选项有误时提交会自动展开。人数指包含本人的出行总人数。修改输入不会自动生成行程，仍需点击生成按钮。[表单校验说明](docs/FORM_VALIDATION.md)。

侧栏自动显示最近5条旅行，点击即可打开；“查看全部历史”支持搜索和分页，所有历史及版本仍保存在原数据库。界面不显示策略选择、编号输入或调试JSON。美团景点与酒店资料结合进每日安排，明确的开放/闭馆提示作为参考参与排程；取得参考不等于官方核验或已预约。[历史与资料整合](docs/PRODUCT_HISTORY_REFERENCES.md)。

生成后可以改上方表单再点“重新生成行程”，或在结果下方输入“预算改成3000元”“排除广州塔”“日期改成2026年11月1日”。文字修改基于已生成的方案，修改结果会同步回上方表单，保留其他字段，旧版本仍可读取。侧栏的“开始新的旅行”开始独立行程；历史版本、开发信息默认收起。

离线模式使用广州、深圳、珠海的人工样例与有限关键词解析，也接受“广州市”等全称。支持完整年月日、今天/明天/后天、基础人数/天数/预算、必去/排除、兴趣、房间数、公共交通/自驾、返程、无障碍，以及“第二天轻松一点，只安排下午”“只修改第二天下午，游览30分钟”等明确规则。它不是通用中文理解；未识别的请求仍待补充，所有离线地点、分类、天气、费用和路线均是测试数据。

## 新增能力怎么用

- 时间：填写每天从住处出发和最后一个游玩项目结束的时间。默认按已到达目的地安排当地游玩，出发地往返交通单独估算，不占首末日游玩时段；目的地之间换城仍检查当天时间。[往返交通说明](docs/HOME_TRANSPORT.md)。午餐与休息显示为活动，费用按每日餐饮统一计入。
- 地点确认：规范名唯一时绑定POI ID；同名、简称或无法确定时，生成结果下方显示候选。选择后继续规划。演示别名表仅适用于演示数据，不冒充真实POI核验。
- 冲突调整：超预算或时间不足时查看“选择一个调整方案”，点击后才应用并生成新版本。人数、必去地点和单价不会自动放宽。
- 景点入场：自动核对已适配场馆的官网常规规则，检查闭馆、入场时段、预约和基础门票；未知不会当成已核实。结果下可查看原文、补充资料、确认已预约、重新核验。[覆盖范围与票务 API 接口](docs/ADMISSION_VERIFICATION.md)。
- 美团参考：真实模式配置个人开发者凭证后，自动查询行程涉及景点的开放、预约、票价参考及入口，展示在行程下方并随文档导出；文字咨询不冒充实时库存或严格核验。[接入说明](docs/MEITUAN.md)。
- 精确接驳：在“酒店、车站与开放信息”查询候选并明确选定位置，重新计算每日起终点与换城衔接。自行提供的开放/闭馆或室内属性需填写依据，作为用户信息保存。
- 路线：对邻近候选顺序与原顺序使用同一Provider逐段耗时比较；只采用较短方案，显示每日交通时间。调用预算不足或有开放时间规则时保留原顺序，不承诺全局最优。
- 分日和雨天：展开“分日修改与雨天替换”。只修改指定日期或下午；未影响日期保留活动、时间和费用。全局变更或旧版缺少分日费用时明确说明扩大重算范围。雨天只能选择有演示分类或用户属性依据的室内候选，选择后重新算路；真实模式不会自动把名称含“博物馆”的地点当成已核实室内。
- 历史：展开侧栏“历史旅行列表”，按城市/日期查找并打开。结果下方可查看版本、比较活动/时间/费用/约束、读取最新版或从旧版复制为新旅行。旧版修改仍受409保护。

真实模式用 AutoGen 模型提取字段补丁，资料 Agent 调用只读工具，规划 Agent 选择已查询 POI，审核 Agent 给修改意见。Python 控制器做预算、人数、间夜、日序、城市顺序、来源与时间审计。最多两次返工，硬约束不通过不会输出 `ready`。

## 配置真实接口

```powershell
Copy-Item .env.example .env
```

只在本地 `.env` 填入 `DEEPSEEK_API_KEY`、`AMAP_API_KEY`，不要发送到聊天或提交 Git。设置 `TRAVEL_MODE=live` 后重启 API。先查看 [真实接入与验收步骤](docs/LIVE_ACCEPTANCE.md)，确认模型名、账户权限、地图配额与费用。

默认 `MODEL_NAME=deepseek-flash`、`MODEL_BASE_URL=https://api.deepseek.com`。模型调用默认禁用思考模式，以适配 AutoGen 0.7.5 的消息转换；API 使用次数与输出 token 有上限。价格单价留空，只有自行核对并填写人民币单价后才显示估算模型费用。费用估算不是账户账单，也不是全局100元硬限额。

## 测试与评测

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m ruff check app scripts tests
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m scripts.evaluate
.\.venv\Scripts\python.exe -m scripts.evaluate --suite extended --output artifacts/evaluation-v2
# API 正在以 demo 模式运行时：
.\.venv\Scripts\python.exe -m scripts.api_smoke
```

模型测试是额外的付费运行，不会被上述命令触发：

```powershell
.\.venv\Scripts\python.exe -m scripts.capability_probe --run-live
.\.venv\Scripts\python.exe -m scripts.evaluate --live-compare --split dev --max-cases 2 --output artifacts/model-pilot
.\.venv\Scripts\python.exe -m scripts.evaluate --suite extended --live-compare --max-cases 2 --output artifacts/model-extended-pilot
.\.venv\Scripts\python.exe -m scripts.live_smoke --run-live
```

真实端到端脚本包含广州两日、深圳两日、广州—深圳五日、广州—珠海四日及一次单城预算修改。固定工具模型对照、真实地图端到端、浏览器交互是不同验收层级。详细结果见 [评测报告](docs/EVALUATION.md)。

## API

| 接口 | 用途 |
|---|---|
| `POST /trips` | `prompt` 或结构化 `constraints` 创建行程，`strategy=multi/single` |
| `GET /trips?query=广州` | 最新行程列表，按城市或日期查找 |
| `GET /places/search?city=广州&query=酒店&category=hotel` | 查询同城地点候选，类型为attraction/hotel/station |
| `GET /trips/{id}?version=1` | 读取指定版本；省略 version 读取最新 |
| `POST /trips/{id}/revise` | `base_version` + `instruction` / `changes` / `adjustment_id`；可指定target_day和target_period；冲突返回409 |
| `GET /trips/{id}/versions` | 版本列表 |
| `GET /trips/{id}/compare?from_version=1&to_version=2` | 约束、日程、时间和预算差异 |
| `POST /trips/{id}/clone` | 从所选version复制为独立的新旅行 |
| `GET /trips/{id}/export?version=1` | 导出 Markdown |
| `GET /health` | 存活与模式/密钥存在状态，不验证真实账户连通性 |

成功 HTTP200也可能是 `needs_input`、`needs_review`、`blocked` 或 `failed`；必须检查业务状态。

## Docker 与公开准备

Dockerfile、Compose 和离线 CI 配置已提供，**容器构建和运行尚未验收**。有 Docker 环境后执行 `docker compose config`、`docker compose up --build`，访问 health 与页面，再完成单城/跨城/重启持久化检查。CI 配置未推送，也没有云端运行结果。

SQLite 运行数据位于 `data/runtime/`；密钥、运行记录、虚拟环境和评测原始结果均在忽略列表。执行 `python -m scripts.package_public` 生成只含源码、测试、配置样例和文档的本地 ZIP。本地 Git 已保存修复前基线与本次修复，ZIP 清单记录提交与 SHA256；另交付 history.bundle 供还原历史。打包不会上传、推送或发布；尚未选择开源许可证。

## 文档地图

- [当前验收与交付](docs/ACCEPTANCE.md)：唯一当前状态入口；测试、源码哈希、历史真实小样本与待验收范围。
- [交付与复现](docs/DELIVERY.md)：源码包、历史包、资源校验与安装边界。
- [产品设计](docs/PRODUCT_DESIGN.md)：范围、输入、聊天流程、预算与信息边界。
- [技术架构](docs/ARCHITECTURE.md)：模块、调度、状态、校验、版本并发与安全。
- [参考项目报告](docs/REFERENCES.md)：固定版本、核心代码审查、取舍与许可证。
- [评测证据](docs/EVALUATION.md)：测了什么、没测什么、冻结集局限。
- [真实接口验收](docs/LIVE_ACCEPTANCE.md)：密钥接入、费用核对、四条端到端。
- [演示脚本](docs/DEMO.md)：聊天演示与预期结果。
- [简历证据](docs/RESUME_EVIDENCE.md)：当前可以陈述的成果与不能陈述的提升。
- [学习路线](docs/LEARNING_GUIDE.md)：后续自行阅读、复现、解释和修改的练习。

当前支持1～3个目的地、1～30天、最多10人，用户指定城市顺序。长行程地点不足时留出自由活动日，不重复或编造景点。住宿/车票为估算，地图查询不代表出行日余票或房态；具体车站、酒店未指定时使用城市中心区域衔接假设。不会预订、付款、取消订单，也不会保存身份证、护照、银行卡或密码。真实30天、扩展城市、全国覆盖、并发吞吐、质量提升、商业落地和用户独立掌握均无已完成验收。

2026-10-16 更新：旧行程与新行程均以通俗说明展示和导出；酒店提供地图、房价搜索入口及每间每晚预算参考，未接入实时房价。表单只保留每天从住处出发、最后一个游玩项目结束两个时间，随后返程单独计算；可选填写酒店或住宿地址，未填按住宿区域（城市中心）估算。天气随行程自动显示，跨城当天分别展示出发和抵达城市，只匹配今天起七天内已发布的日期；默认高德仅提供当天至第四天，更换七天天气服务的适配约定及房价 API 选择见 [天气与酒店接入说明](docs/WEATHER_AND_HOTELS.md)。离线回归使用评测协议 v3：仅把原“8天越界”迁移为“31天越界”，原始冻结 v1 文件保留，不能直接比较跨协议通过率。
