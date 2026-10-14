# 动态 DPI 插件（Raw Accel 灵敏度调度）

通过**动态改写 Raw Accel 的灵敏度参数（Sens Multiplier）**实现 DPI 随时间/肌电信号变化，
用于实验「動的マウス感度調整による操作負担分散効果の検証」
（研究交接文档 `研究内容总结_交接文档.md` §4.2 / §5 / §7.2.3 的对应实现）。

> 两档 DPI 只差 sens：鼠标硬件 DPI 固定 **800**，Raw Accel sens **1.0 ⇄ 1.5**
> （即 800 ⇄ 1200 DPI），**加速关闭（accel off）**。完全按实验条件设计。

---

## 1. 文档要求 → 实现对照

| 交接文档要求 | 实现 | 位置 |
|---|---|---|
| 时间驱动自动调度：块模式 800,800,1200,1200 × 30 分钟 | `--mode time`（会话A） | `scheduler.py` + `controller.py` |
| 5 分钟线性渐变（分步写入 writer） | ramp_sec=300，每秒一步逐步写驱动 | `scheduler.py` / `_ramp_apply` |
| 2 小时会话上限，自动复位 | session_cap_sec=7200 | `controller.py` |
| 对照条件 800 固定 2 小时（会话B） | `--mode fixed` | `controller.py` |
| 2 档手动切换 UI | GUI 手动面板 / `--mode manual` | `dpi_gui.py` |
| EMG 实时输入接口（external 模式） | TCP 服务器（自建协议 + OpenSignals 帧） | `emg_server.py` |
| 阈值触发模式：ECU 16% 升、FDS 20% 降（§5.3-1） | `--policy threshold`（hold/迟滞/最小切换间隔） | `emg_server.py` |
| 交叉点法（§5.3-1，模拟版） | `--policy crossover`（需基准实验校准 reference） | `emg_server.py` |
| **负荷交替法（v2）：按两块肌肉的高暴露区域谁更严重来反复切** | `--policy balance`（死区/确认/死时间/安全阀/事件日志） | `emg_server.py` |
| 感度时序列日志 `sens_log_*.csv`，可复现任意时刻有效增益 | 每步写入：目标/实际 sens、有效 DPI、EMG 值、原因 | `session_log.py` |
| 模拟阈值触发模式（可录屏演示，文档标注“仍待做”） | `--mode demo` + 内置模拟 EMG + 时间加速 + dry-run | `emg_server.py` / GUI |
| 与 EMG/任务日志同步时间戳 | 每行 unix_ms/iso + MARK 打标记 | `session_log.py` |

## 2. 文件结构

```
动态DPI插件/
├── main.py                  # 命令行入口（所有模式的启动/演示/自检）
├── dpi_gui.py               # 图形界面（Tkinter，无需第三方库）
├── controller.py            # 统一控制器（引擎 + 驱动桥 + 日志 + EMG 策略）
├── scheduler.py             # 时间驱动调度引擎（块 + 渐变 + 上限，纯逻辑）
├── emg_server.py            # EMG TCP 服务器 + 阈值/交叉点策略 + 模拟 EMG 源
├── rawaccel_bridge.py       # Raw Accel 驱动桥（1.7 writer.exe / 1.6 --write / 模拟后端）
├── rawaccel_template_v17.json  # Raw Accel 1.7 写入模板（Output DPI=800，noaccel）
├── session_log.py           # sens_log_*.csv 感度时序列日志
├── config.json              # 实验参数配置（默认即文档确定版参数）
├── rawaccel_template.json   # Raw Accel CLI 写入模板（按本机版本可替换）
├── tools/
│   ├── mock_emg_stream.py   # 独立模拟 EMG 数据流（演示阈值/交叉点切换）
│   └── plot_log.py          # 绘制 sens_log 的有效 DPI 时间序列
└── tests/
    └── test_scheduler.py    # 自检（35 项，python main.py --check）
```

## 3. 环境要求

- Windows 10/11
- Python 3.9+（**仅用标准库**，无需 pip 安装任何东西）
- 真实实验时需安装 [Raw Accel](https://github.com/RawAccelOfficial/rawaccel) 并加载驱动；
  **无硬件开发/演示时完全不需要**（自动退回模拟后端）

### Raw Accel 实验前检查清单（真实实验必读）
1. 安装 Raw Accel 并启动驱动（GUI 左下角显示已连接；本机为 1.7.0 版，
   装在 `C:\Users\15654\Desktop\RawAccel`，插件会自动探测该目录的 `writer.exe`）；
2. Windows「设置 → 蓝牙和其他设备 → 鼠标 → 其他鼠标设置 → 指针选项」：
   **取消勾选“提高指针精确度”**，指针速度置于第 6 格（中间）；
3. 鼠标厂商驱动中把硬件 DPI 设为 **800**（实验基准，文档 §5.1）；
4. Raw Accel GUI 中 **Acceleration 设为 noaccel**（插件每次写入都会强制 noaccel）；
5. 若 Raw Accel 装在别处：把 `config.json → rawaccel.install_dir` 改成实际路径；
6. **写入机制（与参考插件 files/ 一致）**：插件读取 Raw Accel 安装目录的
   `settings.json`（保留 devices 等所有原有字段），只更新 `Output DPI`
   与加速参数，**原地写回该文件**，再调用 `writer.exe <settings.json>`
   推给驱动。Sens Multiplier 映射（本版本 GUI 规则 = Output DPI ÷ 1000）：
   **800 档 → Output DPI 1000 → Sens 1.0；1200 档 → Output DPI 1500 → Sens 1.5**。
   配合鼠标硬件 DPI=800，有效 DPI = 800×sens = 800/1200。驱动有防作弊的
   1 秒变更延迟：连续写入需间隔 ≥1s，插件已内置最小间隔与失败重试。
7. 如需自定义模板（如改 profile 名）：运行 `python main.py --dump-template`
   复制本机 settings.json 后自行编辑，再把 `template_file_v17` 指向它。
   换版本后（如升级到 1.8）必须重新核对 settings.json 字段结构。

## 4. 快速开始（无硬件，先跑一遍）

```powershell
# 1) 自检
python main.py --check

# 2) 查看时间调度计划（会话A）
python main.py --show-plan

# 3) 加速演示时间驱动会话（60 倍速，2 小时 → 2 分钟，模拟模式不改真实驱动）
python main.py --mode time --dry-run --time-scale 60

# 4) 加速演示 EMG 阈值闭环切换（内置模拟 EMG：低DPI→ECU↑，高DPI→FDS↑）
python main.py --mode demo --policy threshold --dry-run --time-scale 60

# 5) 图形界面（英文）         python dpi_gui.py      或双击「启动图形界面.bat」
#    图形界面（日本語）       python dpi_gui_ja.py   或双击「启动日语界面.bat」
```

> 语言：GUI 与控制器消息支持 `en` / `ja` 两种语言。英语界面用 `dpi_gui.py`；
> 日语界面用 `dpi_gui_ja.py`（控制器以 `ui.lang=ja` 启动，日志消息与调度
> 计划也为日语）。CLI 默认英文；可在 `config.json → ui.lang` 调整控制器默认语言。

图形界面（精简版）功能：
- **① 实验时间窗口（可编辑，上升+下降一条曲线）**：四个字段——
  **会话时长**、**上升开始时间**、**下降开始时间**、**变化区间（渐变时长）**。
  一条曲线：800 DPI → 在上升开始时间起线性渐变（变化区间）→ 1200 DPI →
  在下降开始时间起线性渐变（变化区间）→ 800 DPI 直到会话结束
  （对应 PPT8 第7页「上昇モード・下降モード」合并在同一条曲线）。
  校验：0 ≤ 上升开始 < 下降开始 < 会话时长；上升开始＋变化区间 ≤ 下降开始；
  下降开始＋变化区间 ≤ 会话时长。默认 120 / 30 / 90 / 5 分钟。
  会话从 800 DPI 立即开始，进度条与时间线图随输入自动调整。
- **② 选项**：模拟模式开关、时间加速。
- **③ 会话控制**：开始 / 停止（停止时自动复位 800 DPI）。
- **④ 手动切换**：800 / 1200 按钮**点击立即写入 Raw Accel**（无渐变）。
  **会话外（未开始会话）也直接生效**——不创建会话与日志，直接改驱动；
  会话运行中同样生效（覆盖到下一次调度切换，渐变起点自动衔接）。
- 右侧：大号有效 DPI、会话进度（含下次切换倒计时）、会话信息、
  有效 DPI 时间线图（块边界/渐变阴影/当前游标；「⟲ 复位」按钮 =
  **会话进度归零重来**：计时清零、曲线清空、调度从头开始）、
  深色运行日志。
- 若双击启动后无窗口，查看同目录 `gui_error.log` 中的报错。

打标记（GUI 已移除，CLI 仍可用 `python main.py --mark "备注"`）、
EMG %MVC 面板、会话模式/切换策略选择等已从 GUI 移除
（CLI 仍保留完整功能，见 `python main.py --mode emg` 等）。

演示结束后用 `tools/plot_log.py` 可视化日志：

```powershell
python tools/plot_log.py logs\sens_log_20260829_103000.csv
```

## 5. 实验运行手册

### 5.1 会话A —— 动态条件（时间驱动）
DPI 每 30 分钟一档：800, 800, 1200, 1200（共 2 小时），
800→1200 切换时前 5 分钟线性渐变（Krakauer 2000/2004 依据），每步写入驱动并记日志。

```powershell
python main.py --mode time                 # 真实驱动
python main.py --mode time --dry-run       # 只记录不写驱动（预演）
```
- 渐变放块首 5 分钟（lead_in，默认）还是块尾 5 分钟（lead_out）在
  `config.json → time_mode.ramp.placement` 切换（§5.3-2 切换窗口位置问题）。
- 会话满 2 小时自动复位 800 DPI 并结束；`Ctrl+C` 随时停止并复位。

### 5.2 会话B —— 对照条件（800 固定 2 小时）
```powershell
python main.py --mode fixed
```

### 5.3 手动两档
```powershell
python main.py --mode manual     # 输入 up/down/quit
```
或在 GUI 中点「切到 800 DPI / 切到 1200 DPI」，可选 5 分钟渐变。

### 5.4 EMG 驱动 —— 阈值法（文档 §5.3-1）
```powershell
python main.py --mode emg --policy threshold --port 9000
```
- 程序内置 TCP 服务器，等待 **%MVC（p50）** 数据；
- **ECU ≥ 16 %MVC**（持续 hold_sec=5s）→ 升到 1200 DPI（把负荷从臂部转移给手指）；
- **FDS ≥ 20 %MVC**（持续 5s）→ 降到 800 DPI；
- 防抖：最小切换间隔 min_dwell=60s、迟滞 hysteresis=2%MVC（须回落到阈值−2 才会再次触发同方向切换）；
- 阈值/时长全部在 `config.json → emg` 可调，正式实验的阈值取值待基准实验确定（§5.3-1）。

**数据协议（text，默认）**——每行一种：
```
{"EDC":11.2,"ECU":12.5,"FDS":13.1,"UT":6.0}     # JSON
EDC 11.2 ECU 12.5 FDS 13.1 UT 6.0               # 空格键值对
EDC=11.2,ECU=12.5                               # 逗号键值对（可只发部分肌肉）
```
- OpenSignals 直接输出（`emg.protocol="opensignals"`）：按 4 字节序号 +
  每通道 2 字节大端 ADC 解析，再用 `opensignals.adc_to_mvc` 线性换算；
  **帧格式随 OpenSignals 版本有差异，实验前必须用本机版本核对**
  （更稳妥的路线：先用自建 TCP 服务器把 EMG→%MVC 处理好再转发给本插件）。
- 无硬件演示：`python tools/mock_emg_stream.py --port 9000`（`--watch-log logs` 可形成闭环）。

### 5.5 EMG 驱动 —— 交叉点法（模拟版，§5.3-1）
```powershell
python main.py --mode emg --policy crossover
```
把当前实测 p50%MVC 与“另一档 DPI 的参考 %MVC”比较：对方档位在监控肌肉上
低 margin_mvc 以上且持续 → 切换。**`config.json → emg.crossover.reference`
目前是占位值，正式使用前必须先用基准实验（800/1200 各肌肉 %MVC）校准**；
这也正是文档 §5.3-1 所说“交叉点法须先跑基准实验”的原因。

### 5.6 负荷交替法（`--policy balance`，v2）

不再用"每 N 分钟切一次"的时间表，也不用固定的绝对阈值，而是**比较两块肌肉各自的高暴露
区域**，由两者的交替结果决定切换次数。

```powershell
python main.py --mode emg --policy balance --port 9000      # 真实（%MVC 由 Part 2 推来）
python main.py --mode demo --policy balance --dry-run --time-scale 60   # 无硬件演示
```

判定链：

```
高暴露区域   A_m(t) = Σ max(%MVC_m − high_thr, 0)·Δt   （最近 roll_window 内）
严重度       r_m(t) = A_m(t) / base_m                  （base = 会话最初 baseline_sec 的同一量）
差值         Δ(t) = r_ECU − r_FDS
```

* **交叉**：Δ 变号 → 记一次"交替"事件（`CROSSING`，无论幅度大小）；
* **执行**：|Δ| ≥ `deadband`（默认 0.15）且持续 `confirm_sec` → 切换：
  Δ>0（ECU 相对更重）→ 升档；Δ<0（FDS 相对更重）→ 降档；
* **死时间**：切换后等 `switch_ramp_sec`(300s) 渐变走完 + `settle_sec`(120s) 稳定，
  期间只记录不动作。这不是时间表，而是"上一次干预没生效之前不许再动"的物理下限；
* **抑制重复**：同方向命令发过而档位一直没变（说明上游没生效）→ 不再重复刷，
  记 `repeat_suppressed`，等档位真的变了或方向翻转再说；
* **两种模式**：两块肌肉的基线都 ≥ `floor_mvc` → 比**相对倍数**（ratio）；
  有一块基线≈0（低档下该肌肉本就不参与）→ 两块都退回比**绝对速率**（absolute，
  死区用 `deadband_abs_mvc`）。**绝不允许把"倍数"和"原始 %MVC"混着比**；
* **安全阀**：两块同时 > `safety_mvc`(40%MVC) → 强制降到低档；
* **会话上限**：到 `session_cap_sec` 自动收尾并复位 800 DPI（EMG/demo 模式同样生效）。

**切换次数完全由数据决定**：时钟流逝本身永远不会引起切换。三者关系：
`crossings` = 交替次数；`switches` ≤ `crossings` + 1（+1 是基线结束后面对明显失衡的
一次合理动作）。负荷全程均衡 → 两者都是 0，这是**"负荷本来就分散"的正常结果**。

方向映射保证这是负反馈，所以负荷会在两块肌肉间**轮流承担**；切换次数就是把
"轮流了几轮"量化下来的数。

**事件日志**（"记录下这个点"）：`logs/balance_events_<时间戳>.csv`（**按会话分文件**，
与 `sens_log_*.csv` 一致；追加到同一文件会让多次会话的 t_sim 重叠、事后无法区分），
每次交叉/动作/基线就绪各一行，含 `t_sim, event, rate_A/B, base_A/B, r_A/B, delta,
deadband, level, action, reason`，末尾一行 `SESSION_END` 带汇总（switches/crossings/
separation/saturated/suppressed/mode）。参数全部在 `config.json → emg.balance`。

### 5.6.1 与 EMG 插件（Part 2）联调

Part 3 只等 **%MVC**，不关心它从哪来。用 `lsl_apdf` 的 `stream-mvc` 一条命令即可推送：

```powershell
# 终端 A：DPI 插件（balance 策略，监听 9000）
python main.py --mode emg --policy balance --port 9000

# 终端 B：把每肌肉 %MVC 推过去（真实 LSL）
python -m lsl_apdf stream-mvc ^
    --channels "A1=ECU,A2=FDS,A3=UT,A4=EDC" ^
    --mvc "ECU=80,FDS=57,UT=83,EDC=59" --port 9000

# 想先演练整条链路（不需硬件，模拟"负荷在 ECU/FDS 间交替"的被试）：
python -m lsl_apdf stream-mvc --synthetic --profile balance ^
    --channels "A1=ECU,A2=FDS,A3=UT,A4=EDC" ^
    --mvc "ECU=1000,FDS=1000,UT=1000,EDC=1000" --port 9000
```

用 `config_linktest.json` 可以把 balance 的时间常数缩到几十秒，2 分钟跑完全流程演练：

```powershell
python main.py --mode emg --policy balance --config config_linktest.json
```

> `--mvc` 用**你现场实测的** MVC（每肌肉 2–3 次取最大 RMS），不要复用别人的值。

> **演示注意**：`--time-scale` 会同时压缩渐变时长。倍率过高时（如 ≥120），渐变可能
> 在下一步决策到来前还没走完，于是档位迟迟不变、日志里出现 `repeat_suppressed`。
> 这只是演示的时间压缩效应；正式实验用实时（`time_scale=1`），渐变 300 s 远短于
> 最小决策间隔（渐变 300 s + 稳定 120 s + 确认 30 s），不会发生。

### 5.7 打标记（与 EMG/任务日志时间戳对齐）
GUI 的「打标记」按钮，或会话进行中另开终端：
```powershell
python main.py --mark "Fitts任务块3开始"
```

## 6. 配置说明（config.json）

| 键 | 默认 | 说明 |
|---|---|---|
| mouse.base_dpi | 800 | 鼠标硬件 DPI（实验基准，§5.1） |
| mouse.levels | low=1.0 / high=1.5 | 两档 sens（800/1200），仅 sens 不同 |
| rawaccel.exe_path | "" | 留空自动查找；装在其他位置就填全路径 |
| rawaccel.dry_run | false | true=模拟模式（不写驱动，演示/开发用） |
| session_cap_sec | 7200 | 会话上限 2 小时 |
| time_mode.pattern_dpi | [800,800,1200,1200] | 块 DPI 模式（§5.2 确定版） |
| time_mode.block_sec | 1800 | 每块 30 分钟 |
| time_mode.ramp | sec=300, lead_in | 5 分钟线性渐变 |
| fixed_mode.dpi | 800 | 对照会话固定 DPI |
| manual_mode.ramp_sec | 300 | 手动切换渐变时长 |
| emg.thresholds | ECU≥16↑ / FDS≥20↓ | 阈值法触发（UT 默认关闭） |
| emg.balance | pair=ECU/FDS, deadband=0.15, confirm=30s, roll_window=300s | 负荷交替法参数（v2） |
| emg.hold_sec / min_dwell_sec / hysteresis_mvc | 5 / 60 / 2 | 持续确认 / 最小切换间隔 / 迟滞 |
| emg.crossover.reference | 占位值 | **交叉点法必须用基准实验校准** |
| time_scale | 1.0 | 时间加速倍率（仅演示用；1=实时） |

## 7. 日志格式（sens_log_*.csv）

| 列 | 含义 |
|---|---|
| unix_ms / iso | 时间戳（与 EMG/任务日志对齐） |
| elapsed_s | 会话内秒数（模拟时间） |
| event | SESSION_START / BLOCK / RAMP / TICK / MANUAL / EMG_SWITCH(_RAMP) / MARK / EMERGENCY_RESET / SESSION_END |
| target_dpi / target_sens | 当前目标（渐变期间为渐变终点） |
| applied_sens / effective_dpi | **实际写入值 / 有效 DPI**（可复现任意时刻有效增益） |
| emg_EDC…UT | 各肌肉 %MVC（p50） |
| reason | 事件原因（如 `emg_threshold_up(ECU=16.4%MVC>=16%)`） |

## 8. 已知限制与后续工作

1. **交叉点法 reference 未校准**：需要基准实验数据（见 §5.5）；
2. **OpenSignals 二进制帧解析为实验性**：版本相关，正式实验前核对/改用自建转发服务器；
3. **20%MVC 高负荷阈值**（§5.3-3）目前只用于事后解析（APDF 管道），
   本插件内阈值 16/20 是切换触发值，两者概念不同，勿混淆；
4. 时间驱动渐变窗口位置（lead_in/lead_out）与时长（3/5/10 分钟）的取舍，
   建议先用本插件的 `--time-scale` 加速模式 + 参数敏感性仿真（交接文档 §7.2.2）确定；
5. 若实验需要「按块自动播放任务提示」（Fitts/拖拽/菜单每 15 分钟块），
   可在 `controller.py` 的块边界事件处挂接提示音/提示窗口，后续扩展。

## 9. 常见问题

- **打开 Raw Accel GUI 发现数值"没变"**：旧版本插件写独立临时文件，GUI
  读不到；现已改为**原地更新安装目录 settings.json**——打开 GUI 时
  Sens Multiplier 会显示插件写入值（800 档→1.0、1200 档→1.5）。
  GUI 从不回读驱动、显示的是 settings.json 内容，所以"GUI 显示变化"=
  "参数确实被切换"。真正的鼠标行为验证：`python main.py --read-driver`
  回读驱动 Output DPI（800 档=1000、1200 档=1500，Sens 1.0/1.5），
  或用 DPI Analyzer 实测移动距离（比值 1.5）。
- **快速切换 800/1200 偶发"写入失败"**：驱动有防作弊的 1 秒变更延迟，
  连续写入需间隔 ≥1s；插件已内置最小间隔（1.1s）与自动重试，正常操作
  下不会失败。
- **插件没影响到 Raw Accel 参数**：先看界面顶栏/启动日志的「后端」：
  显示 `rawaccel_writer_v17` 才是真的在写驱动；若显示 `mock`，说明没找到
  Raw Accel——把 `config.json → rawaccel.install_dir` 填成实际安装目录
  （本机为 `C:\Users\15654\Desktop\RawAccel`）。若显示 `rawaccel_writer_v17`
  但顶栏变橙「⚠ 驱动写入失败」，说明驱动未加载（先打开 Raw Accel GUI 连接驱动）。
- **写入失败 rc=...**：驱动未加载或模板与版本不符 → 先 `--dump-template` 更新模板，
  再在 Raw Accel GUI 确认驱动已连接。
- **EMG 无反应**：确认发送的是 **%MVC** 数值（不是原始 ADC），端口一致，
  且策略要求持续 hold_sec 秒才切换（看 GUI 的 EMG 面板是否有数值进来）。
