# BOM 采购台（bom-procurement）

上传硬件 BOM → 规则引擎锁定规格 → 本地会话在淘宝按规则比价、选 SKU、加入购物车。

**购物车是终点。** 结算、付款、地址、发票永远由你本人在淘宝完成。

> **合规声明**：本项目是个人自用的采购辅助工具。不代登录、不读取或导出 Cookie、不绕过验证码、不自动结算付款。
> 自动化访问可能违反目标网站的服务条款，使用风险自负；请自行确认合法合规后再使用。

---

## 它能做什么 / 不能做什么

| 能做 | 不能做 |
|---|---|
| 解析 CSV / XLSX 格式 BOM，识别型号、封装、参数、数量 | 不代你登录、不保存或上传账号密码 |
| 按规则选型：容差分档、介电质（C0G/X7R）、精密档、连接器系列、尺寸与耐压 | 不绕过滑块/图片验证（撞到会停下来等你手动完成） |
| 淘宝搜索候选并按规则打分，挑最贴合的一家 | 不自动结算、付款、填地址、开发票 |
| 自动选 SKU、设置数量、加入购物车，并三态确认（页面提示 / 角标 / 加购接口） | 不保证一定能买到：无合格候选、限流、库存不足都会如实报告 |
| 断点续跑、撞验证原地等待、限流时自动降温 | 不做多账号、不做代理池、不做指纹伪装 |
| 批量结束可导出逐行结果 CSV（Excel 直接打开） | — |

## 截图

![选型台账](bom-site/docs/screenshot.png)

上传 BOM 后逐行给出采购口径、搜索词与必须核对的点。左栏是台账（行号、用料、封装、数量、状态），右栏是选中行的完整判断依据。截图用的是仓库内的示例 BOM，不含任何真实订单或价格。

---

## 快速开始（Windows）

### 1. 环境

- Windows 10/11
- Python 3.10+（开发环境实测 3.14）
- Microsoft Edge

```bash
pip install -r requirements.txt
```

### 2. 启动

```text
打开调试版Edge.bat     ← 第 1 步：独立 profile + CDP 9222
启动BOM选型台.bat      ← 第 2 步：自检 → 后台起服务 → 打开界面
```

三个 `.bat` 只是薄入口，逻辑都在 [`bom-site/launcher.ps1`](bom-site/launcher.ps1)：Python / 依赖 / Edge 自检、端口冲突检查、后台起服务、健康检查、CDP 就绪探测；失败会给出原因和日志路径。服务用 `pythonw.exe` 跑，不留黑框；启动窗口显示完结果会自动收起。横幅版本号读自根目录 `VERSION`（当前 v0.9.1）。

打开 <http://127.0.0.1:8765/>。

启动器支持参数（在 cmd 里带参数，或写进快捷方式的目标里）：

| 命令 | 作用 |
|---|---|
| `启动BOM选型台.bat status` | 只看运行状态，不启动任何东西 |
| `启动BOM选型台.bat diag` | 只读体检：Python / 依赖 / Edge / 端口 / PID / 日志 |
| `启动BOM选型台.bat restart` | 重启服务 |
| `启动BOM选型台.bat help` | 打印用法 |
| `停止BOM选型台.bat` | 停服务 + 关调试版 Edge（普通 Edge 不动） |

也可以手动起服务（等价，但没有自检）：

```bash
cd bom-site
python server.py            # 默认 127.0.0.1:8765
```

### 3. 首次使用

1. 上传 BOM（CSV / XLSX），确认解析结果与警告
2. 在「4 · 淘宝执行」点 **启动淘宝会话**（会自动附到调试版 Edge）
3. 若未登录，在浏览器窗口里手动登录淘宝；登录态保存在调试 profile 里，下次自动复用
4. 选一行 → **1 · 搜索候选** → 选一个 → **2 · 打开并匹配 SKU** → 确认数量 → **3 · 加入购物车**
5. 或直接点 **一键全部加购**，跑完看逐行结果

### 4. 关闭

- 网页右上角 **退出服务**：停批量、停会话、关浏览器、退进程（最常用）
- 或双击 `停止BOM选型台.bat`（备用）：只结束本项目的服务进程与带 `--remote-debugging-port=9222` 的 Edge，不碰你自己的普通浏览器窗口

---

## 规则是怎么定的

规则细节全部在 [`taobao-bom-procurement/SKILL.md`](taobao-bom-procurement/SKILL.md)，核心几条：

| 规则 | 作用 |
|---|---|
| 硬门槛（封装、针数、间距、容值/阻值、耐压、沟道…） | 不一致直接否决，不靠总分侥幸通过 |
| 容差分档 | 宽于要求按倍数降分；精密档（≤0.5%）超 3 倍直接否决 |
| 介电质门禁 | 要 C0G/NP0 却给 X7R/X5R → 否决；未标注 → 降权 + 人工核对提示 |
| 连接器系列 | XH ≠ KF2510 ≠ PH；KF128 与 2EDG 视为同族 |
| 尺寸 / 体积 | 电解电容、电感按封装和 MPN 提取尺寸，超 1.35 倍否决（参数对但装不上） |
| 加购确认 | 页面提示 / 角标 +1 / **加购接口 2xx** 三选一；都没有就查购物车定夺 |
| 收尾对账 | 跑完把每个 ok 行跟购物车实际行比对，找不到就降级并记录 |

## 目录结构

```
bom-site/                     本地网站与调度
  launcher.ps1                启动 / 停止 / 状态 / 体检 / 调试 Edge 的统一入口
  server.py                   HTTP 服务（127.0.0.1:8765）
  engine.py                   BOM 解析 + 选型规则 + 候选打分
  taobao_runner.py            批量调度、SKU 决策、加购确认、对账
  window_pos.py               调试浏览器窗口显隐（跑批时不抢前台）
  static/                     前端（无框架）
  test_*.py                   542 项断言（8 套纯 Python 测试）
  samples/                    示例 BOM / 候选
Taobao-Search-Skill/scripts/  淘宝执行层（Playwright over CDP）
taobao-bom-procurement/       决策层 SKILL 文档
*.bat                         三个 Windows 入口（UTF-8 / CRLF）
VERSION                       当前版本号
```

## 测试

```bash
cd bom-site
python test_engine.py                 # 176 项：解析与规则
python test_sku_selection.py            #  73 项：SKU 选择与门禁
python test_runner.py                   # 148 项：调度、对账、进度、续跑
python test_verification_recovery.py    #  27 项：验证/风控恢复
python test_window_focus.py            #  58 项：窗口显隐与不抢前台
python test_click_guard.py              #  14 项：点击三守卫
python test_cart_cleanup.py             #  24 项：精准清车不误删
python test_integration.py             #  22 项：假会话端到端（批量→对账→删车；HTTP 路由/同源/导出）
```

以上 8 套共 542 项断言，纯 Python、不需要浏览器；涉及真实淘宝的路径需要调试版 Edge + 已登录会话。
另有 `python test_http.py`：需要服务**已经在 8765 跑着**，只做 HTTP 冒烟打印，不断言，故不进 CI。
GitHub Actions 在 Windows runner 上跑 8 套测试 + 启动器语法/体检（见 `.github/workflows/tests.yml`）。

## 常见问题

| 现象 | 处理 |
|---|---|
| 撞到滑块/图片验证 | 页面顶部会出现提示条，批量原地等（默认 30 分钟），**不会把淘宝窗口突然弹到前台**；想看窗口点「显示淘宝窗口」，过完验证点「验证已完成，重新检测」 |
| 双击 bat 后中文乱码 | 脚本已固定 `chcp 65001` + UTF-8；若仍乱码，确认按 `.gitattributes` 检出（`.bat` 为 CRLF），别用记事本另存为 ANSI |
| 启动器说 8765 被占用 | 先跑 `启动BOM选型台.bat diag` 看是谁占着；多半是上次没退干净，双击 `停止BOM选型台.bat` 再启动 |
| 提示缺少 playwright / openpyxl | `pip install -r requirements.txt` |
| 9222 被普通 Edge 占了 / CDP 不通 | 关掉所有 Edge 窗口再双击 `打开调试版Edge.bat`；脚本会先探测 CDP，已就绪就不重复启动 |
| 服务起不来，窗口一闪而过 | 窗口不会自动关（失败会 pause），看报错；或跑 `diag` 查 `bom-site/runs/server.err.log` |
| 加购频繁「无回执」 | 多为限流前兆，程序会自动降温；可调 `TAOBAO_LINE_GAP`、`TAOBAO_COOL_*` 环境变量放慢 |
| 浏览器被关掉 | 网页里点「启动淘宝会话」会自动重开；窗口默认藏在屏幕外，需要点「显示淘宝窗口」 |
| 解析少行 | 多工作表 XLSX 会自动合并（表头不一致的页会在警告里点名）；若仍少行，检查表头是否被识别 |
| 批量跑完有几行显示「跳过」 | 型号/名称/参数都空的行没有搜索词，跑也搜不到东西，会直接跳过并在行上写明缺什么；补好 BOM 再跑 |
| 购物车越跑越多 | 用「购物车维护」按关键字删除，或跑批前清空；开跑前会自动看件数，≥190 直接不启动（淘宝上限约 200 行） |

## 可调参数（环境变量）

| 变量 | 默认 | 作用 |
|---|---|---|
| `TAOBAO_LINE_GAP` | 1.2 | 行间隔秒数，越大越慢越稳 |
| `TAOBAO_COOLDOWN_EVERY` / `TAOBAO_COOLDOWN_SEC` | 20 / 40 | 每 N 行冷却 M 秒 |
| `TAOBAO_UNC_TRIGGER` / `TAOBAO_UNC_COOL` / `TAOBAO_UNC_COOL_MAX` | 2 / 60 / 300 | 连续无回执时的自适应降温 |
| `TAOBAO_CART_WARN` / `TAOBAO_CART_STOP` | 150 / 190 | 购物车件数提醒 / 拒跑阈值 |
| `BOM_KEEP_DAYS` | 14 | runs/ 里日志与截图的保留天数（0 = 不清理） |

## License

[MIT](LICENSE) © 2026 zyx781

> 仅限个人学习与自用采购辅助；请遵守目标网站服务条款，风险自负。
