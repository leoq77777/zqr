# Quant Research Agent 开发方案与服务调研

调研日期：2026-10-12。状态：修订方案，尚未实现；已验证 Coinbase 公开历史行情样本。

## 1. 范围与结论

默认范围为 BTC/ETH 现货、小时线/日线、做多或空仓；账户地区尚未确认。纽约时区不代表纽约居民。衍生品、股票、Tick 级研究需要另补市场与数据规格。

采用一个通用 coding agent 作为 Research Agent，自建薄的研究控制层，配合确定性回测与验证、版本化实验记录。Python 主导，计算核心可替换；第一版不强制 C++，不引入多 Agent 或分布式集群。

第一版选择 Coinbase Advanced Trade：公开历史行情用于数据导入，原生 WebSocket 用于后续实时行情，私有订单 API 延后接入。Gemini 为备用候选，暂不实现第二个适配器。账户地区尚未确认，因此实盘资格仍需单独验证；公开历史行情开发不依赖私有交易权限。

本次包含官方文档调研和公开历史行情实测。2026-10-12，从执行环境以无密钥 GET 请求读取 BTC-USD 与 ETH-USD，start=1704067200、end=1704153600、granularity=ONE_HOUR，两者均返回 HTTP 200 和 25 根 K 线，最早/最晚 start 分别为上述两个时间戳。样本包含结束边界，因此导入器必须自行裁剪为 [start, end)、排序并去重。这不是全历史覆盖验证，也不是私有交易 API 验证。未登录账户、生成密钥或提交订单。账户资格、实际费率和持续网络可靠性仍待验证。

## 2. 模块与技术

| 模块 | 初始实现 | 边界 |
|---|---|---|
| Agent | 可编程调用的通用 coding agent，通过 AgentRunner 适配 | 检索资料、编写候选策略、调试、调用固定工具、生成报告；无交易密钥 |
| Controller | Python 状态机、Pydantic | 控制预算、权限、重试、停止与恢复 |
| Strategy | JSON 模板 + 固定接口的 Python 插件 | 模板参数研究与开放策略研究并存；候选代码仅在隔离 worker 执行 |
| Data | pandas、PyArrow、Parquet | 数据适配、质量检查、不可变快照 |
| Backtest | NumPy、范围有限的事件循环 | 明确成交、手续费、滑点与账户账本 |
| Evaluation/Validation | NumPy、pandas | 固定指标、基准、时间切分、滚动验证 |
| Experiments | SQLite、JSON、Parquet | 全部尝试，包括拒绝、失败与中止 |
| Report/UI | Markdown、Matplotlib、CLI | 先交付可重放报告，按需求再加界面 |
| Paper/Execution | Python 独立进程、交易所 SDK 或 HTTP/WebSocket 客户端 | 风控、订单状态、仓位对账；研究进程不能发送订单 |
| Engineering | Git、锁定依赖、pytest、Ruff | 计算正确性与集成检查 |

选择可安装且依赖兼容的稳定 Python 版本，实际建项目时锁定。当前不指定未经安装验证的版本组合。

研究接口：`ExperimentRequest(strategy_ref, parameters, dataset_id, split_id, engine_config_id) -> ExperimentResult`。strategy_ref 为模板 ID 或冻结代码制品哈希，Python 插件同时锁定依赖。
运行接口：`StrategySnapshot -> TargetPortfolio -> RiskDecision -> OrderIntent -> BrokerEvent`。
适配器声明自身支持的订单类型、精度、最小订单、事件字段与查询能力，不强行抹平交易所差异。

## 3. 数据与研究可信度

- 行情记录 venue、instrument、bar_open_time、bar_close_time、available_at、UTC 时间、OHLCV 和来源版本；实时事件另记录接收时间。
- 丢弃未完成 K 线；检查重复、缺口、负值、OHLC 一致性和时间顺序。无交易区间补值规则必须明示。
- 同一根收盘生成的信号默认下一根开盘成交；加入成本和滑点，记录此假设的局限。
- 限价单不能只因 K 线触价就默认全部成交；MVP 优先目标仓位配合市场调仓。
- 开发、验证、最终留出区间按时间拆分。留出数据由独立验证入口控制，Agent 迭代工具不可访问。
- 策略冻结后才运行最终留出测试；看过的留出结果不能继续声称未使用。
- 记录参数网格、跨会话累计试验数、父实验和失败原因，限制轮数与候选总数。
- 每次结果绑定策略规格或插件代码哈希、数据哈希、引擎版本、成本配置和切分配置；LLM 原始建议与程序指标分开保存。
- 基准包括买入持有与现金；检查成本加倍、邻近参数、不同时间段的表现。指标年化按实际频率定义。

## 4. 交易 API 调研

| 服务 | 官方能力证据 | 测试环境 | 地区与选型结论 |
|---|---|---|---|
| Coinbase Advanced Trade | REST 订单管理、WebSocket 行情、官方 Python SDK | 静态预设响应，适合协议检查，不是成交模拟 | 第一版选定；公开历史样本已成功读取，私有交易权限待验证 |
| Gemini | REST、WebSocket 与客户端订单 ID 等文档 | 测试资金、机器人订单簿与完整交易所功能 | 纽约现货候选；sandbox 适合订单生命周期测试，其行情不是生产流动性 |
| Kraken | 现货 API、OHLC、历史 OHLCVT 下载 | 现货订单 validate 用于校验；不能当作 paper matching engine。衍生品 demo 与现货不同 | 官方美国说明排除 NY、ME；非纽约且符合资格时再评估 |
| Alpaca | Trading API、crypto 订单、行情与 paper trading | 实时行情驱动模拟成交 | 适合测试 API 流程；crypto 实盘地区需单独确认，旧地区 FAQ 不足以确认当前纽约资格 |
| Binance.US | 官方支持地区页面 | 本方案未确认可用测试环境 | 官方不支持地区包含纽约；纽约账户不列入首选 |

证据：
- [Coinbase API 概览](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/overview)、[官方 Python SDK](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/sdk)、[静态 sandbox](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/sandbox)。
- [Gemini sandbox](https://developer.gemini.com/get-started/sandbox)、[REST API](https://developer.gemini.com/rest)。
- [NYDFS 受监管实体清单](https://www.dfs.ny.gov/virtual_currency_businesses)：实体许可不等于所有产品、所有账户都可用。
- [Kraken 美国客户说明](https://support.kraken.com/hc/de-de/articles/quick-start-for-clients-in-the-united-states)、[API FAQ](https://support.kraken.com/in/articles/advanced-api-faq)。Kraken 衍生品测试环境相关公告存在迁移信息，应在采用前重新确认，不能照抄旧 demo 地址。
- [Alpaca paper 文档](https://docs.alpaca.markets/us/docs/paper-trading)、[crypto 交易文档](https://docs.alpaca.markets/us/docs/crypto-trading)、[地区 FAQ，页面标记 May 2023](https://alpaca.markets/support/alpaca-cryptocurrency)。
- [Binance.US 地区支持清单](https://support.binance.us/en/articles/9842798-list-of-supported-and-unsupported-states-and-regions)。

Alpaca paper 不模拟市场冲击、延迟滑点及限价单队列位置；不能用 paper 收益证明实盘容量。跨平台 paper 可验证通用订单流程，但不能证明 Coinbase 或 Gemini 的实际成交表现。

交易费用按实际账户 maker/taker 档位读取并保存，不使用未确认的统一费率。Coinbase 官方说明费用依产品与账户档位变化：[费用说明](https://help.coinbase.com/en-gb/coinbase/trading-and-funding/advanced-trade/advanced-trade-fees)。

原生 SDK 优先用于第一个交易适配器；CCXT 可用于多交易所行情和后续适配，但仍需交易所特定规则与错误处理。[CCXT 手册](https://github.com/ccxt/ccxt/wiki/manual)

## 5. 行情与相关服务

| 需求 | 方案 | 已确认限制或待验证项 |
|---|---|---|
| 长历史 K 线 | 目标场所历史渠道；Kraken 官方下载作辅助样本 | Kraken REST OHLC 最多最近 720 条，不能通过 since 获取更早数据；下载截止范围与补全缺口需检查 |
| Coinbase 历史 K 线 | 按时间窗口读取并本地缓存 | 单次最多 350 根；BTC/ETH 2024 年小时样本已无密钥读取，完整历史覆盖仍需验证 |
| 实时行情 | 目标交易场所原生 WebSocket | 测量事件间隔、断线、重连、订阅限制、序列缺口；必要时 REST 恢复 |
| Tick/L2 历史 | 按需求评估 Tardis.dev | 提供交易、订单簿与衍生品数据；先验证目标场所覆盖、样本质量和购买报价 |
| 常驻运行 | 本地研究；paper 阶段 Linux VPS + systemd | 网络、固定出口 IP、时间同步、自动恢复与备份 |
| 云主机候选 | AWS Lightsail 或等效固定 IP VPS | Lightsail 支持静态 IP；按实际区域、规格、存储、流量核价，暂不采购 |
| CI | GitHub Actions | 跑测试和构建；官方说明定时运行可能延迟，不承担交易时钟 |
| 监控 | 结构化日志、健康检查、事件告警 | 数据陈旧、对账失败、订单状态不明、进程退出触发；paper 开始前实施 |
| 备份 | 配置、实验与账本定期备份 | 本地起步；多机阶段对象存储，验证恢复 |

来源：[Kraken OHLC](https://docs.kraken.com/api-reference/market-data/get-ohlc-data)、[历史下载](https://support.kraken.com/in/articles/360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data)、[Coinbase candles](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/public/get-public-product-candles)、[Tardis 数据](https://docs.tardis.dev/downloadable-csv-files/overview)、[Lightsail 静态 IP](https://docs.aws.amazon.com/lightsail/latest/userguide/lightsail-create-static-ip.html)、[Actions 定时限制](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)。

跨场所行情只能作为明确标记的研究数据：Kraken 的历史收益不能直接当作 Coinbase 可成交收益。数据许可、保存及再分发条款需要随选定数据源确认。

## 6. 接入验收：从文档可用到实际可用

为每个候选建立 readiness 表：文档确认、网络实测、账户确认、sandbox 验证、生产只读验证；分别记录时间与证据。

1. 确认账户主体、注册地、产品资格、KYC、API 交易权限及目标交易对。
2. 在实际运行网络测 REST 成功率、p50/p95/p99 延迟与错误响应；不要用浏览器能打开网站推断 API 连通性。
3. 查询产品精度、最小订单、状态与成本。用十进制定点处理订单数量和价格。
4. 运行低强度行情订阅，记录 heartbeat、断线和恢复；建议连续观测至少 24 小时，这是工程观测，不是盈利验证。
5. 使用只读密钥查账户、余额和已有订单；密钥不进入模型上下文和日志。
6. 在合适的 sandbox 或本地模拟器验证提交、取消、拒绝、部分成交、重复事件、超时和重启。
7. 请求超时进入 UNKNOWN 状态，按 client_order_id 查询并对账；不能盲目重发或假设客户端 ID 天然保证幂等。
8. paper 检查重启恢复、账户余额和成交账本一致性、陈旧行情阻止新订单、风险限制与停止开仓机制。

不通过最终资格与权限验证的场所不得成为实盘依赖。本方案不授权提交真实订单。

## 7. Scalability 与 C++

实验任务独立、数据快照不可变、结果统一提交；先单机进程池，SQLite 由单写入入口维护。多机并发时迁移 PostgreSQL 与对象存储，再加入队列和 worker lease，避免重复计算与重复执行。

首个性能基准建议为两资产五年小时线、100 个固定规格候选，记录机器规格、墙钟时间、峰值内存、CPU 热点和正确性结果；这是待测工作量，不是吞吐量承诺。

NumPy 起步。若计算热点明确且无法满足设定预算，再选 Numba 或 C++。C++ 仅用于事件循环、订单簿重建等热点，通过 pybind11 接口复用同一套黄金案例。盘口研究需要独立的更大数据和性能基准。

扩展触发器：内存持续超预算、研究队列等待超出可接受时长、数据库写锁影响任务、核心循环占据主要耗时、明确 Tick/L2 或低延迟需求。先优化算法和数据复制，再增加基础设施。

## 8. 开发阶段与交付

| 阶段 | 工作 | 完成标准 |
|---|---|---|
| P0 服务与契约验证 | Coinbase 全目标区间采样；确认地区；固定策略和成交契约 | BTC/ETH 公开样本已通过，完整区间覆盖、缺口和增量规则验收 |
| P1 确定性研究 | 数据快照、两类策略、回测账本、指标与 CLI | 手算黄金案例通过，无同根未来信息成交，可重放 |
| P2 验证与实验 | SQLite、全部试验记录、时间切分、成本分析、报告 | 独立留出权限、完整版本身份、失败与中断可恢复 |
| P3 Agent | 通用 coding agent 适配、隔离候选目录、固定工具、预算、停止规则 | 一个问题完成有限轮研究，输出可重放报告，允许没有有效策略 |
| P4 Paper | 实时行情、本地模拟成交、一个外部适配器、风控与对账 | 超时、断线、重复事件与重启故障测试通过；无状态不明的新订单 |
| P5 条件式优化 | 参数并行、性能分析、必要时原生核心 | 达到事先定义的吞吐量与内存预算，同时结果一致 |

paper 盈利验收单独制定观察期限、交易样本数、成本与异常行为标准，不能仅凭运行数天无报错通过。实盘是后续独立阶段。

## 9. 成本与尚待确定项

本地阶段不新增云服务；软件、模型调用、交易费用和数据订阅分别预算。模型按真实 token 用量乘选定模型当时价格；预算耗尽由控制器停止。云与付费数据在目标明确后询价，不给未经核实的月费总额。

优先明确：市场与账户地区、已有账户、是否计划衍生品、研究频率和并行规模、历史覆盖需求、月度模型/数据预算，以及是否必须 24/7 运行。

当前建议从 P0 与 P1 开始：先验证数据路径和账户条件，再建设可信研究引擎。Research Agent 不依赖交易 API 才能开发，但交易场所的成本与数据条件必须提前进入研究假设。


## 10. 修订后的 Agent 执行方式

自建 Research Controller，不自建完整代码编辑、调试与上下文管理系统。通用 coding agent 通过 AgentRunner 接收研究任务、允许的数据 ID、工具与预算，返回候选代码、假设、实验引用和报告。执行器可更换；具体 Claude Code/Codex 的编程入口、许可与运行方式在集成前另做兼容验证，本方案不假定产品接口完全相同。

第一步允许人工启动 coding agent，使用固定研究 CLI；流程稳定后才自动化派发和恢复。Controller 记录外层实验预算，但不能假设能拦截 agent 的任意 shell：生成代码必须在独立环境执行，并限制 CPU、内存、时间、网络与文件权限。

候选目录可写，回测、评估、成本和分割规则只读，最终留出数据不挂载；仅授予开发/验证快照。数据下载由控制器的数据进程负责。研究环境不挂载交易密钥。升级策略时校验依赖和代码身份，再在独立验证环境运行。隔离环境具体采用容器或同等级机制，须在开放 Python 插件之前完成；容器仅用于代码隔离，不引入 Compose 服务平台。

## 11. 获取历史数据与回测的产品流程

开发完成后用户可以指定 Coinbase、BTC-USD/ETH-USD、时间区间、周期，执行：

1. fetch-data：按最多 350 根的窗口分批下载，退避重试、限速、断点续传；保存原始响应与请求记录。
2. validate-data：统一 UTC、[start,end) 裁剪、去重、移除未完成 K 线、报告缺口；不可静默跨缺口。
3. freeze-dataset：生成 Parquet 快照及 manifest，标明实际最早/最晚数据、覆盖率、缺口和哈希。
4. backtest：固定策略与成本，在批准的数据切分上运行，生成成交、净值、指标和实验 ID。
5. report/replay：展示结果及数据局限；同一快照可脱网重放。

首次开发目标：BTC-USD、ETH-USD 的小时线与日线；先验证 2021-01-01 至最近已完成周期的覆盖，目标不是已有保证。若 API 无法提供完整区间，则缩短到明确报告的覆盖区间，或引入具有 Coinbase 场所覆盖的补充数据供应商，禁止换场所后仍标作 Coinbase 数据。

增量更新使用重叠窗口，产生新快照，不改写旧实验数据。所有窗口共用限速器，禁止并行 worker 各自独立放大请求量。

验收包含历史窗口拼接、结束边界、重复数据、断点恢复和完整回测重放。数据获取在 P0/P1 即开发和验证，不能等全部开发完再确认服务可用性。

Coinbase sandbox 为静态响应，后续 paper 使用 Coinbase 生产公开行情驱动本地模拟成交；订单私有接口只在独立阶段接入。当前历史 K 线选择不覆盖 Tick、L2 队列或衍生品历史需求。

## 12. Researcher角色适配与执行器选型

执行器不预先按品牌排名，采用 benchmark/README.md 中的比较协议。可迁移角色定义为 benchmark/role_prompt.md；八个公开开发任务在 benchmark/tasks.json。当前已准备合成数据、任务、评分规则与结果 schema，未实现框架适配器、统一实验服务及自动评分器，也未运行付费模型比较。

先接 Claude Agent SDK 与原版 Codex，使用相同研究工具、权限和产物契约做初筛。若要区分模型与框架影响，在兼容的同模型配置下扩展 OpenHands、Deep Agents 与最小自建 loop。不用跨模型的结果声称某框架更强。Codex源码修改须基于已复现的接口缺口，进行同模型前后对照。

研究员交付假设、预先实验计划、候选制品、全部实验引用与证据校准结论。正式选择优先无严重方法/权限错误、可复现性、低人工干预和每个合格交付的成本；不以最高回测 Sharpe选执行器。先单 Agent，团队能力在同总预算下单独比较。
