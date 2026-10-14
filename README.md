# lsl_apdf —— OpenSignals LSL 实时信号导出 + APDF 曲线插件

依据 `OpenSignals_LSL使用说明.md` 实现的插件：从 **OpenSignals（bitalino / biosignalsplux）**
发布的 LSL 流**实时**接收 EMG 原始数据，做偏移去除 / 带通滤波 / 增量 RMS，按
**%MVE 归一化**后实时绘制并导出 **APDF（幅值概率分布函数）** 曲线。

* 实时导出：原始信号、RMS/%MVE 负荷、APDF 曲线与分位点，全部边采边落盘；
* 强制 %MVE 标定：静息基线 + 多次 MVC，自动取稳定平台作为参考值；
* 实时界面：波形 + 负荷序列 + APDF 曲线 + 标定引导（Tkinter，无额外依赖）；
* 可嵌入：核心是一条 `AcquisitionSession` 流水线，可 import 进你自己的管道。

---

## 1. 安装

```bash
pip install -r requirements.txt        # numpy scipy pylsl matplotlib
```

Tkinter 随 Python 自带（Windows 官方安装包默认包含）。检查环境是否齐备：

```bash
python -m lsl_apdf selftest
```

自检会验证算法正确性（含**解析真值**对照）、滤波去 DC、MVC 标定精度、
以及 pylsl / Tkinter 是否可用。全部 PASS 再往下走。

---

## 2. 快速上手（不需要硬件）

终端 A 发布一个模拟 OpenSignals 流（`name=OpenSignals`、`type=设备MAC`，
与真实 OpenSignals 的命名约定一致）：

```bash
python -m lsl_apdf mock --profile calibration
```

终端 B 打开实时界面：

```bash
python -m lsl_apdf gui
```

在界面里选择流 → 「连接并开始」→「开始标定」→ 按提示做静息与 3 次最大用力
→ 标定完成后右侧即出现 static / median / peak 三个负荷水平与 APDF 曲线，
同时 `exports/session_*/` 里已经在实时写文件。

只想跑一遍看看结果、不开界面：

```bash
python -m lsl_apdf run --duration 120 --mac 00:07:80:AA:BB:CC
```

---

## 3. 接真实 OpenSignals

1. OpenSignals 里配好设备与 EMG 通道；
2. **Settings → INTEGRATIONS → 勾选 Lab Streaming Layer**；
3. 点主界面**红色录制按钮**开始采集 —— LSL 流随采集开始才发送。

然后查看当前可见的流：

```bash
python -m lsl_apdf list
```

```
[0] 名称=OpenSignals | type(MAC)=00:07:80:AA:BB:CC | 主机=MyPC | 通道数=1 | 采样率=1000 Hz
    按 MAC 定位：--mac '00:07:80:AA:BB:CC'   按主机定位：--host 'MyPC'
```

三种定位方式（对应说明文档 §3.2）：

| 方式 | 参数 | 适用场景 |
|------|------|---------|
| 按流名称 | `--stream-name OpenSignals`（默认） | 只有 1 台设备时最简 |
| 按 MAC | `--mac 00:07:80:AA:BB:CC` | 多设备时精确定位（OpenSignals 把 MAC 放在 `type` 字段） |
| 按主机名 | `--host MyPC` | 多台电脑提供流时 |

> **注意**：`list` 只显示短信息（名称/主机/通道数/采样率），**通道 label/unit
> 要连上之后才能读到**。这是 liblsl 协议本身的行为（解析服务不回传完整
> `<desc>`），不是 bug，插件会自动从 `inlet.info()` 补读。

---

## 4. APDF 曲线怎么读（重要，方向容易搞反）

APDF 给出"**幅值超过某一水平的时间占比 P**"（超概率）：

* **X 轴**：幅值水平（%MVE）
* **Y 轴**：超概率 P（%）

三个代表性负荷水平按**幅值分布百分位**定义：

| 负荷水平 | 幅值分布百分位 | 对应的超概率 P | 曲线上的位置 | 含义 |
|---------|--------------|--------------|------------|------|
| `static` 静态 | 第 10 百分位 | **90%** | Y=90% 处（左端低幅值） | **低**水平，"几乎一直存在"的负荷 |
| `median` 中位 | 第 50 百分位 | 50% | Y=50% 处 | 中位负荷 |
| `peak` 峰值 | 第 90 百分位 | **10%** | Y=10% 处（右端高幅值） | **高**水平，偶尔出现的负荷 |

**所以 `peak > median > static`。** 静态负荷水平是那个"一直在"的低水平，
不是"最大"的水平 —— 这一点在文献里表述不统一时极易搞反，本插件以
**幅值分布百分位**为主口径，同时在 `summary.json` 里给出两者
（`static_level_pct` 与 `static_exceedance_pct`）以便核对。

依据：*"Reporting the APDF of the EMG signals provides low-level (10th
percentile), median (50th percentile), and peak (90th percentile)"*
（[Swinburne APDF 报告](https://figshare.swinburne.edu.au/ndownloader/files/47665777)、
[CDC EMG APDF levels](https://stacks.cdc.gov/view/cdc/188399/cdc_188399_DS1.pdf)）。

**单位换算不影响 APDF**：%MVE 是两个同单位量之比，所以 ADC→mV 的线性换算
（`--units`）乃至整段信号乘常数，都不改变曲线形状，只影响日志与 CSV 里的
绝对幅值。

---

## 5. %MVE 标定流程

APDF 的 Y 轴是相对值，没有 MVC 参考值就无法跨人、跨电极位置比较，
所以本插件**强制**先标定（或显式给出已知的 MVE 参考值）。

```
静息(REST) → MVC 试次 1 → 休息 → MVC 试次 2 → 休息 → ... → DONE
```

* **静息**：估计基线噪声（中位数，稳健）并做信噪比检查；
* **MVC**：每次用力保持数秒，取该试次内**长度为 `plateau_sec` 的连续最强
  平台均值**作为该试次结果（比取瞬时峰值稳健，不会被一次电极抖动污染）；
* **试次间休息**：避免叠加疲劳把参考值压低；
* **结果**：取最强试次（可选"达最强值 90% 以上的试次数值平均"）作为 MVE；
  并检查 SNR 与试次一致性（CV），不达标会给出警告。

时间推进用的是**数据的 LSL 时间戳**而非墙钟：流一停，标定进度就停，
不会出现"人还没使劲、阶段已经跑完"的错位。

标定结果可存下来复用：

```bash
python -m lsl_apdf calibrate --save mve.json     # 只标定
python -m lsl_apdf run --mve-file mve.json --duration 300
```

或在配置里直接给定时用：`--mve 1.23`（单位与信号一致）。
标定段默认**不计入**任务 APDF（`include_calibration_in_apdf=False`），
因为用力本身不是工作任务，混进去会显著抬高静态/峰值水平。

---

## 6. 命令行参考

```
python -m lsl_apdf <命令> [参数]
```

| 命令 | 作用 |
|------|------|
| `gui` | 打开实时界面（不加参数时的默认命令） |
| `list` | 列出可见的 LSL 流（排查"找不到流"的第一步） |
| `mock` | 发布模拟 OpenSignals 流（无硬件验证） |
| `run` | 无界面采集：标定 + 采集 + 实时导出 + 打印 APDF |
| `calibrate` | 只做标定并保存 MVE 参考值 |
| `apdf <raw.csv>` | 对已录制的 CSV 离线重算 APDF（可出 PNG） |
| `selftest` | 端到端自检（算法 + 环境） |

常用通用参数：`--channel` `--units` `--rms-window` `--rms-hop` `--out`
`--no-export` `--subject` `--config cfg.json` `--set signal.highpass_hz=10`。

常用参数（数据源）：`--stream-name` `--mac` `--host` `--resolve-timeout`
`--synthetic` `--replay file.csv` `--fast` `--profile work|calibration`。

`--units` 可选 `raw` / `bitalino_emg` / `biosignalsplux_emg`（见 `units.py`，
只影响绝对幅值的显示，不影响 %MVE）。

---

## 7. 导出文件

每次会话建立一个目录 `exports/session_YYYYmmdd_HHMMSS[_被试]/`：

| 文件 | 内容 | 列 |
|------|------|----|
| `raw.csv` | **实时导出的原始信号**（全通道） | `lsl_time, local_time, ch1, ch2, ...` |
| `rms.csv` | 每个 RMS 窗口的幅值与负荷 | `lsl_time, rms, level_pct_mve` |
| `apdf_curve.csv` | 最新 APDF 曲线快照 | 上半：`probability_pct, level_pct_mve`；下半：`level_pct_mve, exceedance_prob, ...` |
| `percentiles.csv` | 三个负荷水平随时间变化 | `local_time, n_windows, total_sec, mve_ref, static_pct, median_pct, peak_pct, mean_pct, max_pct` |
| `calibration.json` | 标定结果（MVE、各试次、警告） | — |
| `config.json` | 本次运行的全部参数（可复现） | — |
| `summary.json` | 结束时汇总（时长/样本数/APDF 摘要/QC/文件清单） | — |
| `events.log` | 人类可读运行日志 | — |

时间列一律用定点 `%.6f` 写出。**LSL 时间戳是"开机以来秒数"（量级 1e6），
若用 `%.6g` 这类有效数字格式会把小数部分全部抹掉**（`863672.077137` →
`863672`），离线重算连采样率都推不出来 —— 这一点有专门的回归测试守着。

离线重算：

```bash
python -m lsl_apdf apdf exports/session_x/raw.csv --mve-file mve.json --plot
```

离线路径默认用零相位 `filtfilt`（`--causal` 可切到与实时完全一致的因果滤波）。
实测两条路径的 static/median/peak 差异在 1% 以内。

---

## 8. 作为插件嵌入你自己的管道

```python
from lsl_apdf import AppConfig, AcquisitionSession, LSLSource

cfg = AppConfig()
cfg.mve_ref = 1.23                       # 或用 session.start_calibration() 标定
session = AcquisitionSession(cfg, LSLSource(cfg.stream))
session.open()

# 每次 APDF 更新都会被回调（下游做疲劳判定 / 切换灵敏度等）
session.add_apdf_callback(lambda r: print(r.static_level, r.median_level, r.peak_level))

session.run(duration=300)                # 或自己在循环里调 session.step()
```

* **只要原始样本**：`session.snapshot().raw_y` / `raw_t`（只保留显示窗），
  或读导出的 `raw.csv`；想完全不落盘就设 `cfg.export.enabled = False`。
* **要处理结果**：`snapshot().apdf` 是 `ApdfResult`，含曲线数组与分位点，
  `snapshot().latest_level_pct` 是当前瞬间的 %MVE 负荷。
* **把结果再喂给别的程序**：`cfg.export.publish_processed = True` 会把
  `level_pct_mve / static / median / peak` 发布成一个新的 LSL 流
  （默认名 `OpenSignals_APDF`），下游用 `resolve_stream` 订阅即可，
  不必自己重算。

无界面/离线分析只用到 `ApdfAccumulator` 与 `compute_apdf` 也可以：

```python
from lsl_apdf import compute_apdf
result = compute_apdf(rms_values, mve_ref=1.23, window_sec=0.125, hop_sec=0.0625)
print(result.static_level, result.median_level, result.peak_level)
```

---

## 9. 模块结构

| 模块 | 职责 |
|------|------|
| `config.py` | 全部可序列化配置（也保证结果可复现） |
| `units.py` | ADC → mV 换算预设（对 %MVE 无影响） |
| `dsp.py` | 滤波链设计、流式滤波、增量 RMS、可增长数组 |
| `apdf.py` | **APDF 核心算法**（超概率曲线 + 三个负荷水平） |
| `calibration.py` | %MVE 标定状态机（静息 + 多试次 MVC） |
| `sources.py` | 数据源：LSL inlet / 合成源 / 回放源 + 元数据解析 |
| `recording.py` | 实时导出（后台写盘线程）、处理后信号回发 LSL |
| `session.py` | 会话编排（GUI / CLI / 你的代码都调它） |
| `gui.py` | 实时界面 |
| `cli.py` | 命令行 |
| `mock_stream.py` | 模拟 OpenSignals 发布端 |
| `offline.py` / `plotting.py` | 离线重算 / 出图 |

几个刻意的设计选择：

* **写盘放独立线程**：采集线程只入队（非阻塞），磁盘 IO 绝不阻塞采集，
  否则 LSL inlet 会堆积甚至丢样本；队列满时丢弃并计数，不反过来卡采集。
* **APDF 更新按"数据时间"节流**，不按墙钟 —— 这样快速回放/离线跑批与
  实时行为完全一致（早期版本按墙钟节流，快跑时曲线几乎不更新）。
* **APDF 存原始单位幅值**：标定完成或事后修正 MVE 后无需重新采集，
  换个 `mve_ref` 立刻重算，标定前的数据也不浪费。
* **不写死采样率**：一律读 `inlet.info().nominal_srate()`，连标定的平台
  长度也是按实际 RMS 速率反推的（说明文档 §6 的要求）。

---

## 10. 常见问题

| 现象 | 原因 / 解决 |
|------|------------|
| `resolve_stream` 超时找不到流 | ① 未勾选 LSL 集成；② 未点录制按钮；③ 防火墙；先跑 `python -m lsl_apdf list`。同机运行通常无防火墙问题 |
| 列表里没有通道名，只有 `ch1` | 正常：解析服务只回传短信息，通道 label/unit 连接后才读得到（见 §3） |
| 数据是 ADC 原始值 | 说明文档 §6 已说明：LSL 发的是原始数据。用 `--units bitalino_emg` 换算得到 mV（**不影响 %MVE**） |
| 采样率不一致 | 插件一律读流声明的采样率；界面状态栏显示"实测采样率"可对照 |
| 界面显示"尚未标定"、APDF 为空 | APDF 需要 MVE 参考值：点「开始标定」，或 `--mve` / `--mve-file` / 手动填入 |
| 标定警告 SNR 偏低 | 电极接触不良、放大器设置不当，或静息段没放松；建议重新贴电极后重标 |
| 标定警告试次 CV 偏大 | 几次用力不一致，重新标定，注意试次间充分休息 |
| `peak` 比 `static` 小 | 不可能发生（有回归测试守着）。若见到请检查是否手动改过百分位配置 |
| 同进程内自建 outlet 收不到数据 | 实测本机（Windows 多网卡）liblsl **同进程自环收不到样本**，跨进程正常 —— 这也正是 OpenSignals 的真实用法。测试因此用子进程发布 |
| Windows 控制台中文乱码 | CLI 已强制 UTF-8 输出；若仍乱码可先 `chcp 65001` |
| 想少写点盘 | `--no-export`，或 `--set export.write_raw=false` 只留 RMS/APDF |

---

## 11. 测试

```bash
python -m unittest discover -s tests -t . -v     # 115 项
python -m lsl_apdf selftest                      # 带解析真值的端到端自检
```

测试覆盖：APDF 数学（含**方向性回归测试**，防止 static/peak 被对调）、
尺度不变性、滤波去 DC/陷波/流式与批量一致性、跨块 RMS 连续性、
标定状态机与警告、配置往返、导出文件格式与**时间戳精度**、
离线重算与在线结果一致性、真实 LSL 跨进程往返（子进程发布端）、
GUI 建窗与刷新。

LSL 相关测试若在受限网络环境下发现不了流会自动跳过（并说明原因）；
解析到流却收不到数据则判定**失败**。

---

## 12. 局限与后续可扩展

* 目前只用**单通道**做 APDF（多通道可分别建会话，或用 `--channel` 逐个跑）；
* 未实现伪迹剔除（电极抖动尖峰会抬高 `peak`）—— 可在 `session._ingest_rms`
  前加一步幅值/斜率门限；
* 疲劳指标（中位频率 MDF / 平均功率频率 MPF 斜率）不在本插件范围内，
  但 `session.add_apdf_callback` 就是为这类下游判定预留的接入点；
* 未做长时（数小时）滚动的分段 APDF 报告，目前 `percentiles.csv` 给出的是
  累积曲线随时间的变化。

---

## License

MIT — see [LICENSE](LICENSE).
