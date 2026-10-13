# Strategy contract v0.2

Python入口 `generate_exposures(history: pandas.DataFrame, parameters: dict) -> list[float]`。

- `history` 按UTC时间升序，只包含已完成的bar。列：`bar_open_time`、`bar_close_time`、`available_at`（均为UTC时间），
  `open`、`high`、`low`、`close`、`volume`，以及数据身份列。不要修改输入。
- 返回长度等于 `len(history)` 的序列。第 t 个值是在第 t 根bar收盘时、只用第 0..t 行即可决定的**暴露提案**，
  范围 [0, 1]，不得为 NaN；暖启动期间返回 0。改变或截断未来的行不得改变之前的输出（前缀因果性），固定引擎会检查。
- 暴露提案经过研究授权中的组合政策变成目标权重。本套件的政策是 `policy-v1`：无波动率目标、单资产上限 1、
  总暴露上限 1、每根bar再平衡、无交易带，因此**目标权重 = 暴露提案**。
- 目标权重最早在下一根bar的open成交；引擎每根bar按当时净值重新调仓到目标权重（不是只在信号变化时调仓）。
  费用、滑点、数量精度、净值与年化由固定引擎提供，策略不得更改。
- 不能联网、写文件或做随机参数搜索；代码在无网络的独立容器中运行。
- 兼容：v0.1 入口 `generate_targets(bars: list[dict], params)`（`start` 为bar open的Unix秒）仍可运行，按同一政策处理。

通过研究工具 `submit_experiment` 提交 `/workspace/` 下的策略文件和参数；只有工具返回的结果才算已完成实验。
