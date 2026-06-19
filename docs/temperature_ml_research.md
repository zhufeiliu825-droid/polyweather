# 温度预测机器学习研究与落地方案

_研究日期：2026-06-19。目标是为 Polyweather 的日最高温市场提供可回测、概率化、不会信息泄漏的模型输入。_

## 1. 结论

可以训练，而且用户提出的类比在技术上成立：把最近一段时间的温度、风、雨、云、气压等看作一个
weather state，模型根据过去状态和外生条件预测未来状态；预测结果又可以作为下一步输入，形成自回归
序列。GraphCast 就是这种思路的大规模版本：输入当前和 6 小时前的全球天气状态，预测 6 小时后的
状态，再将预测递归送回模型。

但 Polyweather 不应从“只用本站历史观测预测下一条温度”开始。第一版应训练：

1. **Open-Meteo/NWP 相对机场 METAR 的未来残差**，而不是从零重建天气系统。
2. **多个提前量的概率分布**，而不是只输出一个点值。
3. **当日最高温和整数桶概率**，因为这是交易真正结算的目标。

推荐的生产公式是：

```text
最终预测 = 当时真实可见的 NWP 预测 + ML 预测的站点/天气型/提前量残差
```

这就是气象业务长期使用的 MOS（Model Output Statistics）思路。NOAA 对 MOS 的定义也是：用历史观测
和其他模型数据，统计后处理 NWP 输出，以改善预报。相比纯本站自回归，这条路线保留了 NWP 对上游天气
系统、锋面移动和大尺度环流的认识，同时让我们的数据学习机场微气候及模型偏差。

## 2. 当前数据盘点

截至 2026-06-19，本地已有：

| 数据 | 数量 | 实际覆盖 | 判断 |
|---|---:|---|---|
| `data/*_obs.jsonl` | 32 站、4,372 条 METAR | EGLC 约 20 天；多数城市约 2-3 天 | 特征很有用，但远不够训练深度序列模型 |
| `data/forecast_residuals.jsonl` | 960 条 | 32 城 × 最近 30 个已结算日 | 可做短期校准，不足以分城市、季节、regime 训练 |
| `data/forecast_snapshots/` | 110 文件、2,404 条快照 | 主要为 2026-06-16 至 2026-06-19 | 已开始积累真实 issue-time 轨迹，但时间太短 |

现有 METAR 特征已经覆盖一个不错的训练基础：温度、露点、相对湿度、风向/风速/阵风、风矢量、云量、
云底、降水、雷暴、雾、能见度和 QNH。离线还可以补太阳高度、距太阳正午时间、季节和 Open-Meteo
曲线。数据结构不需要推倒重来，主要缺的是**历史长度**和**当时发布的预报版本**。

4,372 条观测只够做管线验证和极简单基线。模型很容易学会“下午比凌晨热”这种日周期，然后在随机切分
测试中看起来很准，却没有真正学会天气变化。若回补 5 年、32 站的逐小时数据，约有 140 万个站点小时；
若保留半小时 METAR，规模还会翻倍。这足够训练一个跨站点的树模型或小型时序网络，但仍不是训练
GraphCast 这种全球天气基础模型的规模。

## 3. 不应该只预测“下一个时间点”

下一时点温度当然能预测，而且通常看起来非常准，因为 30-60 分钟温度有很强持续性。但这不是交易目标：

- 预测 13:00 的温度很准，不代表知道 16:00 是否会因云层破裂再升 2°C。
- 每一步的小偏差自回归累计后会漂移。
- 本站序列看不到尚未抵达的锋面；NWP、邻站和压力层数据才有这类空间信息。
- 市场结算的是当日最大整数温度桶，点预测不能表达相邻桶的不确定性。

因此第一版同时设两个任务：

```text
任务 A：在 h = 0.5/1/2/3/6/12/24 小时，预测
        actual_temp(t+h) - nwp_temp(issue=t, valid=t+h)

任务 B：预测
        actual_daily_max - nwp_daily_max(issue=t, target_day=d)
        并输出每日最大值的 p10/p25/p50/p75/p90
```

任务 B 的分布可以直接积分成 `P(max rounds to N)`、`P(<=N)` 和 `P(>=N)`，再喂给 fair-value 与下单
逻辑。任务 A 则用于判断当前升温路径是否偏离预报、峰值是否已经出现。

## 4. 最关键的数据对齐问题

训练样本的每一列都必须满足：“在 issue time 当时已经可见”。否则会产生未来信息泄漏。

### 4.1 固定提前量

Open-Meteo 的 Historical Forecast API 是把连续模型运行的**最前几个小时拼接起来**，形成接近实况的
连续时间序列。它适合描述近年天气，也适合某些 ML 输入，但不能代表“前一天 18:00 对次日最高温的
真实预报”。

严格回测应使用：

- **Previous Runs API**：同一有效时刻的固定 1-7 天提前量，适合评估 lead-time skill。
- **Single Runs API**：保存一次模型初始化的完整预报轨迹，最适合重放当时的真实决策上下文。
- 我们自己的 `forecast_snapshots`：从 2026-06-16 起保存的 live issue-time 快照，今后是最可信的审计集。

现有 `maintain/build_residuals.py` 使用 Historical Forecast API。它仍可作为“接近分析时刻的空间/站点
偏差”参考，但不能把得到的误差分布解释成 12h、24h 或 48h live forecast 的严格误差。新模型训练和
模型晋级必须使用固定 issue time / lead time 数据。

### 4.2 时间切分

禁止随机打散逐时行。应使用滚动时间回测，例如：

```text
train: 2021-2023 -> validate: 2024 Q1 -> test: 2024 Q2
train: 2021-2024 Q1 -> validate: 2024 Q2 -> test: 2024 Q3
...
最终锁箱测试：最近完整的 6-12 个月
```

同一天的任何行都不能同时出现在训练集和测试集。模型选择后，锁箱期只能打开一次。

### 4.3 模型版本漂移

业务 NWP 会升级，Open-Meteo 的 Best Match 也会随地点和时间选择不同模型。NOAA MOS 文档明确把
NWP 变化列为限制；Open-Meteo 也提示 Historical Forecast 不适合无限长的一致序列，因为模型版本会变。
训练表必须保留 `model_id`、`run_init_utc`、`lead_hours`，并按模型版本监控漂移，采用滚动重训。

## 5. 建议的训练表

一行代表一个 `(station, issue_time, valid_time, target_day, model_run)`，至少包含：

### 身份与时钟

`station_id`、经纬度、海拔、时区、`issue_utc`、`valid_utc`、`lead_hours`、local hour、day-of-year、
太阳高度、距太阳正午分钟数。

### 当时可见的观测序列

最近 1/3/6/12/24h 的温度、露点、RH、风 u/v、阵风、QNH、云量、降水、能见度，以及每项的斜率、
变化量、滚动极值；同时记录当日 running max 和距当前时刻的时间。

### 当时可见的预报

各 horizon 的 NWP 温度曲线、预测日最高温及峰值时间、云/雨/风/湿度/辐射/气压、模型身份、前几次
run 的 forecast revision、多个模型的 spread。不能只保存峰值点，整条未来曲线对判断云层和升温路径很重要。

### regime 与目标

`sunny/cloudy/rainy/windy/humid/mixed` 作为可解释标签和评估切片；模型仍应看到连续原始变量，不能让
人工 regime 丢掉信息。目标包括各 horizon 残差、当日最高温残差、最高温发生时间和整数桶。

## 6. 模型路线

### Phase 0：先把数据做对

1. 从 IEM/NOAA 历史 METAR 回补至少 2 年，目标 5 年。
2. 从 Open-Meteo Previous Runs/Single Runs 回补固定提前量预报。
3. 统一到 UTC issue/valid time，保留 station local date，生成无泄漏训练表。
4. 对缺测只加 missing flag 或采用训练集内确定的插补；不能用未来值回填过去。

### Phase 1：必须保留的基线

任何复杂模型都要同时击败：

1. persistence：未来温度等于当前温度；
2. 原始 Open-Meteo/NWP；
3. 当前 30 天 bias + regime sigma；
4. 线性 MOS/ElasticNet。

### Phase 2：首个候选生产模型

建议使用一个**跨 32 城共享的 gradient-boosted tree 残差模型**，而不是每城单独训练。它对当前这种
表格化、缺测不规则、样本仍有限的数据更合适，也能让首尔从其他城市的相似湿热/阴雨 regime 借样本。
输出多组 quantile，或均值加尺度参数。

首版不建议把 LLM 放进训练环。LLM 可以解释预测和检查异常，但数值映射应由可复现、可回测的模型完成。
Farmer 的 hourly LLM advice 可以继续作为独立软参考，不能成为标签，也不能掩盖 ML 回退机制。

### Phase 3：小型序列模型

数据达到数年、并且 Phase 2 基线稳定后，再比较 TCN/GRU/TFT 这类多步模型。输入过去 24-48h 的观测
和未来 NWP covariates，直接输出未来曲线及 daily max quantiles。只有在严格 out-of-time 测试上稳定
胜过树模型，才进入 shadow 模式。

不建议自己训练类似 GraphCast 的模型：我们的数据只覆盖少量站点和地面变量，缺少全球空间网格、压力层
和几十年的再分析资料。更合理的做法是消费 GraphCast/AIFS/IFS/GFS 等模型输出，再训练本站后处理。

## 7. 概率输出与交易指标

气温不是确定的。WeatherBench 2 强调概率预报，因为大气具有混沌性；EMOS 也直接用高斯预测分布校准
集合预报的偏差和离散度。我们的模型至少输出五个 quantile：

```text
p10, p25, p50, p75, p90
```

离线指标分三层：

| 层级 | 指标 |
|---|---|
| 温度点值 | MAE、RMSE，按 station/season/lead/regime 切片 |
| 概率分布 | pinball loss、CRPS、区间覆盖率、sharpness |
| 市场桶 | Brier、log loss、reliability curve、ECE、相邻桶混淆 |
| 最终交易 | 扣除 spread/slippage/fill 后的 EV、PnL、回撤、样本外桶命中校准 |

模型晋级条件应在训练前写死。例如：锁箱集总体 CRPS 比当前生产基线至少下降 5%，关键城市/regime 不出现
明显退化，桶概率可靠性更好；然后至少 30 天 shadow，只记录不下单。5% 是项目门槛建议，不是气象领域
通用定律，可以在第一轮基线结果后调整一次并锁定。

## 8. 与现有 Farmer/主 bot 的集成边界

模型应每小时或每次新 NWP run 计算一次，产出版本化建议：

```json
{
  "station": "RKSI",
  "target_day": "2026-06-20",
  "issue_utc": "...",
  "model_version": "temp-residual-v1",
  "nwp_peak_c": 27.1,
  "adjustment_p50_c": 3.8,
  "daily_max_quantiles_c": {"p10": 29.4, "p50": 30.9, "p90": 32.2},
  "bucket_probabilities": {"30": 0.24, "31": 0.31, "32": 0.21},
  "valid_until": "..."
}
```

Farmer 每 5 分钟复用该结果，和现有 hourly LLM cache 类似。安全规则保持确定性：模型过期、schema 错误、
当前 NWP 相对 issue-time 漂移过大、站点/regime 不匹配时，自动回退当前 residual 模型。模型只提供 fair
和不确定度，不直接控制订单状态机。

## 9. 推荐实施顺序与工作量

1. **数据审计/回补脚本**：约 2-4 天。先选 EGLC、RKSI、RJTT、CYYZ 四个不同气候城市做 2 年原型。
2. **无泄漏训练表 + 基线回测**：约 3-5 天。先证明固定提前量数据能完整重放。
3. **线性 MOS + boosted-tree quantile 模型**：约 3-5 天。
4. **概率落桶与 walk-forward 报告**：约 2-4 天。
5. **生产只读 shadow 接入**：约 2-3 天，运行至少 30 天后再决定是否参与 fair。

因此，这件事不是“等几年才开始”。历史数据可以立即回补，一到两周可以得到可信的第一轮离线答案；但
让它影响真金白银下单，必须经过固定提前量回测和 shadow 观察。

## 10. 研究来源

- GraphCast：输入两个相隔 6h 的全球状态、自回归预测、0.25° 网格、227 个变量、用 1979-2017 ERA5
  开发训练。[Science 论文预印本](https://arxiv.org/abs/2212.12794)
- WeatherBench 2：1-14 天数据驱动天气预报基准，强调 operational baseline、held-out time 和概率指标
  CRPS。[论文](https://arxiv.org/abs/2308.15560)
- NOAA MOS：用模型输出、历史观测和地理气候变量做统计后处理；同时明确指出高质量长期观测和 NWP
  版本变化是关键限制。[NOAA Model Output Statistics](https://vlab.noaa.gov/web/mdl/mos)
- EMOS：对集合预报做偏差校正并估计预测分布，使用 CRPS 拟合。
  [Monthly Weather Review 论文](https://doi.org/10.1175/MWR2904.1)
- Open-Meteo：Historical Forecast、Previous Runs、Single Runs 的适用场景、可用年份和模型版本限制。
  [Historical Forecast API 文档](https://open-meteo.com/en/docs/historical-forecast-api)
- 历史 METAR：仓库已通过 Iowa Environmental Mesonet ASOS archive 获取 routine/SPECI 原始报文；生产近期
  实况来自 NOAA Aviation Weather Center。训练数据需继续遵守各数据源许可和署名要求。

