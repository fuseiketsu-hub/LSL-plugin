# 上传 GitHub · 操作说明

## 一、已经替你做完的

| 项 | 状态 |
|----|------|
| 初始化 git 仓库 | ✅ |
| 清理**个人绝对路径** | ✅ 3 个配置里的 `C:\Users\<用户名>\Desktop\` → `..\` |
| 生成 `.gitignore` | ✅ 排除 `sessions/`、`__pycache__/`、`*.log`、`exports/` |
| 确认**无真实被试数据**入库 | ✅ `sessions/` 全部排除 |
| 确认**无真实设备 MAC** | ✅ 扫描 0 处（代码里的 MAC 是模拟用的 `00:07:80:AA:BB:CC`） |
| 第一次提交 | ✅ `c10f1df`，56 个文件，约 250 KiB |

## 二、我做不了的（需要你的凭据，我不应该替你输入）

```
创建 GitHub 仓库  —— 需要登录你的账号
git push         —— 需要你的凭据（token / SSH key / 浏览器授权）
```

## 三、你要做的四步

**1）先在 github.com 点 `New repository` 建一个空仓库**（**不要**勾选 Add README / .gitignore，否则推送会冲突）

**2）设置你的提交身份**（现在用的是占位值）
```powershell
cd C:\Users\<用户名>\Desktop\LSL
git config user.name  "你的GitHub用户名"
git config user.email "你的GitHub邮箱"
git commit --amend --reset-author --no-edit     # 把占位作者改成你
```

**3）关联远程仓库**（把 `<你的用户名>/<仓库名>` 换掉）
```powershell
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git branch -M main
```

**4）推送**（第一次会弹出浏览器授权，或让你填 token）
```powershell
git push -u origin main
```

## 四、推送前建议排除的内容（现在还在仓库里）

这几样跟软件本体无关，公开仓库里会显得杂乱，其中 PLUX 文档还有**版权问题**：

```powershell
# 你的组会材料脚本（会把仓库变成"杂项抽屉"）
git rm --cached make_groupmeeting_ppt.py （组会脚本） `
              （另一个组会脚本） make_system_diagram.py `
              make_filter_demo_figure.py
# PLUX 官方文档（第三方版权，建议改放官方链接）
git rm --cached "OpenSignals_LSL使用说明.md"
git commit -m "移除与软件无关的材料与第三方文档"
```
再把这几个名字加进 `.gitignore`。

> 如果组会脚本你也想留档，另开一个私有仓库更合适。

## 五、⚠️ 一个结构性问题：仓库里的软件**别人跑不起来**

`lsl_apdf/unified.py` 会导入**仓库外**的 DPI 插件：

```
配置里： "plugin_dir": "..\\插件\\plugin"
代码里： sys.path.insert(plugin_dir) → from emg_server import BalancePolicy
                                       import rawaccel_bridge
```

也就是说，**"采集→%MVC→APDF→写 DPI"这条完整链路依赖另一个目录**（在 `Desktop\插件\plugin`）。三个选项：

| 选项 | 做法 | 后果 |
|------|------|------|
| **A. 把插件一起放进仓库**（推荐） | 复制成 `plugin/`，配置改 `plugin_dir: "plugin"` | 仓库自包含、别人能跑；但那是你的另一份代码，你同意公开吗？ |
| **B. 只上传信号侧** | README 里写明"DPI 执行侧是另一个项目，需另行提供" | 仓库只做 APDF/%MVC，别人能跑一半 |
| **C. 保持现状** | 只在你本机能跑 | 公开仓库跑不起来，容易被 issue 问 |

**现在仓库里没有任何插件代码，所以别人 clone 后只能用到信号侧（采集/APDF/离线分析），界面里的 DPI 部分会因找不到模块而报错。**

## 六、还差一个许可证

公开仓库**没有 LICENSE 等于"保留所有权利"**，别人不能用也不敢引用。常见选择：

| 许可证 | 适用 |
|--------|------|
| **MIT** | 最宽松，学术代码常用，别人可自由使用（推荐） |
| **Apache-2.0** | 多一条专利授权，企业友好 |
| **GPL-3.0** | 要求衍生作品也开源 |
| 不放 | 别人不能合法使用 —— 不建议 |

需要我加哪个，说一声即可（我会写 `LICENSE` 并在 README 里注明）。

## 七、上传前的最后自查

```powershell
cd C:\Users\<用户名>\Desktop\LSL
git status                       # 应干净
git ls-files | Select-String sessions   # 应无输出（确认无会话数据）
python -m lsl_apdf selftest      # 应全部通过
```
