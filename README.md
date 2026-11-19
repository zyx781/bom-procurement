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
| 同一份 BOM 断点续跑、撞验证原地等待、限流时自动降温 | 不做多账号、不做代理池、不做指纹伪装 |
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

**拿到发布包的人**：双击 `安装环境.bat`，它会建 `.venv`、装依赖、做自检，一步到位；
逐步说明见 [`使用说明.md`](使用说明.md)。

开发机上也可以手动装：

```bash
pip install -r requirements.txt
```

只想跑程序不需要别的。要跑测试和静态检查（`ruff` / `mypy`）再装一份开发依赖：

```bash
pip install -r requirements-dev.txt
```

### 2. 启动

```text
打开调试版Edge.bat     ← 第 1 步：独立 profile + CDP 9222
启动BOM选型台.bat      ← 第 2 步：自检 → 后台起服务 → 打开界面
```

启动器优先用根目录的 `.venv`（`安装环境.bat` 建的）；没有才去系统 PATH 里挑依赖齐全的 Python。

两个 `.bat` 只是薄入口，逻辑都在 [`bom-site/launcher.ps1`](bom-site/launcher.ps1)：Python / 依赖 / Edge 自检、端口冲突检查、后台起服务、健康检查、CDP 就绪探测；失败会给出原因和日志路径。服务用 `pythonw.exe` 跑，不留黑框；启动窗口显示完结果会自动收起。横幅版本号读自根目录 `VERSION`（当前 v0.13.2）。

打开 <http://127.0.0.1:8765/>。

启动器支持参数（在 cmd 里带参数，或写进快捷方式的目标里）：

| 命令 | 作用 |
|---|---|
| `启动BOM选型台.bat status` | 只看运行状态，不启动任何东西 |
| `启动BOM选型台.bat diag` | 只读体检：Python / 依赖 / Edge / 端口 / PID / 日志 |
| `启动BOM选型台.bat stop` | 停服务 + 关调试版 Edge（普通 Edge 不动） |
| `启动BOM选型台.bat restart` | 重启服务 |
| `启动BOM选型台.bat help` | 打印用法 |

也可以手动起服务（等价，但没有自检）：

```bash
cd bom-site
python server.py            # 默认 127.0.0.1:8765
```

### 2.1 配置与数据放在哪

本机配置是 `bom-site/settings.json`，**不进仓库**；仓库里只放模板 [`settings.example.json`](bom-site/settings.example.json)。没有 `settings.json` 时程序直接读模板，所以 clone 下来不复制也能跑；想改端口或节奏再自己建一份：

```bash
cd bom-site
cp settings.example.json settings.json
```

它控制调试端口（`cdp_url`）、跑批节奏（`pace` / `speed`）和每行金额上限（`max_line_cost`）。写坏了也不会起不来：解析失败会打一行警告并退回模板。

登录态和浏览器数据都在**项目目录里**，不写系统用户目录，也不随 git 走：

| 路径 | 什么时候产生 |
|---|---|
| `bom-site/.cdp-profile/` | 用调试版 Edge（配了 `cdp_url`）时的浏览器用户目录，登录 cookie 在 `Default/Network/Cookies` |
| `bom-site/.browser-profile/` | 不配 `cdp_url`、走内置 Playwright 时的用户目录，同样含 cookie |
| `Taobao-Search-Skill/scripts/.cache/` | 绕过网页直接跑执行层命令时的默认 profile |

三处都在 `.gitignore` 里。**分享项目时只给代码，别把这两个 profile 目录一起拷走** —— 里面的 cookie 等同于登录凭证。需要换位置（比如放加密盘）可以用环境变量 `TAOBAO_PROFILE_DIR`，默认值就是项目内的 `.browser-profile`。

### 2.1 搜索记录不写进浏览器历史

用调试版 Edge（配了 `cdp_url`）跑批时，执行层会**另开一个无痕窗口**做所有搜索和加购：这些页面地址只存在于内存，窗口一关就没了，**不会出现在 Edge 的「历史记录」里**。你日常在那个 Edge 里上网留下的记录（bing / github 等）完全不受影响。

- 登录态照常保留：启动时从调试 profile 注入 cookie，退出时把刷新过的登录 cookie 写回 profile 和 `taobao-session.json`，不用重新扫码。
- 需要人工过验证 / 登录时，弹出的窗口是那个无痕工作窗口，操作方式不变。
- 想关掉无痕、退回旧行为（历史会写盘）：设环境变量 `TAOBAO_PRIVATE_WINDOW=0`。

> 无痕只清「本机浏览器历史」。淘宝账号自己记的「搜索历史 / 我的足迹」是网站存在服务器上的，浏览器这层管不到——要清得去淘宝 App/网页里删。另外那些**已经**产生的旧记录不会被这次改动追溯删除，需要的话手动在 Edge 历史里按 `s.taobao.com` 搜一下删掉即可。

### 2.2 自动过滑块（可选）

装上依赖就启用：撞验证时先自动拖滑块，拖不过才刷新，刷新也失败才叫你。

```bash
pip install -r requirements-optional.txt   # ddddocr + opencv-python
```

不装也能跑，只是滑块一律走「刷新自恢复 → 人工」。两种滑块都认：

| 形态 | 判定 | 怎么解 |
|---|---|---|
| 拼图（geetest） | 有背景图 + 拖动按钮 | ddddocr 找缺口 → 轮廓检测兜底 → 贝塞尔轨迹拖动 |
| 阿里 nc（`#nc_1_n1z`） | 只有拖动按钮 | 量轨道宽度，拖到最右侧 |

三个旋钮，一般在调风控节奏时才动：

| 变量 | 默认 | 作用 |
|---|---|---|
| `TAOBAO_SLIDER_SOLVE` | `1` | 设 `0` 关掉自动过滑块 |
| `TAOBAO_SLIDER_TRIES` | `2` | 单次撞验证最多拖几次（失败会换一张图再试） |
| `TAOBAO_SLIDER_MAX_PER_SESSION` | `30` | 一个会话的总次数上限，防止死循环把风控逼急 |

> 自动过验证属于绕过站点的人机校验，**有被加严风控的风险**。跑批时如果发现滑块出现频率明显上升，先调慢 `speed`（`settings.json`），别一味加次数。

### 3. 首次使用

1. 上传 BOM（CSV / XLSX），确认解析结果与警告
2. 在「4 · 淘宝执行」点 **启动淘宝会话**（会自动附到调试版 Edge）
3. 若未登录，在浏览器窗口里手动登录淘宝；登录态保存在调试 profile 里，下次自动复用
4. 选一行 → **1 · 搜索候选** → 选一个 → **2 · 打开并匹配 SKU** → 确认数量 → **3 · 加入购物车**
5. 或直接点 **一键全部加购**，跑完看逐行结果

### 4. 界面怎么读

页面只有一块工作台，不再是「投放 / 台账 / 打分 / 执行」四段往下堆：

| 区域 | 作用 |
|---|---|
| 投放条 | 没上传时展开；有清单后压成一行（`清单名 · 6 行 · 5 可执行`），点标题随时换一份 |
| 状态条 | 四个可点的筛子：**全部 / 可执行 / 已加购 / 待处理**，右边是批量按钮 |
| 进度轨 | 只在跑批时出现：`已处理/总数` + 当前在跑哪一行 |
| 台账 | 一行 BOM 一行，**选型**和**加购**是两列状态；加购列就写着 `已加购 / 失败 / 待核对 / 未跑` |
| 抽屉 | 点任意一行打开：详情（搜索词、硬门槛、必须核对、风险）/ 单行执行 / 维护（购物车、日志） |

待确认的行有两种处理方式，都在详情抽屉里：

- **人工放行**：点「我已人工核对，标记为可执行」。适合引擎拿不准但你知道没问题的行（比如立创编号已经核准了规格）。放行只改这一行的判定，不动引擎规则；刷新/重启后依然生效（落盘到 `last-analysis.json` / `batch-lines.json`）。
- **改搜索词**：搜索词每条都是输入框，改完点「保存搜索词」，最多 4 条。适合型号写法特殊、想让跑批换个词搜的行。

**只跑这一行**：详情抽屉底部的「只跑这一行」（或「单行执行」页签里的「自动跑完这一行」）会按批量同一套逻辑跑完当前行（搜索→评分选优→匹配 SKU→加购），只落这一行的进度。适合批量里个别行失败（比如换词后想重试）——整批重跑会把已成功的行再加一遍车。前置条件：淘宝会话已启动、没有批量任务在跑；标了「不买」的行会拒绝。

两条设计约束写在代码里：

- **批次只写进属于它的那张表**：拿当前 BOM 的行数和服务端 `progress.total` 对，不一致就不往行上写状态，
  只在进度轨上写「上一批（另一份清单）的记录」。否则上一批 18 行会把这份 6 行的 1–6 行全标成「已删车」。
- **一屏看全**：6 行 BOM 的页面高度约 700px（旧版是 2981px，要点开三屏才看完），表头吸顶。

### 4.1 出错了怎么看

每一行失败都会带上四个分类字段（界面悬停可见，也进进度文件和导出 CSV）：

| 字段 | 取值 | 意思 |
|---|---|---|
| `error_code` | `COMMAND_TIMEOUT` / `VERIFICATION_REQUIRED` / `SKU_NOT_SELECTED` … | 稳定的短码，日志和导出里可搜 |
| `error_kind` | `input` / `network` / `page` / `auth` / `internal` | 数据问题 / 网络 / 页面 / 验证 / 程序异常 |
| `error_retryable` | true / false | 原样再跑一次有没有意义 |
| `error_needs_human` | true / false | 是否必须人工介入（登录、验证码、页面改版） |

界面上的失败行会先显示「需人工·原因」或「可重试·原因」；日志里每条异常是一行：

```text
[error] code=VERIFICATION_REQUIRED kind=auth line=7 stage=batch-line retryable=0 human=1 msg='需要人工完成验证'
```

认不出来的异常一律算 `internal` 并附 `[error/tb]` traceback —— 程序缺陷不会被伪装成
「网页波动」重试掉。日志和错误信息在落盘前会抹掉 `cookie` / `token` / `x5secdata`
这类键值。

**诊断流水**：每次搜索/开页/加购都会往 `runs/diagnostics-YYYY-MM-DD.jsonl` 追加一行
结构化事件（搜索词、结果条数、SKU 组数、加购回执来源、错误码），不记页面全文。
按行号看：

```bash
curl "http://127.0.0.1:8765/api/diagnostics?line=7&limit=80"
```

返回里带 `summary.structure_suspect`：当**所有搜索词都是 0 结果**或出现
`SELECTOR_MISS`/`OPEN_FAILED` 时置 true，并在行结果上标 `structure_suspect` ——
这时先怀疑页面改版/风控，别急着改选型规则。诊断文件按 `BOM_KEEP_DAYS`（默认 14 天）清理。

### 5. 关闭

- 网页右上角 **退出服务**：停批量、停会话、关浏览器、退进程（最常用）
- 或 `启动BOM选型台.bat stop`（备用）：只结束本项目的服务进程与带 `--remote-debugging-port=9222` 的 Edge，不碰你自己的普通浏览器窗口

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
| 收尾对账 | 跑完把每个 ok 行跟购物车实际行比对（按料号/封装/阻容值等规格原子，不看字符串相似度），找不到就降级并记录；整批都对不上时判「读车异常」，不逐行降级 |

## 目录结构

```
bom-site/                     本地网站与调度
  launcher.ps1                启动 / 停止 / 状态 / 体检 / 调试 Edge 的统一入口
  server.py                   HTTP 服务（127.0.0.1:8765）
  engine.py                   BOM 解析 + 候选打分 + 单行分析
  bom_params.py               参数解析（封装归一化、阻容感/电压/引脚抽取、目录归类）
  rules.py                    采购规则与 SKU 决策（纯函数：能不能买、选哪一档）
  taobao_runner.py            批量调度、会话、购物车、加购确认、对账
  errors.py                   错误分类（重试 / 人工 / 程序异常）与日志脱敏
  diagnostics.py              执行诊断流水（按行号记「搜到几条、卡在哪一步」）
  lcsc.py                     立创商城器件查询（编号/型号 → 权威规格）
  window_pos.py               调试浏览器窗口显隐（跑批时不抢前台）
  static/                     前端（无框架）
  settings.example.json       本机配置模板（settings.json 本身不进仓库）
  test_*.py                   27 套、约 1500 项断言的纯 Python 测试
  runtests.py                 测试总入口：一条命令跑全部（UTF-8 输出）
  mypy.ini                    类型检查范围（渐进式，先覆盖新模块）
  samples/                    示例 BOM / 候选
Taobao-Search-Skill/scripts/  淘宝执行层（Playwright over CDP）
taobao-bom-procurement/       决策层 SKILL 文档
ruff.toml                     静态检查规则（只查会藏 bug 的，不做全库格式化）
requirements-dev.txt          开发/CI 依赖（ruff + mypy）
*.bat                         三个 Windows 入口（UTF-8 / CRLF）
VERSION                       当前版本号
```

**分层是硬的**：`rules.py` 不 import `taobao_runner`，不碰浏览器、网络和文件；它只回答
「这个候选能不能买」和「这家店的 SKU 选哪一档」。`bom_params.py` 只依赖 `re`，是纯粹的
字符串解析层。调度器与引擎把规则入口 re-export 回去（`tr.suggest_sku`、`engine.parse_params`
这类老写法照常能用），所以拆分不改变任何调用方。

## 测试

一条命令跑全部（在 `bom-site` 下）：

```bash
cd bom-site
python runtests.py
```

输出形如：

```text
Python 3.14.4 · 27 个脚本 · UTF-8 输出
  [PASS] test_engine.py                              0.4s
  ...
27/27 套通过，共 1496 项断言，总耗时 18.7s
```

`runtests.py` 做三件事，都是为了不再出现「测试没跑起来却以为挂了」：

1. **自己发现 `test_*.py`** —— 新增脚本不用改 CI，也不用记清单；
2. **把子进程输出钉成 UTF-8** —— Windows 默认控制台是 GBK，`print("µF")` 直接抛
   `UnicodeEncodeError`，看起来像断言失败，其实一条都没执行（以前要手工设
   `PYTHONIOENCODING=utf-8`，现在 cmd / PowerShell / Git Bash 结果一致）；
3. **失败时打印该脚本末尾 25 行**，并通过退出码让 CI 变红。

常用参数：

| 命令 | 作用 |
|---|---|
| `python runtests.py test_engine` | 只跑名字匹配的（可给多个子串） |
| `python runtests.py --list` | 只列出会被跑的脚本 |
| `python runtests.py --fail-fast` | 第一个失败就停 |
| `python runtests.py -v` | 透传每个脚本的完整输出 |
| `python runtests.py --include-http` | 连 `test_http.py` 一起跑（需 8765 已在跑） |

单个脚本仍可单独跑：`python test_engine.py`（老控制台请先 `set PYTHONIOENCODING=utf-8`）。

各套件分工：

```bash
cd bom-site
python test_engine.py                 # 解析与规则
python test_lcsc.py                   # 立创编号解析与型号核准
python test_sku_selection.py          # SKU 选择与门禁
python test_runner.py                 # 调度、对账、进度、续跑
python test_errors.py                 # 错误分类 / 日志脱敏 / 导出列
python test_diagnostics.py            # 诊断流水 / 结构变化判定
python test_verification_recovery.py  # 验证/风控恢复 + 自动过滑块
python test_window_focus.py           # 窗口显隐与不抢前台
python test_click_guard.py            # 点击三守卫
python test_cart_cleanup.py           # 精准清车不误删、删车后进度作废
python test_batch_progress_guard.py   # 换 BOM 不误用上一批进度、重跑语义、引擎升级不误判换表
python test_procurement_guard.py      # 采购正确性（编号锚定/设计值/单价/同系列降规格/演练同闸门）
python test_bom_regression.py         # 三份真实 BOM 的多品类回归（金标快照 + 危险不变式）
python test_candidate_replay.py       # 候选回放（31 条真机标题 + 6 条对抗样本，判定不许漂移）
python test_sku_replay.py             # SKU 档位回放（33 家真店的选项组，选中即核实）
python test_screenshot_retention.py   # 截图留存（只留本批证据图，.jpg 也要能删）
python test_single_run.py             # 单行执行（前置拦截、进度只覆盖这一行）
python test_line_update.py            # 人工放行 / 改搜索词会落盘
python test_cart_remove_line.py       # 行级删车（只删这一件，别的行不动）
python test_cart_reconcile_scroll.py  # 购物车滚动加载与收尾对账
python test_cart_view_ui.py           # 购物车维护区块的前端结构
python test_buy_guard.py              # 加购三态确认与金额闸门
python test_query_fallback.py         # 搜索词回退（词 1 全被门槛拒时换词）
python test_private_window.py         # 无痕工作窗口与登录 cookie 回写
python test_resume_restore.py         # 「上次分析」恢复提示条
python test_settings.py               # 配置回落 example、登录态目录可被环境变量覆盖
python test_integration.py            # 假会话端到端（批量→对账→删车；HTTP 路由/同源/导出）
```

以上 27 套（含 1 套端到端）纯 Python、不需要浏览器；涉及真实淘宝的路径需要调试版 Edge + 已登录会话。
另有 `python test_http.py`：需要服务**已经在 8765 跑着**，只做 HTTP 冒烟打印，不断言，故不进 CI。
所有测试都把批量进度隔离到临时目录，不会碰到 `runs/` 里的真实进度。

### 静态检查（CI 同款）

```bash
pip install -r requirements-dev.txt

# 仓库根目录：未使用导入、未定义名字、可疑异常处理、import 排序
python -m ruff check bom-site Taobao-Search-Skill/scripts

# bom-site 目录：类型检查（渐进式，只覆盖边界清晰的模块，见 mypy.ini）
cd bom-site && python -m mypy
```

CI 在 Windows runner 上依次跑 **ruff → mypy → runtests.py（27 套）→ 启动器语法/体检**
（见 `.github/workflows/tests.yml`）。规则是刻意收窄的：不做全库自动格式化（一次重排上千行
会把真实改动埋掉），类型检查先覆盖 `lcsc.py` / `errors.py` / `diagnostics.py` /
`bom_params.py` / `runtests.py`，老模块等拆分时再逐个纳入。

### 多品类回归与候选回放（v0.10.0 → v0.11.0）

数字钟那份 BOM 只有阻容二三极管，所有闸门都是从那 19 行踩出来的。开发板（MCU/排针/LED/晶振）和电源板（LDO/DC-DC/钽电容）一进来，立刻暴露「按位号判品类」「封装名当型号」「0Ω 写成 0mΩ」这类问题。所以加了两层：

| 机制 | 文件 | 干嘛的 |
|---|---|---|
| 多品类回归 | `test_bom_regression.py` + `tests/fixtures/boms/` | 三份真实 BOM（18+29+20 行）走完整分析，立创编号用离线快照打桩；危险结论写成**不变式**（零欧搜索词不许出现 mΩ、NC 行不许有搜索词、2x20 排针必须判 connector），其余全量结论进**金标** `golden-analysis.json` |
| 候选回放 | `test_candidate_replay.py` + `tests/fixtures/replay-titles.json` | 真机上实际打开过的 31 条商品标题 + 6 条对抗样本，直接跑标题/材料两道闸门，判定不许漂移 |

改了规则要更新金标：`python test_bom_regression.py --update-golden`，它会把差异打印出来，逼你看一眼「这次动了哪些行」。

| SKU 档位回放（v0.11.0） | `test_sku_replay.py` + `tests/fixtures/sku-replay.json` | 从演练抓到的 33 家店里挑出 12 组（4 家该买到、8 家必须拒买），跑一遍选值 → 材料闸门 → 值核实；不变式：**选中了值就必须能核实出 BOM 参数** |

SKU 夹具同样可重建：`python tests/build_sku_replay.py` 从 `runs/sku-observations.jsonl` 重新生成（期望结论表在脚本里，改行为要显式改期望）。

**SKU 观测怎么来的**：演练/真跑每打开一个商品，就把该商品的 SKU 结构（组名 + 每档文本 + 页面已选值）追加到
`runs/sku-observations.jsonl`，一份 JSON 一行。这是回放的原料，也是排查「这家为什么没买」的现场记录。


## 常见问题

| 现象 | 处理 |
|---|---|
| 撞到滑块/图片验证 | 页面顶部会出现提示条，批量原地等（默认 30 分钟），**不会把淘宝窗口突然弹到前台**；想看窗口点「显示淘宝窗口」，过完验证点「验证已完成，重新检测」 |
| 双击 bat 后中文乱码 | 脚本已固定 `chcp 65001` + UTF-8；若仍乱码，确认按 `.gitattributes` 检出（`.bat` 为 CRLF），别用记事本另存为 ANSI |
| 启动器说 8765 被占用 | 先跑 `启动BOM选型台.bat diag` 看是谁占着；多半是上次没退干净，跑 `启动BOM选型台.bat stop` 再启动 |
| 提示缺少 playwright / openpyxl | `pip install -r requirements.txt` |
| clone 下来没有 `bom-site/settings.json` | 正常，它不进仓库。没有就按 `settings.example.json` 跑；要改配置再 `copy settings.example.json settings.json` |
| 登录态在哪个文件，能不能直接分享项目文件夹 | 在 `bom-site/.cdp-profile/` 或 `.browser-profile/` 里（`Default/Network/Cookies`）。这是登录凭证，分享时只给代码，别拷这两个目录 |
| 9222 被普通 Edge 占了 / CDP 不通 | 关掉所有 Edge 窗口再双击 `打开调试版Edge.bat`；脚本会先探测 CDP，已就绪就不重复启动 |
| 服务起不来，窗口一闪而过 | 窗口不会自动关（失败会 pause），看报错；或跑 `diag` 查 `bom-site/runs/server.err.log` |
| 加购频繁「无回执」 | 多为限流前兆，程序会自动降温；可调 `TAOBAO_LINE_GAP`、`TAOBAO_COOL_*` 环境变量放慢 |
| 浏览器被关掉 | 网页里点「启动淘宝会话」会自动重开；窗口默认藏在屏幕外，需要点「显示淘宝窗口」 |
| 解析少行 | 多工作表 XLSX 会自动合并（表头不一致的页会在警告里点名）；若仍少行，检查表头是否被识别 |
| 批量跑完有几行显示「跳过」 | 型号/名称/参数都空的行没有搜索词，跑也搜不到东西，会直接跳过并在行上写明缺什么；补好 BOM 再跑 |
| 跑完一堆行变成「待核对」，提示购物车里没找到 | 先看是不是**整批**都这样：整批都不匹配时程序会判「读车异常」（车被清空/页面没渲染完），此时会保留「已加购」并标「未核实」，不会再整车降级；个别几行对不上才按行降级，去购物车看一眼那几行即可 |
| 购物车越跑越多 | 用「购物车维护」按关键字删除，或跑批前清空；开跑前会自动看件数，≥190 直接不启动（淘宝上限约 200 行） |
| 换了一份新 BOM 再点加购 | 会按 BOM 指纹识别出「不是上一批」，旧进度自动作废、从头跑；上一批的加购记录仍保留，「删上次加购的」照常可用 |
| 买到的阻值/型号不对，或单价离谱 | 名称列（`Name`/`Comment`）是设计值时优先按它买，型号复用错会记 `V01` 风险提示；每只单价超过品类常见价（电阻 ¥2、电容 ¥6、开关 ¥15）直接不买；评分 20 分以下、或单规格商品标题核实不到规格的候选也不会自动加购。这类行会停在「失败」并写明原因，交人工挑 |
| 想重跑上一批，结果提示「已跑过」并跳过 | 按钮按情况自动切换：全部跑完时显示「重新跑全部」（走 `redo`，清空进度从头跑）；有失败/待核对行时旁边出现「重跑问题行 N」（只重跑这几行，已成功的仍跳过）。重跑会重复加购，所以先点「删除上一批」清购物车 —— 进度清掉后仍可用，因为清之前会留一份底档 |
| 嘉立创EDA 导出的 BOM，怎么保证买对 | 每行都带立创编号（`Supplier Part`，形如 `C46749`）。分析时先拿编号去立创商城换回**权威规格**（型号/品牌/封装/阻值/耐压/精度/库存），采购也以它为准：**第一条搜索词就是编号对应的型号**（如 `CC1H103ZA1ED3F4D1100 瓷片电容`），标题命中型号的候选优先；没命中就按编号规格逐项核对（值/耐压/族系/脚距），结果里标「同规格替代」，每行都留一条「采购依据 C 编号 + 是否命中型号」。两种冲突分开处理：型号列与编号不一致（`V02`）以编号型号为准并提示核对；**名称列的设计值**与编号规格冲突（`V01`，例：名称写 470Ω、编号却是 1kΩ 的那颗料）说明 BOM 自相矛盾，这一行不自动买、交人工核对原理图 —— 编号是唯一标识，但两处打架时替谁猜都可能买错。没有编号的行可用完整型号精确匹配（型号完全一致才认，模糊命中一律放弃），但同型号反查只是参考，不当采购依据 |
| 批量在跑，页面数字却不动 | 看按钮右边的连接状态：`实时`（服务端推送中）、`轮询中`（推送断了，退回 3 秒一问）、`已断开 Ns`。以前只靠前端 3 秒轮询，标签页在后台会被浏览器限流到 1 分钟一次、服务重启后旧页面还会静默失联，两种都表现为「数字卡住」。现在进度由服务端推（SSE），切到别的标签页照常刷新，断线会自己重连；真的连不上时会显示断了多久 |
| 某行失败了，不知道要不要重跑 | 看这一行悬停提示里的 `error_code` 和导出表的「处理建议」列：`可重试`（超时/点击被挡）直接点「重跑问题行」；`需要人工处理`（验证码/登录/页面改版）先处理再跑；`程序异常` 把 `runs/server.err.log` 里同 code 的 `[error]` + `[error/tb]` 行一起反馈 |
| 怀疑淘宝页面改版了 | 看行结果有没有 `structure_suspect`，或 `curl "http://127.0.0.1:8765/api/diagnostics?line=行号"`；`summary.reason` 会写「N 条搜索词全部 0 结果」或「N 次选择器/开页失败」，诊断明细在 `runs/diagnostics-*.jsonl` |

## 可调参数（环境变量）

| 变量 | 默认 | 作用 |
|---|---|---|
| `TAOBAO_LINE_GAP` | 1.2 | 行间隔秒数，越大越慢越稳 |
| `TAOBAO_COOLDOWN_EVERY` / `TAOBAO_COOLDOWN_SEC` | 20 / 40 | 每 N 行冷却 M 秒 |
| `TAOBAO_UNC_TRIGGER` / `TAOBAO_UNC_COOL` / `TAOBAO_UNC_COOL_MAX` | 2 / 60 / 300 | 连续无回执时的自适应降温 |
| `TAOBAO_CART_WARN` / `TAOBAO_CART_STOP` | 150 / 190 | 购物车件数提醒 / 拒跑阈值 |
| `BOM_KEEP_DAYS` | 14 | runs/ 里日志与旧截图的保留天数（0 = 不清理）；本批证据图不受此限 |

## License

[MIT](LICENSE) © 2026 zyx781

> 仅限个人学习与自用采购辅助；请遵守目标网站服务条款，风险自负。
