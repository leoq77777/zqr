# Strategy contract v0.1

Python入口 `generate_targets(bars: list[dict], params: dict) -> list[float]`。

bars已按UTC时间升序，包含 start/open/high/low/close/volume；start是bar open的Unix秒时间戳。函数输出每根已完成bar close时决定的目标权重，长度必须与bars相同，范围[0,1]。权重最早在下一根open成交。不能执行网络、文件写入或随机参数搜索。策略不得修改输入。

warmup时输出0。引擎持有现金和现货，无杠杆/做空。在每次执行bar open，按当时净值求目标资产数量，成本若影响可买资金，使用引擎固定可行求解规则。持仓浮动使目标权重不恒定：每根按之前target重新调仓，而不是只在信号变更时调仓。

费用、滑点、数量精度、净值计算和年化由固定引擎提供；候选策略不得更改。未完成bar不传入该接口。提供插件代码、参数与依赖清单，不提交自己声称的回测结果代替固定工具。
