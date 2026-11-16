---
name: taobao-bom-procurement
description: 把硬件 BOM（CSV/Excel：型号、数量、封装、参数、位号）蒸馏为逐行采购意图，驱动本地已登录的淘宝会话（Taobao-Search-Skill v4 session 模式）完成搜索、候选评估、SKU 匹配与加购；结算与付款永远由人工完成。当用户上传 BOM 并要求「按 BOM 采购 / 配单 / 询价 / 比价 / 加购物车 / 找替代料」，或提到阻容感、MCU、功率器件、连接器等电子元器件采购时使用。禁止自动支付、模拟登录、Cookie 劫持、验证码绕过与平台风控规避。
---

# 淘宝 BOM 采购（决策层 Skill）v0.9.7

> **绑定实现**：执行层 Taobao-Search-Skill v4.0.0（`scripts/taobao.py` session 模式）
> ＋ 决策引擎 `bom-site/engine.py`（`ENGINE_VERSION = "0.1.0"`）
> ＋ 执行封装 `bom-site/taobao_runner.py`。
>
> **每个数字都能在代码里核到**，函数对应关系：
> `engine._check_title`（硬门槛）／`engine.score_candidates`（评分与价格）／
> `engine.detect_pack`·`plan_qty`（份数）／`taobao_runner._tol_verdict`（容差分档）／
> `taobao_runner._add_line_to_cart`（批量单行）／`suggest_sku`（SKU 选择）／
> `taobao_runner.add_to_cart`（金额闸门）／`browser_adapter.wait_for_cards`（三态等待）／
> `taobao.SessionHandler._cmd_cart_view`·`_cmd_cart_remove`（购物车问答）。
>
> v0.5 → v0.6 改了哪些口径，见 **§18**；执行层契约（三态/错误码/返回结构）见 **§19**；
> 已知限制与人工清单见 **§20**；**v0.7 → v0.8 的「验证先自救」规则**见 **§21**。
> 执行层或引擎再改，必须同步本文件的版本号。

---

## 0. 四条硬约束（优先于一切效率）

1. **付款不碰。** 购物车是终点；结算、付款、地址、发票由人在淘宝内完成。
2. **不绕验证。** 出现验证码/滑块/短信/人脸立即停。会话进入阻断态，人工完成后再继续。
3. **可以换词，不可以降规格。** 搜索失败只改查询表达；封装、型号、耐压、数值不许"差不多"。
4. **钱和数量都有闸门。** 单行金额有上限、整包超量有阈值（§6.5、§11）；宁可标 `uncertain` 交人，也不自动买贵的/买多的。

---

## 1. 真实调用方式

```bash
# 启动（必须带窗口，不要 --headless；首次需人工登录）
python scripts/taobao.py session --task-id bom-<日期>-<序号>

python scripts/taobao.py check-session
python scripts/taobao.py clear-session
```

- 进程从 **stdin 逐行读 JSON**，每条命令向 **stdout 写一行 JSON**；诊断信息走 stderr，不要混读。
- 一次只发一条，读到返回再决定下一条；禁止并发、禁止预发。
- **命令全集（11 条）**：`search` `open` `select-sku` `cart-add` `cart-view` `cart-remove` `dom` `raw-dom` `screenshot` `ping` `quit`。
  - `cart-remove` 是唯一破坏性命令，必须带 `keyword` 或 `{all:true, confirm:"清空"}`。
  - `raw-dom` 只读，用来补店铺名/参数表这类 JSON 里没有的信息。
  - `ping` 不搜不点不截图，只探活；**验证阻断后用它判断人工是否已完成验证（滑块/图片点选）**。
- 返回信封固定：

```json
{"status":"success","task_id":"...","screenshot":"<png路径>","page_text_summary":"前500字","data":{...}}
{"status":"error","task_id":"...","error":{"message":"...","code":"..."}}
```

- `screenshot` 是**视口截图**（不是整页），落在 `scripts/.cache/taobao-search-skill/artifacts/`。
- `cart-add` 的确认是**6 秒轮询**：每个循环同时看「确认弹层 / 页面已加入购物车 toast / 顶部购物车角标 +1」，任一命中即返回 `cart_verified_via`（`popup` / `toast` / `badge` / `cart_page`）。
- 会话文件在 `scripts/.cache/taobao-search-skill/taobao-session.json`（含 cookies）。**AI 不读取、不导出、不上传**；用户可用 `clear-session` 清除。

**命令超时（执行封装强制，见 `taobao_runner.DEFAULT_TIMEOUTS`）**：search 90s、open 90s、select-sku 90s、cart-add 150s、cart-view 60s、cart-remove 180s、dom 60s。
超时后会话判 `desynced`，**必须重启会话**，不要继续发命令。

---

## 2. 能力边界（决定哪些规则真的可执行）

| 需要的数据 | 能否从 JSON 拿到 | 来源 | 处理 |
|---|---|---|---|
| 标题 / 价格 / 销量 / 好评率 / 天猫标识 | ✅ | `search.data.items[]` | 直接用于硬门槛与初筛 |
| SKU 组、选项、`disabled`、`selected` | ✅ | `open` / `select-sku` 的 `sku_groups` | 匹配与校验 |
| 详情价 / **选后每份价** | ✅ | `detail_price` / `final_price` | 记账与比价（§11） |
| 单份含量（1 份 = N 只） | ⚠️ 推断 | SKU 文本 → 标题 → 详情页文本 | 三处都认不出 → `pack=null`，按 1 只/份算 |
| 店铺名 / 店铺类型 | ❌ | `dom` 元素文本或 `screenshot` | 读不到 → `UNKNOWN`，不得猜 |
| 实拍评价 / 差评 / 问大家 | ❌ | 只能看截图（首屏通常没有） | 默认不可得；`rating` 只当弱信号 |
| 参数表 / 丝印 / 引脚照片 | ⚠️ | `screenshot`（详情页首屏可能含参数） | 看不到 → `UNKNOWN` |
| 库存 / 起订量 | ⚠️ 间接 | `sku_groups[].values[].disabled` | 只能判"该选项不可选" |
| 加购数量 | ✅ | `cart-add` 的 `qty`（份数，详情页设置并回读） | 仍须 G4 复核 |
| 购物车明细 | ✅ | `cart-view` 回 `rows[]`（index/title/spec/price/qty/url）＋ `rows_page/rows_total/pages_read` | I4 已交付；前端「查看购物车」直接渲染明细 |

> 任何"店铺等级、实拍评价、动态评分"的判断，读不到就写 `UNKNOWN` 并降低置信度，**不要用标题或价格反推**。

---

## 3. 输入契约（BOM 解析）

**表头门（`engine.parse_workbook`）**：`型号 / 名称 / 参数` 三列**至少有一个**，否则整表解析失败并报错。
**数量缺失不阻断整表**：该行标 `needs_user`，其余行照跑。

**推荐列**：行号、名称、封装、关键参数、位号、品牌、替代策略、关键件标记、备注。

**表头别名（`engine.HEADER_ALIASES`，中英混写都认）**

| 字段 | 代表性别名 |
|---|---|
| 行号 | 行号、序号、编号、项次、`line`、`no.`、`#`、`item` |
| 型号 | 型号、料号、物料编码、规格型号、`mpn`、`pn`、`manufacturer part`、`mfr part`、`mfg part` |
| 名称 | 名称、描述、品名、品类、`description`、`comment`、`标识` |
| 封装 | 封装、封装形式、`footprint`、`package`、`case` |
| 参数 | 参数、规格、关键参数、**值**、数值、`spec`、`value` |
| 数量 | 数量、用量、采购数、数目、`qty`、`pcs` |
| 位号 | 位号、参考位号、`refs`、`designator` |
| 立创编号 | 供应商编号、立创编号、立创元件编号、嘉立创编号、嘉立创元件编号、`Supplier Part`、`LCSC Part #` |
| 供应商 | 供应商、供货商、`supplier`、`vendor` |

**嘉立创EDA（专业版）导出的表**可原样上传，表头是：
`ID / Name / Designator / Footprint / Quantity / Manufacturer Part / Manufacturer / Supplier / Supplier Part`。
其中 `Supplier Part` 就是立创编号（`C46749`），解析时只收 `C\d{3,9}`，其余值（`无`、`-`）留空。
中文版表头 `序号 / 数量 / 器件名称 / 器件标号 / 器件参数 / 供应商编号 / 供应商 / 封装 / 备注` 同样认。
注意：嘉立创导出的 CSV 是 **UTF-8 + 制表符分隔**，本项目按嗅探分隔符处理，无需另存。
编号怎么用见 §6.1.3。

**Altium / KiCad 直接导出的表**可原样上传：`No. / Quantity / Comment / Designator / Footprint / Value / Manufacturer Part / Manufacturer`。

- `Manufacturer Part` → 型号；`Comment` → 名称（若 Comment 本身是料号会顶成型号；`100nF`/`1kΩ`/`40MHz` 这类纯数值不当型号）。
- `Footprint` 前缀写法要还原：`C0402`→`0402`、`R0603`→`0603`、`LED0603-RD`→`0603`、`VQFN-24_*`→`QFN-24`、`ESOP-8_*`→`SOP-8`、`TDSON-8_*`→`TDSON-8`、`CONN-TH_*`/`CAP-TH_*`→`插件`。
- 没有型号列时，Altium 封装名末尾的料号会被取用（`OSC-SMD_4P-L3.2-W2.5-BL_SIT8008BI` → `SIT8008BI`）。
- 连接器的名称列是**信号用途**（`GPIO: GND,VTREF,OUT0`），**不从中抠料号**（会抠出 `OUT0` 把搜索带偏）。

**规范化**

- 封装：保留原写法并换算（0603=1608，0805=2012，1206=3216）；`LQFP48`→`LQFP-48`、`TO220`→`TO-220`、`VQFN`→`QFN`、`ESOP`→`SOP`。
- 参数：`10kΩ`、`100nF`、`15µH`、`±1%`、`0.1W`、`50V`；`33R`/`120R`/`4R7`/`0R` 这类省略单位写法也认。
- 数量：**BOM 用量 ≠ 加购份数**（§6.5）。
- 型号：保留后缀；去后缀只能做搜索变体，不能做匹配依据。
- 品类 6 类：`passive` / `ic` / `power` / `connector` / `module` / `other`。

**判 `needs_user` 的条件（`engine.analyze_line`）**

- 封装为空 → 缺"封装"（不阻断，除非其余全空）。
- 被动件缺阻/容/感值 → **阻断**（"缺少关键参数，无法锁定规格"）。
- 二极管 / LED / 磁珠 / 保险丝缺型号 → **阻断**（"必须有完整型号，封装不能代替型号"）。
- 芯片 / 功率器件缺型号 → **阻断**。
- 数量缺失或非正整数 → **阻断**（"数量缺失或不是正整数"）。
- `other` 类既无型号也无名称 → **阻断**。

---

## 4. 采购意图与搜索词

单行意图（`engine.analyze_line` 输出，字段比 v0.3 多）：

```json
{
  "line_no": 12, "category": "passive", "subtype": "resistor",
  "mpn": "", "package": "0603",
  "params": {"resistance_ohm": 10000, "tolerance_pct": 1, "power_w": 0.1},
  "params_text": "10kΩ ±1% 0.1W",
  "qty": 38, "criticality": "normal", "substitution_allowed": false,
  "status": "ready", "missing": [], "blocking": [],
  "hard_gates": ["封装 = 0603", "阻值 = 10kΩ", "精度 不限（BOM ±1%，按 ≤±20% 放宽）", "功率 ≥ 0.1W", "数量按 BOM 用量，不静默放大"],
  "query_plan": ["0603 10kΩ 电阻", "0603 10kΩ 贴片电阻"],
  "risks": [{"code": "H04", "text": "..."}, {"code": "H05", "text": "..."}],
  "decision": {"headline": "0603 10kΩ ±1% 贴片电阻 ×38", "buy_spec": [...], "must_verify": [...], "gate": "G1 解析确认 → G3 候选确认", "manual_approval_required": true}
}
```

**搜索词规则（`engine._build_queries`，上限 4 条）**

- **2–4 个高信号 token**，不要把整行 BOM 塞进去；不带数量、位号、单价、"急用/采购"。
- 被动件：`封装 + 数值 + 品类`（`0603 10kΩ 电阻`）；**不带精度**（写精度只会把结果收窄）。
  - 插件再补一条 `10kΩ 直插电阻`；阻值 <1000Ω 补 `0603 120R` 这类写法；有品牌补 `品牌 + 数值 + 品类`。
- 型号类（芯片/功率/模组）：**裸料号优先**，再 `料号 + 封装`、`料号 原装`、`料号 全新`。
  带封装的组合词（`DPAK`/`SMD`/`模块`）常把结果收窄到 0，裸料号反而命中。
- 二极管/LED/磁珠：`料号 + 封装` → `料号 封装 全新` → `料号 LED / 贴片`。
- 保险丝（无料号）：`8A 贴片保险丝` → `封装 8A 保险丝` → `8A 保险丝 快断`。
- 晶振：`8MHz 贴片晶振` → `8MHz 晶振 封装` → 料号。
- 连接器：按**系列 + 针数**，不是"2.5mm 连接器"这种废词。
  - XH：`B4B-XH-A`（有料号时）/ `XH2.54 4P 连接器` / `XH2.5 4P 连接器`
  - 排针/排母：`2.54mm 排针 2P` / `单排排针 2.54mm 2P`
  - 端子台：`5.08mm 端子台 2P` / `KF128-5.08-2P`
- 测试点/焊盘：用**直径**（`1.5mm 测试点 焊盘` / `1.5mm 测试点` / `PCB 测试点 1.5mm`），不要拿网络名（`MCU STATUS`）去搜。
- 兜底：都没生成出来时，取名称前半段（去掉 `DNP`/`tune`/`fast` 等词）。

**换词预算**：每行最多 **4** 条；批量模式下"有结果但候选全被硬门槛拒掉"也会换下一条（`search(require_scored=True)`）。

---

## 5. 执行循环

### 5.1 单行 13 步

1. **G0**：确认本地已登录、session 以 headed 启动（见 §12）。
2. **G1**：解析 BOM，向用户确认字段/单位/数量/关键件。
3. `search`：先不加 `price_min/price_max`、不预设 `require_tmall`，最多取 20 条样本。
4. 硬门槛过滤标题（`_check_title`）→ 只对 `score > 0` 的候选排序。
5. `open`：读 `item` / `detail_price` / `sku_groups` / `pack_size` / `suggested_qty`。
6. `suggest_sku`：按 §7.1 给每组算出选择；**有任何一组 `unresolved` 就不许加购**。
7. 数量：`suggested_qty = ceil(BOM 用量 ÷ 每份只数)`，并把"到手只数"写进清单（§6.5）。
8. **G3**：主选/备选/价格/风险/缺失维度（关键件逐行）。
9. `select-sku`：一次传全部 `selections`（用能唯一命中的最长文本，见 §7.1）。
10. **校验**：`all_selected` 只代表"你发的选择都点到了"。必须检查组里每个 `selected: true`，且 `final_price` 与预期一致。
11. `cart-add`（带 `qty`=份数）：看 `cart_added` / `cart_verified_via` / `qty_confirmed`；失败码见 §9.2。
12. **G4**：汇总清单（品名/规格/份数/到手只数/结算价/实付合计/异常项），人工复核。
13. `cart-view` 记 `cart_item_count` 前后差值；行明细看 `rows[]`（I4）；收尾对账见 §19.5；`quit`。

> 索引生命周期：`open` 的 `index` 只对**最近一次 `search`** 有效；重新搜索会替换 `items`，旧索引立即失效。不要跨轮复用 index。

### 5.2 批量（一键全部加购）

`_add_line_to_cart` 的固定顺序，任何一步失败都**不降规格**：

```
search(require_scored) → 候选池(score>0) → 排序 → 逐个 open → suggest_sku
  → 超量换店判断(≥8 倍) → 金额闸门(COST_TOO_HIGH / OVERBUY_TOO_MUCH)
  → select-sku → cart-add(qty=份数) → 记账(cost/unit_price/pack_size/actual_pcs)
```

- **候选排序**：默认按 `cost`（买够这一行要花多少钱）升序，其次 `over_ratio`；`prefer_cheap=false` 时按 `score` 降序。
- **每行最多试几个候选**：被动件 `max(max_candidates, 5)`，其他品类 `max_candidates`（默认 3）——一个链接常只挂部分阻值，多试两家才不会整行失败。
- **换店规则**：
  - 打开后发现整包超量 **≥8 倍**且还有候选 → 换下一家；**最后一个候选无论多超量都接受**（否则整行买不到）。
  - `COST_TOO_HIGH` / `OVERBUY_TOO_MUCH` → 换下一家；最后一个候选 → `uncertain`（交人确认是否买）。
  - `CART_UNCONFIRMED` → **停止这一行**，标 `uncertain`，不换候选（已经点过加购，换家会重复买）；但**不中断整批**。
- **行间间隔 1.2 s**：连续翻商品页最容易触发滑块，这是实测不触发风控的下限。
- **每行结束立即落盘**（§11），随时暂停/关机都不丢进度。
- 行状态：`pending` → `running` → `ok` / `failed` / `uncertain` / `skipped`（暂停时没轮到）。
- **进度是服务端推的**（`GET /api/taobao/batch/stream`，SSE）：跑批中每 1 s 一帧、空闲 3 s 一帧，
  连接 10 分钟后由服务端收尾（`EventSource` 自动重连）。前端只在推送断了的时候退回
  3 秒轮询，所以标签页切到后台、窗口被别的软件盖住，进度也照常走 —— 以前纯
  `setInterval` 轮询会被浏览器在后台标签页限流到 1 分钟一次，看着像「没在跑」。
  页面按钮右边有连接状态：`实时` / `轮询中` / `已断开 Ns`。

### 5.3 暂停 / 续跑 / 验证解锁

- **续跑默认开启**（`start_batch(resume=True)`）：读 `runs/batch-progress.json`，
  `ok` 的行直接标记「上次已完成（续跑跳过）」，`uncertain` 的行也跳过（避免重复加购），
  `failed` / `skipped` 的行重跑。想重头来：`resume=false` 或 `POST /api/taobao/batch-reset`。
- 进度文件是**合并写**（读旧文件 → 更新本轮结论 → 原子替换），空批量不会覆盖历史进度。
- **BOM 行也在盘上**：`runs/batch-lines.json`；服务重启后不传 `lines` 也能续跑。
- **验证处置（v0.8 起：先自救，再找人）**：
  1. 撞到验证（滑块 / 图片点选 / `_____tmd_____` 中间页）→ 执行层**先刷新页面自恢复**
     （`browser_adapter.recover_from_verification`，默认 2 次，间隔 1.5–2.8 s）；
     刷完不弹验证**且**能数出商品卡 → 视为已恢复，原地继续，**不惊动人、不挂 30 分钟**。
  2. 批量层这时做的是**立刻重试同一行**（`verification_next_step → "auto"`，
     默认 2 次、间隔 12 s），因为下一行的 `search` 会再触发一次上面的自恢复。
  3. 只有「刷新完仍然没有商品」才升级：`VERIFICATION_REQUIRED` → 批量暂停、标
     `stopped_reason`，`phase` 写「刷新后仍无商品，等人工验证（剩余 Ns）」。
     之后由 `ping` 探活（**非阻塞抢锁**，抢不到就跳过，绝不排队）：状态轮询里每隔 2.5 s 自动探一次，
     人工完成后 **2–3 秒内自动解除**，也可 `POST /api/session/recheck` 手动触发。
  4. 已经点过「加入购物车」的行**绝不重跑**（`verification_next_step(added=True) → stop`），防重复下单。
- **截图清理**：跑完/停止/撞验证时清 `runs/*.png` 与执行层 artifacts，保留 `settings.screenshot_keep` 张（默认 0 = 全清）；**运行中跳过**（删正在写的文件会让界面图裂）。
- **重头跑的正确顺序**：点「重新开始」清进度 → **先清空购物车** → 再「一键全部加购」。顺序反了会得到「旧料 + 又加一遍」的重复清单。

---

## 6. 候选评估

### 6.1 硬门槛（`engine._check_title`：哪些真淘汰，哪些只记风险）

**直接淘汰（`reject_code`）**

| 码 | 触发条件（标题阶段） | 适用品类 |
|---|---|---|
| H01 | 标题出现别的封装尺寸（`0402/0603/0805…`）且不含要求的封装 | 有封装的行 |
| H01 | 要排针却在标题见"母"；要单排标题写双排 | 连接器 |
| H01 | 针数不符；排针/排母**少于**需求（多于可用，可剪） | 连接器 |
| H01 | 间距不符（`pitch_match` 认定不同档） | 连接器 |
| H02 | 标题没出现完整型号 | 芯片/功率/模组 |
| H03 | 标题阻值/容值/感值 ≠ 要求（阻 0.5%、容感 2% 容差） | 被动件 |
| H03 | 标题里**所有**电压都低于要求耐压（多电压链接留一个够就放行） | 被动件 |
| H03 | 保险丝标题电流档位里没有要求值（`8A` 不认 `0.25A`） | 被动件/保险丝 |
| H03 | 沟道（N/P）反了；Vds/Id 低于要求；Rds(on) 高于要求 | 功率器件 |
| H04 | 标题含风险词（散新/拆机/翻新/打磨/改字/白字/原字/洗板/二手/返修/旧件/拆板） | 芯片/功率件 |
| H05 | 单只价 < P10 **且** < 0.75 × 中位价（样本 ≥5） | 全部 |
| H07 | 离线评分（`execution=False`）时，芯片/功率件拿不到店铺类型且无实拍 | 芯片/功率件 |

**只记风险/扣分，不淘汰**

- 精度超过 BOM：`≤max(±20%, BOM 值)` 只当 8 分权重，不拦（§16 第 1 条）。
- 功率(W) 不足：只当 10 分权重（被动件阻容感极少真的卡功率）。
- 封装没识别出来：`pkg_hit=True` 放行，靠 SKU 阶段兜。
- H06 物理矛盾（0603 标 0.25W、0603 电容标 100V）：**只复核不淘汰**（§8.3）。
- H07 批量执行时（`execution=True`）：降级为"加购前必须人工确认正品证据"。
- H08 库存/起订量：没有独立码，由 `sku_groups[].values[].disabled` 体现（该组无可用选项 → 换候选）。

### 6.1.1 候选层否决（`taobao_runner._candidate_veto`，**打开商品之前**）

引擎给的分数只保证"像"，不保证"是"。下面两条在候选列表阶段直接丢弃（记进 `rejected`，成本为零）：

| 条件 | 否决理由 | 反例（真实踩过） |
|---|---|---|
| 该行文本含 `保险丝/熔断`，标题里一个都没有 | 不是保险丝 | 「0451008 8A」行给 `贴片铝电解电容器220UF…`、`FFC/FPC连接器…` 都打 18.8 分且不拒 → 买回一个 FFC 连接器 |
| 该行是连接器（系列/排针/端子台）且**没要**"带线"，标题却含 `带线/线束/母头/渡锡线/端子线` | 形态不符 | `XH2.50mm-4P` 针座行买成「2P 适用单头渡锡线」，焊不上板，6 行都中 |

**料号只影响顺序，不影响生死**（`_title_has_mpn`）：标题含完整料号（≥6 字符）的候选排前面；
不含的照样可用 —— 国产替代/系列链接很常见（实测 30 行属于这种），做成硬闸会把这些行全判死。

### 6.1.2 采购正确性闸门（v0.9.7，全部在真机上踩过）

| 闸门 | 规则 | 反例（2026-11-11 真机） |
|---|---|---|
| 设计值优先（`_RE_DESIGN_VALUE` / `value_conflict`） | 名称列是纯数值（`470` / `2.2K` / `5kΩ` / `10nF`）时，**以名称列为准**，型号只作兜底；两者冲突时按名称值采购并在风险里记 `V01` | 一份 BOM 里 5kΩ、470Ω、2.2K 三行的 `Manufacturer Part` 全是 `MF1/4W-1KΩ±1%T52`，按型号解析把 42 只 470Ω 全买成 1kΩ |
| 每只单价天花板（`_unit_price_ceiling`） | 电阻 ¥2 / 电容 ¥6 / 电感 ¥10 / 二极管 ¥5 / LED ¥3 / 磁珠 ¥5 / 保险丝 ¥20 / 开关 ¥15 / 连接器 ¥30；折合每只超上限即 `UNIT_PRICE_ANOMALY`，换下一家 | 标题价 ¥94 的拨动开关其实是「2000 只一整袋」，BOM 只要 1 只；单行上限 200 元拦不住 |
| 淘汰级不自动买（`decision == 淘汰`） | 评分 <20 直接跳过；单规格商品 <60 也跳过（多规格商品由 SKU 选中值证明自己） | 22µF 行买到评分 51.9 的杂牌多值链接；1N4148 行花 ¥8 买 50 只 |
| 单规格商品看标题（`_title_value_kinds`） | 商品没有 SKU 选择器时，标题必须能解析出 BOM 要的阻值/容值/感值，否则不加购 | 5kΩ 搜出来整页压敏电阻，SKU 里也有数值选项，靠数值打分混过（`压敏/热敏/电位器` 另由 `_candidate_veto` 按品类否决） |
| 兜底不许降规格（`_spec_kinds`） | 最优候选命中、而兜底候选核实不到的**非数值**规格项（型号/针数）→ 跳过 | 最优候选「加购按钮找不到」后，兜底买了 1/8W 的 1K 顶替 1/4W，界面还写「已加入购物车」 |
| SKU 阻值先剔功率（`_sku_resistance`） | 解析前先去掉 `1/4W`、`1/8W`、`2W` 这类功率字样，再去掉前缀中文 | `1/4W 470R(100只)` 被 `^` 锚定读成 1Ω，470R 的选项全被判「与 BOM 冲突」，整行买不到 |
| SKU 选中值必须能证明规格（`_value_kinds`） | 多规格商品选中的 SKU 值（+标题）解析不出 BOM 要的阻值/容值/感值 → 不加购。选项里根本没有那个值时，SKU 冲突判定看不出来 | 95µF 搜到「微波炉高压电容 0.85/0.9/0.95/1.0UF」，系统选了「维修八件套」——配件包，不是电容，容值差 100 倍 |
| 同系列不同号＝降规格（`_sku_model_conflict`） | 选中值里出现与 BOM 型号**同前缀、不同序号**的料号（SS26 vs SS34）→ 不加购；料号互相包含算相同（NE555 ⊂ NE555P） | SS34 的链接里 SKU 既有「丝印SS34」也有「丝印SS26」，选错就是 60V 2A 顶 40V 3A |
| 演练与真跑同一套钱闸门（`_money_guards`） | 单价/单行金额/超量三道闸门抽成公共函数，`dry_run` 也过。以前演练在 `add_to_cart` 之前就 return，闸门全绕过 | 演练报告「19 行全通」，真跑 5 行被拦，演练等于白跑 |
| 材料/结构一致（`_material_conflict`，v0.9.7） | 阻值对了不代表装得上：电阻类型（金属膜/绕线/碳膜）、功率量级、电容耐压、脚距、**电容族系**（电解 / 瓷片 / 薄膜·启动电容）逐项比对，任一项冲突即换下一个候选 | 470Ω 金属膜买成「绕线 1W」（有感、体积 3 倍）；10nF/50V 瓷片买成 3KV 焊机电容；95µF 铝电解买成 CBB65 空调启动电容（容值一样，极性/体积/纹波全不同） |
| 族系靠封装与立创属性判定（`_bom_cap_family`） | 立创 `极性类型=有极性` → 电解；封装 `CAP-TH_BD<直径>`（圆柱体）→ 电解；`CAP-TH_L…W…` 或立创 `温度系数/介质材料` → 瓷片。判不出族系就不拦 | 只看容值时，95µF 的 CBB65 与铝电解无法区分 |
| 单位字母大小写 + 后面跟中文（`_RE_CAP` / `_RE_POWER` / `_RE_VOLT`） | 淘宝标题/SKU 写「10NF」「95UF单盒精装」「1/4W金属膜」「50V贴片」都是常态。正则收大小写，并用「后面不是英文字母」代替 `\b`（中文是 Unicode 词字符，`\b` 会判成没边界） | 只认小写时 `10NF/103` 判「核实不到容值」，50V 瓷片整行买不到；`\b` 收尾时 `95UF单盒精装` 读不出 95µF |
| 铝电配料号当第二证据（`_cap_spec_from_mpn`） | 立创查不到这颗料时，从 `PA100V100M10x12` 这种料号读出 100V/100µF，与名称列互相印证；只补空缺、不改名称列结论 | 名称写 95uF、料号写 100M（改了一半忘了另一半），没有第二证据就只能照 95µF 买 |
| 浏览器没了整批中止（`_run_batch`） | 每行开跑前探一次 CDP 端口；浏览器被关掉就把整批停下，`stopped_reason` 写明「重新启动会话后点继续加购」 | 浏览器中途被关，剩下 17 行全记成「搜索词都没有可用结果」，看着像选型失败，实际是环境没了 |

**这些闸门默认「买不到」而不是「买错」**：行会停在 `failed` 并写明原因（缺哪个规格项、单价多少、淘汰多少分），
剩余行照常跑完。人工只需处理这几行，不必复核整批。

**BOM 自己矛盾的行改口径说人话（`_review_reason`）**：名称列与料号冲突、且名称值不在标准档位（E24/E12）时，
这一行无论买到没买到都记 `uncertain` 并写明「5kΩ 不在 E24 标准档位，请核对原理图」——
不然失败信息会是「候选 5W 太大」，把人引去手动加一个 BOM 里本来就不存在的值。

### 6.1.3 立创编号核准（v0.9.3，从嘉立创 BOM 规范反推）

嘉立创EDA 导出的 BOM 每行带 `Supplier Part`（立创编号，形如 `C46749`）。官方 BOM 规范原文：
**「使用嘉立创元件编号进行匹配时，如果编号与 Comment 或 Footprint 不一致，系统以编号为准。
错误的编号比缺少编号更容易造成错误匹配。」** 匹配优先级是「器件值 → 封装 → 库存 → 基础库 → 拓展库」。

落到本项目（`lcsc.py` + `engine.analyze_line`）：

| 环节 | 做法 | 依据 |
|---|---|---|
| 认列 | `供应商编号` / `立创编号` / `嘉立创元件编号` / `Supplier Part` / `LCSC Part #` → `row["lcsc"]`，只接受 `C\d{3,9}` | 不同模板叫法不同，非编号值（`无`/`-`/纯数字）一律留空 |
| 编号解析（`resolve`） | `GET wmsc.lcsc.com/ftps/wm/product/detail?productCode=C…` → 型号/品牌/封装/**完整参数表**/库存/包装单位 | 参数表是中文原文（`阻值 10kΩ`、`功率 100mW`、`耐压 75V`、`温度系数 ±100ppm/℃`），不再靠正则猜 |
| 型号解析（`resolve_model`） | `POST …/product/query/list` 检索后**要求 productModel 完全相等**才采纳，模糊命中一律放弃 | 无编号的行只想知道「BOM 写的这颗料到底是什么」，不是让搜索引擎替人换料 |
| 冲突 V01 | 名称列设计值 vs 立创参数不一致 → 按名称列买，提示里带上立创证据 | 实测：名称写 100nF，型号 `CC1H103ZA1ED3F4D1100` 在立创是 **10nF**（`C2832505`），同表 #4 就是 10nF |
| 冲突 V02 | 型号列 vs 编号不一致 → **以编号为准**改型号，并留痕 | 官方口径；避免拿作废料号去搜 |
| 参数合并范围 | 只合并与选型有关的键：电阻 `阻值/功率/精度/耐压`、电容 `容值/耐压/精度`、电感 `感值/精度`、保险丝 `电流/耐压`、连接器 `针数/间距` | 芯片的「耗散功率 0.7W」、开关的「额定电压 12V」只展示，不进 `params` —— 否则摘要行会从 `CD4511BE` 变成 `0.7W` |
| 连接器类型 | 立创的 `连接器类型 = Type-C` 直接变成一条硬闸门，搜索词走「TYPE-C 母座 2P」 | 治的就是「KH-TYPE-C-2P 买成 VH3.96 二针座」 |
| 电容耐压 | 立创核准的耐压写进搜索词（`插件 22uF 50V 电容`） | 22µF 搜出来一堆 16V，装上会爆 |

三条不做的事：

1. **不按模糊搜索结果改设计值**。搜「470 1/4W 电阻」立创返回的第一条是 4.7kΩ —— 采纳就是换规格。
2. **不猜**。立创查不到（如 `PA100V100M10x12` 查无此件）就留空，该行照旧停失败交人工。
3. **不因为联网失败挡住流程**。缓存 30 天（`runs/lcsc-cache.json`），离线只读缓存，`LCSC_OFFLINE=1` 可强制离线；
   查询是并行 + 总预算 30 秒，19 行冷启动约 2.6 秒、命中缓存 0.5 秒。

### 6.1.4 重跑语义（v0.9.2）

「续跑跳过」只在用户**没说要重跑**时成立。三种入口，互不猜：

| 调用 | 行为 | 界面 |
|---|---|---|
| `start_batch(resume=True)` | 跳过进度里 `ok`/`uncertain` 的行，只跑剩下的 | 主按钮「继续加购 N 行」 |
| `redo=True` | 整批重跑：清空进度（清前留底档）、一行不跳 | 全部跑完时主按钮变「重新跑全部 N 行」 |
| `redo_lines=[2,15]` | 只重跑点名的行，其余照旧跳过 | 有问题行时出现「重跑问题行 N」 |

两条硬规则：

1. **全部可跑行都已定局时，`resume` 直接抛 `BATCH_ALL_DONE`**，不再静默空跑。
   踩过的坑：用户觉得上次买错了想重跑，点一下按钮，`done_map` 把 ok 行全跳过，
   进度条瞬间满格报「已跑完」—— 实际一行没动，用户以为系统跑过了。
2. **清进度前先把旧进度存成 `batch-progress-prev.json`**（`clear_batch_progress` 内做）。
   「删除上一批」要靠进度里的 `candidate.url` 定位购物车商品，清完就认不出来了 ——
   而重跑恰恰会重复加购、最需要清理。`batch_removal_targets` 在无进度时回落到这份底档。

### 6.2 评分（按品类换权重，满分 100）

| 维度 | 被动件 | 芯片 | 功率器件 | 连接器 | 模组 | 其他 |
|---|---|---|---|---|---|---|
| 型号/封装/参数一致性 `match` | 40 | 35 | 35 | 40 | 35 | 40 |
| 店铺 `shop` | 10 | 20 | 15 | 10 | 20 | 15 |
| 评价/实拍 `review` | 15 | 20 | 15 | 15 | 20 | 15 |
| 价格 `price` | 20 | 15 | 15 | 15 | 15 | 15 |
| 销量 `sales` | 15 | 10 | 20 | 20 | 10 | 15 |

**缺失维度**：只对"已知且可核实"的维度计分，权重按比例分摊，不按 0 分；报告里标注"缺失维度 + 原因"。

**子项权重（`match` 内部的 hit/miss）**

| 检查项 | 权重 | 检查项 | 权重 |
|---|---|---|---|
| 完整型号（芯片 / 功率） | 55 / 40 | 封装（被动件 / 其余品类） | 30 / 20 |
| 阻 / 容 / 感值 | 35 各 | 单一封装链接（被动件） | 15 |
| 精度（放宽口径） | 8 | 功率 ≥ | 10 |
| 耐压 ≥ | 20 | 保险丝电流 | 35 |
| 沟道 / Vds / Id / Rds | 15 各 | 针数 / 间距 | 30 / 30 |
| 名称匹配（仅 `other`） | 60 | 封装（仅 `other`，另加） | 40 |

**判定带**：≥75 主选；60–74 备选（须人工确认）；<60 淘汰。
**歧义**：前两名分差 <5 且第二名 ≥60 → `ambiguous`，交人裁决。
**销量分**：`100 × log10(1+sales) / log10(1+max_sales)`（同一批候选内相对值）。

### 6.3 价格：**双轨**

| 用途 | 基准 | 说明 |
|---|---|---|
| H05 异常低价 | **单只价**（报价 ÷ 每份只数） | 整盘 ¥120/1000 只不会被误判成"异常低价" |
| 价格维度打分/排序 | **采购合计价 `cost`**（买够这一行要花多少钱） | BOM 只要 1 个电阻时，100 只/包 ¥3.5 的单只价最低，但要掏 3.5 且多拿 99 个 |

- **H05 = 单只价 < P10 且 < 0.75 × 中位价**（样本 ≥5 才算）。只写"< P10"会机械淘汰每批最便宜的那条。
- **价格分（按 `cost`）**：≤P25 → 88（并提示"需确认不是引流价"）；P25–P75 → 100；≤P90 → 78；>P90 → 52 + `S01`。
- **样本 <5 条不下价格结论**（`price_conclusion=false`）。
- **引流价识别**：`select-sku` 后 `final_price ≥ 标题价 × 5` → 记 `price_shift`（标题那 `¥1` 是引流价，真实价在选完 SKU 后）。
- P10/P25/P75/P90 一律本地按同规格候选算，**不使用任何固定金额**。

### 6.4 店铺与评价（受数据限制）

| 店铺类型（按判定优先级） | 分数 | | 评价 | 分数 |
|---|---|---|---|---|
| 旗舰 / 官方 | 100 | | 好评率 0–1 → ×100 | 原值 |
| 企业 / 专营 / 专卖 | 80 | | 有实拍且无好评率 | 66 |
| 个人 C 店 | 55 | | 有实拍 | +8（上限 100） |
| 天猫（名称里看不出类型） | 85 | | 两者都无 | `None`（缺失维度，不按 0 分） |
| 未知 | `None` | | | |

> 判定顺序是"名称关键词 → 天猫标识"，所以天猫店名里带「专营/企业」时得 80、不是 85。

- 读不到店铺类型就 `UNKNOWN`，**不得推断"个人店=假货"**。
- `rating` 只作弱信号；没有实拍数据时**不得声称"评价好/有实拍"**。

### 6.5 整包与份数（数量语义，v0.4 新增）

散料常按整包卖：**1 份 = 50 / 100 / 1000 只**。数量一律算"份"，`份数 = ceil(BOM 用量 ÷ 每份只数)`。

- **识别顺序**：已选 SKU 文本 → 商品标题 → 详情页文本（`engine.detect_pack`）。
- **认得的写法**：`100只/包`、`50个/袋`、`1包50个`、`一件50只`、`每包100只`、`50个装`、`1件=50个`、
  `1卷=5000只`、`拍1发50`、`拍下1件发50只`、`1000pcs/卷`、`50个/件`、`1盘1000`、`整盘=2000`、`1000/盘`。
- **不误判**：`3个串口`、`4个通道`、`3个引脚`、`单只价` 不算包装数；「N个」只在标题含 包/装/袋/盒/卷/盘/份/拍/发/整 时才认。
- **输出**：`pack_size`（每份只数）、`pack_source`（从哪认出来的）、`suggested_qty`（份数）、`actual_pcs`（到手只数）、`unit_price`（单只价）。
- **超量惩罚只进排序**：`over_ratio ≥20 → -30`、`≥8 → -18`、`≥3 → -8`，只扣 `rank_score`，
  **不改 `score`/`decision`** —— 万一整行只有整包卖，扣到 60 以下会直接判淘汰，把"不划算"误判成"买不到"。
- 加购时数量以**页面已选 SKU** 为准重算一次（SKU 里常藏"100只"，标题看不出来）。
- **整包超量不再一律换店（v0.5）**：散料按 50/100 只一包卖，BOM 要 1–4 只时超量必然是 20–100 倍，
  旧口径"`over ≥ 8` 就换下一家"**恒真** —— 实测每行连开 4 家（14–22 s/家）后收的还是同样的 100 只/包。
  现口径：只有**后面确实有更小包装**、且其总价 ≤ `max(当前价 ×1.3, 当前价 + 1 元)` 时才换店；
  否则第一家直接收（少浪费 80 只的诱惑不值得多花 60 秒）。

---

## 7. 分类匹配规则

### 7.1 SKU 选择（`taobao_runner.suggest_sku` 的分支顺序）

**打分**（`_score_sku_value`，只用来"挑最好的"，**不用来判合不合格** —— 判合格的是 §7.1.1 的 veto）：

| 信号 | 加分 | 扣分 |
|---|---|---|
| 型号出现在 SKU 文本（≥5 字符，忽略符号） | +60 | |
| 阻/容/感值命中（阻 0.5%、容感 2%） | +50 | 冲突 -100 |
| 精度：≤BOM ≤±20% | +15 / +10 | >±20% -40 |
| 耐压够 / 不足 | +15 | -30 |
| 功率够 / 不足 | +10 | -20 |
| 保险丝电流同档 / 不同档 | +50 | -100 |
| 封装命中 / 命中别的封装 | +15 | -40 |
| 沟道（N/P） | +15 | -40 |
| 针数相符；排针多针 | +15 / +5 | -40 |
| 单排/双排 | +15 | -45 |
| 间距同档（2.50≡2.54） | +15 | -40 |
| 风险词（散新/拆机/翻新…） | | -80 |
| **不可直接下单**（询价/联系客服/拍下改价/议价） | | -150 |

### 7.1.1 硬属性 veto（`_sku_veto`，v0.5 新增 —— 这是最重要的一条）

**任何一项不一致，该 SKU 值直接出局，不参与打分、不可能被"保留默认值"，也不受总分影响：**

| 属性 | 判据 | 说明 |
|---|---|---|
| 阻值 | `_sku_resistance`：`0R / 2.2R / 100R / 1R2 / R24 / 4K7 / 4.7KΩ / 330Ω` | 差 >0.5% 即否决 |
| 容值 / 感值 | `_sku_capacitance` / `_sku_inductance` | 差 >2% / >5% 即否决 |
| 耐压 | SKU 文本里出现的所有电压取最大值 | **低于**要求即否决（高于要求放行） |
| 保险丝电流 | `engine.title_amps` | 不同档即否决 |
| 针数 | 认 `3P / 4PIN / 6针 / 3A(带扣)` 等写法 | 不符即否决；**例外**：排针/排母且是 `1*` 单排、针数多于需求（整条可剪） |
| 排数 | `1*40P / 2*2p / 单排 / 三排` | BOM 写了单/双/三排就必须同排数（`2*2p` 顶不了 `2P 单排`） |
| 间距 | `2.54mm / XH2.54` | 不同档即否决（2.50≡2.54） |
| 沟道 | N / P | 反了即否决 |
| 形态 | 连接器行**没要**"带线"，SKU 却是 `带线/线束/渡锡线/母头/端子线` | 焊不上板，否决 |
| 不可下单 | 询价 / 联系客服 / 拍下改价 / 议价 | 否决 |

**分支（自上而下，命中即止；`enabled` 只含通过 veto 的选项）**

1. 页面已选值（**已过 veto**）≥10 分 → **保留页面已选值**。
2. 该组**没有任何值能对上 BOM 参数**（纯包装数量/颜色/产地）→ 保留已选或取最小包装（"该组不区分电气规格"）。
3. 最高分 ≥10 且（唯一，或领先第二名 ≥5）→ **按 BOM 参数匹配**。
4. 该组唯一可选（已过 veto）且分数 > -50 → **该组唯一可选**。
5. 最高分 ≥10 但分差 <5 → 同分里按 **`_tie_pick`：电压正好等于要求 → 包装最小** 挑。
   *v0.4 这里取"最小包装"，包装数解析不出来就退化成"选项列表第一个" —— 电压组 `[6.3,10,16,25,50]V` 里 ≥16V 全是 +15 平手，结果挑中排第一的 **6.3V**（−30 分那个），9 行电容因此买错。*
6. 整组都是包装数量（`10个 / 100个`）→ 取最小包装，数量按 BOM 折算。
7. 一个值都没剩（全被 veto）→ `unresolved`：**所有选项都与 BOM 冲突（耐压 6.3V < 要求 16V）**。
8. 其余 → `unresolved`：页面默认值无法确认符合 BOM，交人工在下拉框里选。

**硬性要求**：只要 `unresolved` 非空，**不许 `cart-add`**；宁可换候选、标失败，也不"先加了再说"。
回归用例见 `bom-site/test_sku_selection.py`（34 条，覆盖上述每一条 veto 与打平口径）。

### 7.2 被动件

封装 → 数值 → 耐压 → 功率 → 品牌 → 价格。封装不符直接 H01。不得以"更小体积更高耐压"自行升级。
精度按 §16 放宽：能买、不拦，但不抢最优选。

### 7.3 芯片

完整型号 → 封装 → 原装性证据 → 店铺专注度 → 价格。后缀差异视为不同料；
"国产兼容/可替代型号"只能作为 G2 提案。`H07`：店铺类型与实拍都拿不到时，批量里降级为"加购前人工确认"。

### 7.4 功率器件

沟道/型号 → 耐压/电流/Rds(on) → 封装与散热 → 原装性 → 价格。
翻新与打磨是重灾区：引脚氧化、丝印重影、批次混杂、显著低价 → H04。

### 7.5 连接器/结构件

- 系列（XH/PH/ZH/SH/EH/GH/排针/排母/端子台/牛角/FPC）→ 针数 → 间距 → 排数 → 高度 → 材质镀层 → 价格。
- **间距 2.5 与 2.54 同档**（JST XH 真实 2.50mm，淘宝写 XH2.54）；**1.27 与 1.25 仍不同料**。
- 排针/排母整条卖（40P 一条自行剪断）：标题针数 **≥** 需求即可，少于需求淘汰。
- 单排/双排从 Altium 的 `1x02` / `2x20` 判，写法 `1*2p / 2*2p` 也要认。

### 7.6 替代料

默认 `substitution_allowed: false`。即使 BOM 允许，也只能提案，由 G2 逐项授权；
不得自行放宽封装/耐压/精度/温度等级。

---

## 8. 风险规则

### 8.1 翻新/散新信号

- 标题/详情含"散新、拆机、翻新、打磨、改字、原字、白字、洗板、二手、返修、旧件、拆板"。
- 价格显著低于同规格中位价，且店铺杂；无实拍、无包装/编带/批次标识。
- 评价/问大家提到"丝印模糊、引脚氧化、上机不良、批次不一"；同链接多型号混卖、SKU 无法区分批次。

芯片/功率件命中 → **H04 淘汰**；被动件命中 → **-12 分**并人工复核。

### 8.2 异常低价

`单只价 < P10 且 < 0.75×中位价` → H05；叠加"店铺杂 + 无实拍 + 评价异常" → 直接淘汰。低价不能提升排名。

### 8.3 物理合理性复核（只触发复核，不定罪）

| 封装/类型 | 常见合理范围 | 触发例子 |
|---|---|---|
| 0603 电阻 | 约 0.1W（>1.4× 触发） | 标 0.25W 无规格书 |
| 0805 / 1206 / 1210 / 2512 | 0.125W / 0.25W / 0.5W / 1W | 标 1W 的 1206 |
| SOT-23 | 持续电流通常 <1A | 标 5A/10A |
| TO-220 | 耗散数十瓦级（需散热） | 标 200W 无散热条件 |
| 0603 陶瓷电容 | 常见耐压 ≤50V | 小体积标 100V |

---

## 9. 异常处理

### 9.1 执行层修复状态（现码已合入）

| 问题 | 状态 | 现在的行为 |
|---|---|---|
| 加购丢 SKU | 已修 | 同商品不重载；必须重载时重放 `selections`；加购前后回读 SKU |
| 数量只能 1 件 | 已修 | `cart-add` 接受 `qty`（份数），详情页输入框优先、`+` 兜底（上限 20 次） |
| 验证码自动求解 | 已移除 | 检测到即 `VERIFICATION_REQUIRED`，批量暂停；`ping` 探活解锁 |
| SKU 包含匹配（`10K` 命中 `100K`） | 已修 | 规范化精确/前缀匹配，多候选返回 `SKU_AMBIGUOUS` |
| 加购确认误判 | 已修 | 6 秒轮询 popup / toast / 角标，任一命中即成功；角标读到 0 当"读不到" |
| 购物车页被风控挡成 Oops | 已修 | 改用顶部角标增量确认；仍不确定 → `CART_UNCONFIRMED`（标 `uncertain`，不重复点） |
| 购物车明细 | 已交付（I4） | `cart-view.rows[]` 是真实行明细；`items` 仍**不是**购物车内容 |
| 验证标志死锁（按钮全灰） | 已修 | `ping` + `recheck_verification`（非阻塞），2.5 s 自动探活 |
| 进度被清空 | 已修 | `batch-progress.json` **合并写**，空批量不覆盖 |
| 整盘 ¥1200 天价 | 已修 | 盘/卷包装识别 + 单行金额闸门（§11） |

### 9.2 错误码 → 动作

| 返回/现象 | 含义 | 动作 |
|---|---|---|
| `No search results. Run 'search' first.` | 未搜索就 open | 先 `search` |
| `Index N out of range (0-M)` | 索引失效/越界 | 重新 `search`，不要猜索引 |
| `Item has no URL` | 该卡片无链接 | 跳过，记录 |
| `No item open. Run 'open' first.` | 未 open 就选 SKU/加购 | 先 `open` |
| `Add-to-cart button not found` | 按钮缺失/遮挡/未选规格 | 截图判断；最多重试 1 次 |
| 搜索 `items_count: 0` | 空结果 | 换 query 变体（≤4 条）→ 仍空则 `A01` |
| `A01` | 所有搜索词都没有可用结果 | 换词/人工；**不许放宽硬门槛** |
| `A06` | 没有任何可核实维度，无法评分 | 补店铺/实拍信息，或人工判断 |
| `VERIFICATION_REQUIRED` | 刷新后仍无商品（验证没被刷掉） | **先自动**：执行层刷新 2 次 + 批量层重试 2 次；仍不行才停，人工完成后 `ping` 自动恢复。不破解、不改指纹 |
| `INVALID_QTY` | qty 非正整数 | 修正后重发 |
| `QTY_CONTROL_UNAVAILABLE` / `QTY_SET_FAILED` / `QTY_UNREADABLE` / `QTY_TOO_HIGH` | 数量无法设置或回读 | 停，人工在购物车设数量；不重复加购 |
| `QTY_CONFIRM_FAILED` | 已加购但数量未确认 | 人工核对购物车；`retryable: false` |
| `SKU_NOT_SELECTED` | 加购前 SKU 状态不符 | 重新 `select-sku`，不要直接重发 `cart-add` |
| `SKU_CONFIRM_FAILED` | 已加购但 SKU 未确认 | 人工核对购物车；`retryable: false` |
| `SKU_REPLAY_FAILED` | 重载后重放失败 | 停，人工重新选 SKU |
| `SKU_AMBIGUOUS` | 一个选项命中多个 | 读 `candidates`，选更精确的值 |
| `SKU_INCOMPLETE` | 还有 SKU 组没选中 | 补齐后再加购；不要"先加了再说" |
| `NO_SELECTIONS` | 没有任何 SKU 选择 | 单规格商品才允许（`allow_empty`），否则拒绝 |
| `CART_UNCONFIRMED` | 点了加购但确认不到 | 该行标 `uncertain` 并停止换候选；人工核对，**不要重复点击** |
| `COST_TOO_HIGH` | 本行合计超单行上限 | 换下一家；最后一个候选交人工（§11） |
| `OVERBUY_TOO_MUCH` | 超量 ≥50 倍且 ≥50 元 | 同上 |
| `BATCH_EMPTY` / `BATCH_RUNNING` | 没有行 / 已有任务在跑 | 先停旧任务再启动 |
| `status: error` 且无 screenshot | 命令级异常 | 记录原文，按 §14 判断是否停 |

### 9.3 搜索为空

只改查询表达：去包装后缀 / 别名 / 公制英制换算 / 中文品类名 / 加"原装"。最多 4 次。
仍空 → 标 `A01`，继续下一行；关键件立即询问。**空结果不许放宽硬门槛**（要改封装/型号/参数走 G2）。

v0.6 起，执行层在返回 0 候选前还会自己先做 2 次受控重试（去 `Ω/±/欧姆` → 换"贴片xx"后缀），
`data.attempts` 里能逐条看到哪次出了结果。**页面明确说没货（`-2`）要 ≤2 s 就返回，
不许再叠 `wait_for_results()`**；被风控中间页接管（`-3`）则停下来等人工，不许换词硬撞（§19.1）。

### 9.4 偶发错误、重试与限额

- **偶发错误**（值得原地重试一次）：`OPEN_FAILED`、`OPEN_TIMEOUT`、`SEARCH_NO_RESPONSE`，
  或报文含 `Timeout` / `超时` / `net::ERR` / `Target closed` / `Navigation` / `ECONNRESET` / `ERR_CONNECTION`。
- **不重试**：`VERIFICATION_REQUIRED`、`NO_QUERY`、`BATCH_RUNNING`、`BATCH_EMPTY`。
- 打开商品失败：**原地重试 1 次（间隔 1.5 s）** → 还不行换下一个候选 → 最后才整行失败。
- 搜索偶发超时：换下一条词，而不是判整行没救。
- **限额**：搜索词 ≤4；候选 ≤3（被动件 ≤5）；`cart-add` 只发一次（确认不到就停，不重复点）。

---

## 10. 人工确认门

`manual_approval_required` 默认 **true**（页面上固定展示 G1–G6）。

| 门 | 节点 | 内容 | 可否关闭 |
|---|---|---|---|
| G0 | 会话 | 本地已登录、headed/正常浏览器启动、通道确认 | 否 |
| G1 | 解析 | 字段/单位/封装/数量/关键件 | 否（可整批） |
| G2 | 替代 | 型号/封装/参数变更 | 否（逐项） |
| G3 | 候选 | 主选/备选/价格/风险/缺失维度 | 可降为整批一次 |
| G4 | 加购清单 | 品名/规格/**份数与到手只数**/单价/实付合计/异常项；人工复核与购物车一致 | 否 |
| G5 | 结算付款 | 人在淘宝完成 | **Skill 永不执行** |
| G6 | 收货发票 | 地址/抬头/税号 | 否 |

关键件（`criticality: critical`）的 G3 必须逐行确认。

---

## 11. 金额、上限与留痕（v0.4 新增）

### 11.1 单行金额闸门

`settings.json` 的 `max_line_cost`（缺省值）：

| 品类 | 上限 |
|---|---|
| `passive` | ¥200 |
| `connector` | ¥300 |
| `other` | ¥200 |
| 其他（含 `ic` / `power` / `module`） | ¥5000（等于不拦） |

- 判定时机：**点 `cart-add` 之前**（钱是那一下花出去的）。`est_cost = final_price × 份数`。
- 超上限 → `COST_TOO_HIGH`，换下一家；最后一个候选 → `uncertain`。
- 另一个闸门 `OVERBUY_TOO_MUCH`：`auto_qty` 且 `est_cost ≥ ¥50` 且 `到手只数 ÷ BOM 用量 ≥ 50` → 不自动买。
- **禁止**为了"跑完整批"去调大 `max_line_cost` 或关掉闸门。

### 11.2 实付记账链路

```
select-sku → final_price（选完 SKU 的每份价）
          → add_to_cart: cost = final_price × 份数, unit_price = final_price ÷ 每份只数
          → 批量 result → runs/batch-progress.json
          → batch_status(): cost_total / cost_lines / ok_count / cost_avg
```

- 单规格商品（没有 SKU）没有 `final_price`，用详情页 `detail_price` 兜底。
- 服务重启后内存批量为空，`cost_total` **从进度文件汇总**（按 mtime 缓存，不每次读盘）。
- `price_shift`：`final_price ≥ 标题价 × 5` 时记录倍数，提示标题价是引流价。

### 11.3 产物（以现码为准）

| 文件 | 内容 |
|---|---|
| `bom-site/runs/batch-progress.json` | 逐行结论（ok/failed/uncertain/skipped）+ 金额 + 缩略信息，**这是续跑的依据** |
| `bom-site/runs/batch-lines.json` | 本轮 BOM 行（重启后不传 lines 也能续跑） |
| `bom-site/runs/session-*.log` | 会话日志（含 `[batch] line #N status total=… search=… open=… sku=… add=…`） |
| `bom-site/runs/*.png` | 失败截图与加购证据（跑完按 `screenshot_keep` 清理） |
| `bom-site/采购统计-*.html` | 由 `stat_report.py` 生成的统计报告（KPI/分类/已下单/失败/未跑/超量） |

> v0.3 里写的 `purchase_intent.json` / `candidates.csv` / `cart_manifest.json` / `run_log.jsonl`
> **现码不产出**，不要再按那四个文件名去找。需要清单时从 `batch-progress.json` + `stat_report.py` 出。

---

## 12. 合规开关（运行前复核）

- 用**普通浏览器 + CDP**：先启动 Edge（独立 profile、`--remote-debugging-port=9222`），会话通过 CDP 附着；
  `taobao_runner.ensure_debug_browser` 只在 9222 不通时按同一 profile 拉起浏览器，**不会静默换成自动化 Chromium**。
- 已删除：`SliderSolver` 及其全部调用、`playwright-stealth`、navigator 覆盖、
  `--disable-blink-features=AutomationControlled`、随机视口/UA。
- 检测到验证码/滑块/验证页 → `VERIFICATION_REQUIRED`：会话进入阻断态，**搜索类命令都先返回这个错误**
  （只有 `quit` 能退出）；`cart-view` / `cart-remove` 例外——它们自己导航 `cart.taobao.com`、
  与搜索结果接口的风控无关，进页面后仍会再查一次验证弹层，所以清点/清理购物车不必等滑块解除。
  用 `ping` 当探针——返回 `success` 说明人工已完成验证（滑块/图片点选）、阻断自动解除，返回 `error` 说明验证页还在。
  探活由 `recheck_verification` **非阻塞抢锁**发起，不会和正在跑的批量抢命令通道。
- 运行前复核：`git log` 中含 P0-2 提交；没有就按"未合规"处理，只能离线决策。

---

## 13. 硬性禁止

1. 模拟登录、代填账号密码、复用他人会话。
2. 读取/导出/上传会话文件、Cookie、token（脚本本地持久化除外，且不得外传）。
3. 绕过或破解验证码、滑块、短信/人脸验证。
4. 自动结算、自动付款、自动确认收货。
5. 为规避风控新增/调优指纹、代理、频率、轨迹参数；**多标签并发抓取也算**。
6. 对平台接口逆向、批量爬取、压测。
7. 伪造采购凭证、评价、比价记录。
8. 未确认就加购或替代。
9. **调大 `max_line_cost`、关金额闸门、跳过 `unresolved` 强行加购**——这些都是在绕过 §0 的约束。

---

## 14. 停止条件

- 出现验证码/滑块/短信/人脸（人工完成后用 `ping` 或 `POST /api/session/recheck` 解锁，再续跑）。
- 异常登录、账号风险、环境异常；登录态失效且本地无法恢复。
- 页面结构变化导致命令不可靠；会话 `desynced`（命令超时后必须重启会话）。
- 必选 Gate 未确认；`uncertain` 行连续堆积（先人工核对购物车，再决定续跑）。
- **连续 3 行同类失败**（环境/脚本问题，不是个例）。

停止后：`quit` → 落盘进度 → 报告"停在哪、为什么、需要人做什么"（含 `cost_total` 与 `uncertain` 行号）。

---

## 15. 自检清单

- [ ] 执行层已完成 §12 的合规开关（CDP 附着、无 stealth、无自动滑块）
- [ ] BOM 表头满足"型号/名称/参数 至少一个"；行号唯一；数量为正整数
- [ ] 封装/单位已规范化（英制↔公制、Altium 前缀）
- [ ] 关键件标记已确认；替代默认 `false`
- [ ] `manual_approval_required: true`；G3/G4 已排进流程
- [ ] 已确认 `settings.max_line_cost` 未被放宽（§11.1）
- [ ] 已确认续跑口径：`ok` 跳过、`uncertain` 跳过、`failed/skipped` 重跑（§5.3）
- [ ] 已告知用户：只到购物车、数量按份折算、金额有上限、付款由人完成

---

## 16. v0.3 → v0.4 变更表

| # | v0.3 写的 | v0.4（现码口径） | 依据 |
|---|---|---|---|
| 1 | 精度低于要求 → H03 淘汰 | 精度**不拦**：`≤max(±20%, BOM)` 收，只当 8 分权重；搜索词不带精度 | `TOLERANCE_RELAX_PCT=20`、`_check_title` §6.1 |
| 2 | 耐压低于要求 → H03 淘汰 | 只有标题里**所有**电压都低才淘汰（多电压链接放行，具体档交给 SKU） | `_parse_voltages` + `_check_title` |
| 3 | `< P10` 触发 H05 | `< P10` **且** `< 0.75×中位价` 才触发 | `score_candidates` |
| 4 | 价格以中位价为基准 | **双轨**：H05 用单只价，价格分/排序用采购合计价 `cost` | `cost_stats` / `_add_line_to_cart` |
| 5 | 数量"考虑 MOQ/整包" | 独立的**份数模型**：`detect_pack` → `plan_qty` → `suggested_qty/actual_pcs`，超量惩罚只进 `rank_score`（`OV01`） | `engine.detect_pack`/`plan_qty` |
| 6 | 搜索词 ≤3、单行 open ≤3 候选 | 搜索词 **≤4**；候选 默认 **3**、被动件 **5**；`require_scored` 自动换词 | `_build_queries`、`_add_line_to_cart` |
| 7 | 连接器只说 1.27≠1.25 | 另加 **2.5≡2.54 同档**、排针整条 ≥ 针数、系列化搜索词、名称列不抠料号 | `PITCH_SAME_PAIRS`、`connector_series` |
| 8 | §11 产物是 4 个 JSON/CSV | 实际是 `batch-progress.json` / `batch-lines.json` / `session-*.log` / `采购统计-*.html` | §11.3 |
| 9 | 未提金额上限 | 新增 §11 单行上限 + `COST_TOO_HIGH` / `OVERBUY_TOO_MUCH` + 实付记账 | `max_line_cost`、`add_to_cart` |
| 10 | 未提批量续跑 | 新增 §5.2/§5.3：状态机、1.2 s 行间隔、合并写进度、resume、验证 `ping` 解锁、截图清理 | `_run_batch`、`_should_probe_verification` |
| 11 | §9.1 修复表未含 `cart-remove`/`ping`/`raw-dom` | 命令全集补齐 11 条；`ping` 作为验证探针 | `taobao.py::_HANDLERS` |
| 12 | H07 一律淘汰 | 离线评分淘汰；批量执行降级为"加购前人工确认正品证据" | `score_candidates(execution=True)` |

**未实现、不要照着写的（v0.3 遗留）**：`S02–S04`、`H08` 独立码、`A02–A05` 独立码、
`purchase_intent.json` 等四件产物。出现对应场景时用本节的实际码（`A01`/`A06`/`S01`/`OV01`/`uncertain`）。

---

## 17. v0.4 → v0.5 变更表

来源：2026-11-06 那轮 119 行跑批的实测（110 ok / 3 uncertain / 6 failed，购物车 81 件，
**其中 22 行买到了不对的东西**）。每条都能用 `bom-site/test_sku_selection.py` 复现。

| # | v0.4 写的/做的 | v0.5（现码口径） | 依据 |
|---|---|---|---|
| 1 | 属性冲突只扣分，靠总分决定 | 硬属性 **veto**：不一致直接出局（§7.1.1） | `_sku_veto` |
| 2 | 打平分"取最小包装" | 打平分按 `_tie_pick`：电压正好等于要求 → 包装最小；**绝不按列表顺序** | `_tie_pick`（#2 买成 6.3V） |
| 3 | 阻值只认 `Ω/K/M` | 补 `0R / 2.2R / 100R / 1R2 / R24 / 4K7` | `_sku_resistance`（#56/#59/#60/#62/#64/#67 买成 2.2R） |
| 4 | `_sku_has_spec` 用 `\b` 判针数 | 改 `(?![0-9a-zA-Z])`，汉字紧跟也认 | #28 一类 6 行买成带线母头 |
| 5 | 候选层只要求 `score > 0` | 增加 `_candidate_veto`：保险丝品类互斥 + 连接器形态否决（§6.1.1） | #22 买成 FFC 连接器 |
| 6 | 料号有没有无所谓 | `_title_has_mpn` 只做**排序优先**，不做硬闸（国产替代太多） | 实测 30 行不含料号仍买到对的 |
| 7 | 超量 `≥8` 就换下一家 | 只有"后面有更小包装且不更贵"才换（§6.5） | 每行白开 4 家，省 45–65 s/行 |
| 8 | 归一化后的 package 拿来判形态/品类 | 用 `_line_blob` 把**原始行**也算进来 | engine 把「排针 … 2P 单排」归一成「排针」、「SMD 保险丝」归一成「SMD」 |
| 9 | 无 SKU 回归测试 | 新增 `test_sku_selection.py`（34 条） | 上面 22 行的最小复现集 |

**（v0.6 已全部关闭）** 这三条曾记在 v0.5 的「还没做的」里：§9.3 空结果快返、`cart_view` 行明细、
`cart-remove` 误报 —— 见 §18 第 3/8/9 条与 §19.1/§19.3/§19.4。

---

## 18. v0.5 → v0.6 变更表

来源：2026-11-07 单日 9 轮真机跑批（含 R1 全量、多轮子集）。每条都能在代码里核到函数名。

| # | v0.5 的状态 | v0.6（现码口径） | 依据 / 函数 |
|---|---|---|---|
| 1 | 搜索只拼 `?q=`，首屏常常不挂载结果容器，白等 13–15 s | URL 固定规范化为 `?page=1&q=…&tab=all`；首屏仍无卡时**重新导航一次**（`_renav`） | `browser_adapter.search` |
| 2 | 0 候选就交给上层换词 | 先做受控重试：去单位符号（`Ω/±/欧姆`）→ 换同义后缀（`电阻→贴片电阻`），最多 2 个变体，全部记进 `data.attempts` | `SessionHandler._empty_retries` |
| 3 | `wait_for_cards` 只有「有卡/超时」，超时打满 12 s 再叠 `wait_for_results()`（再 10–20 s） | **四态**返回：`>=0` 有卡 / `-1` 换词 / `-2` 明确空结果（≤2 s）/ `-3` 风控中间页（硬拦，立刻停） | `browser_adapter.wait_for_cards` |
| 4 | 风控中间页（`_____tmd_____`）不弹窗、页面只显示「加载中…」，被当成首屏没渲染，换词硬撞 4 次 | 识别为硬拦：`-3` → `VerificationRequired`，批量进入「等人工」而不是烧词 | `risk_middleware_hit`、`_looks_access_blocked` |
| 5 | 容差只扣分，且 `cap = max(20, 要求)` 放行，0.1% 要求买 ±1% 还给 +10 分 | 按**倍数**分档：`≤要求 +15`、`≤1.5× +8`、`≤3× 0`、`≤10× −40`、`>10× −70`；普通档不否决，**精密档（要求 ≤0.5%）超 3× 直接 veto → unresolved** | `_tol_verdict`、`TOLERANCE_DEMOTE_RATIO=3`、`TOLERANCE_PRECISION_PCT=0.5`、`TOLERANCE_PRECISION_VETO` |
| 6 | 容差同分时按列表顺序 | `_tie_pick` 先比电压、再比容差（等于要求 > 更紧 > 未标注），最后比包装 | `_tie_pick` |
| 7 | 选中的值宽于要求时报告里看不出 | `suggestions[].note` 追加「容差 1% 宽于要求 0.1%（10×）」 | `suggest_sku` |
| 8 | `cart_view` 只回件数，要看明细得用 CDP 直读 | 回 `rows[]`（`index/title/spec/price/price_text/qty/url`）＋ `rows_page/pages_read/rows_degraded/rows_selector` | `_cmd_cart_view`、`_cart_rows`、`_cart_rows_all` |
| 9 | `cart-remove` 行没渲染时报「购物车里没有可删除的条目」；只处理当前页 | 先等权威计数 `全部商品(N)`（≤15 s），三种情况分开报；**必须 `after < before` 才算 `verified`**；删完本页自动翻页 | `_cmd_cart_remove`、`_wait_cart_count`、`_cart_next_page` |
| 10 | 站点重启后批量状态靠内存，`done/total` 归零，分不清是否还在跑 | `__init__` 装载进度：`running=False`、`stopped_reason="server restarted"`、保留 `done/total`，并给 `has_progress/resumable` | `_restore_batch_from_progress` |
| 11 | 模拟人手点击按贝塞尔逐点移动 15–30 步，详情页每次 `mouse.move` ≈1 s → **单行 24 s 全耗在这里** | 单次移动 + 点击；实测点击 24 s → 0.08–0.16 s | `_human_click` |
| 12 | 加购证据图 png 编码 2–3 s/张 | 批量用 `jpeg(q=45)`，约 0.3 s/张 | `capture_viewport_screenshot(jpeg=True)` |
| 13 | 搜索被风控扣住时，连「清点/清理购物车」也返回 `VERIFICATION_REQUIRED` | `cart-view` / `cart-remove` 走白名单，阻断态下照常执行（两者自己导航 `cart.taobao.com`，进页后仍查验证弹层） | `SessionHandler._CART_SAFE_CMDS` |
| 14 | CDP 附着时取 `pages[0]`，浏览器里有残留空白标签就抓到错页 → 永远「首屏没卡」 | `_pick_page`：优先取 URL 含 `taobao.com`/`tmall.com` 的标签；只选不关用户的标签，多标签时打印清单 | `browser_adapter._pick_page` |
| 15 | 加购成功判定「任一命中即成功」且**点了就算成功**：`querybagcount`（查数量）也被当加购证据，10 行 ok 里 5 行购物车里没有 | 只认三处强信号（页面提示/角标 +1/**加购接口** 2xx）；查询接口排除；兜底改 `CART_UNCONFIRMED` → `uncertain`；收尾对账 ok 行逐条跟购物车行比对，找不到就降级 | `_on_resp`、`_cmd_cart_add`、`taobao_runner._reconcile_cart` |
| 16 | 精密档容差「没标注就放行」：SKU 只写 24.9K 实际 ±1% 照买（#70 形态） | 最终选中的 SKU **组合文本**读不到容差 → 不自动买（`unresolved`）；组合级判断避免误杀「容值/容差独立组」 | `suggest_sku`（`_precision` 收口） |
| 17 | 介质（C0G/NP0 vs X7R/X5R）完全不参与匹配：1nF C0G 5% 买成 X7R（#8） | BOM 解析 `params.dielectric`（NP0/COG/NPO 归一 C0G）；候选标题/SKU 明确写了别的介质 → veto；没写 → 挂「介质未标注」备注 | `engine.parse_dielectric`、`_dielectric_verdict`、`_sku_veto`、`_candidate_veto` |
| 18 | 验证等待期 cart 命令把人工正在解的验证页导航走；进度恢复无条件写「server restarted」；tmd 惩罚帧残留导致永等 | cart 命令阻断态走临时标签页（`_cart_page`）；已跑完的批不写重启（P2-4）；主文档有商品卡则 tmd 帧按残留处理（P2-1） | `SessionHandler._cart_page`、`_restore_batch_from_progress`、`_looks_access_blocked` |
| 19 | 空购物车被报成 `rows_degraded=true`（读不到），且每次空车 cart-view 白等 15 s 权威计数 | 空车先认「空空如也」文案：`cart_empty=true`、`rows_degraded=false`、计数直接 0；实测 11.5 s → 5.1 s | `_CART_ROWS_JS`、`_cart_is_empty`、`_wait_cart_count` |
| 20 | 演练（dry-run）也跑收尾对账，把不点加购的演练行全降级成 uncertain | dry-run 跳过 `_reconcile_cart`，演练行保持 ok（`dry_run=true`） | `_run_batch` finally |
| 21 | 加购实际成功但 550 ms 观察窗没抓到回执 → 报 uncertain，收尾对账又只查 ok 行，真成功反而无人认 | 无信号时补看 1.2 s（角标 +1 也算证据）；对账额外核对 uncertain 行，找到就记 `cart_found=true` 并在 message 里补证 **但不改状态** | `_cmd_cart_add`、`_reconcile_cart` |
| 22 | 400 参数错误的响应体没有 `code`，前端只能显示「未知错误」 | 400 统一带 `code`（缺省 `BAD_REQUEST`） | `server.py` 异常分支 |
| 23 | 「查看购物车」只在加购成功后动态出现，平时没有入口；且容器不存在时点它会 JS 崩 | 购物车维护区加常驻「查看购物车明细」按钮；`execCart` 容器缺失时兜底写入维护区 | `index.html`、`app.js::execCart` |
| 24 | 加购点击偶尔「静默无效」：按视口坐标硬点，按钮在折叠线以下/被弹层盖住时点到别处 | `_human_click` 三守卫：先 `scroll_into_view`、判 `is_enabled`、用 `elementFromPoint` 命中测试（换 4 个候选点），命中不上返回 False；调用方 Esc 收浮层后重点一次，仍不行报 `CART_CLICK_BLOCKED`（retryable） | `browser_adapter._human_click`、`taobao._cmd_cart_add` |
| 25 | 点击「零效果」（无请求/无角标/无 toast）时只判 uncertain，白丢一行 | 零证据时**安全补点一次**（此时车里必然没变化，不会重复下单），1.5 s 后复检 xhr/角标 | `_cmd_cart_add` |
| 26 | 加购无回执一律 uncertain，其实失败的也没人补，成功的也没人认 | uncertain 时查购物车：有 → 记 ok（`via=cart_check`）；没有且明细可信 → **重试一次**；明细降级 → 保持 uncertain 不下结论 | `taobao_runner._cart_contains`、`_add_line_to_cart` |
| 27 | 限流前兆（加购耗时爬升）无预警；SKU 被页面重置导致「请选择规格」；购物车满误判 | 加购 >5 s 连续 2 行 → 预热降温 120 s；「请选择规格」→ 重放 SKU 选择再点一次；「已满/上限」→ `CART_FULL` 不重试 | `_run_batch`、`_cmd_cart_add` |
| 28 | 电解电容/电感只看电气参数：25V 220µF 6×8mm 买成 80V 220µF 13×20mm、15µH 7.1×6.6 焊盘买成 13.5×12.6mm —— 参数全对但**装不上** | 新增 `_size_verdict`：从封装（`CAP-TH_BD6.3`/`IND-SMD_L7.1-W6.6`）和 MPN（`...M6x8`）抠尺寸，候选尺寸超 1.35 倍即 veto；电解再判耐压，多档电压标题（10/16/25/35V）不按首个数字误杀，改由 SKU 实际选中值判 | `_size_verdict`、`_size_pairs`、`_candidate_veto`、`_sku_veto` |
| 29 | 连接器只比间距针数：XH 4P 买成 KF2510 4P（同为 2.54 但外壳互不兼容） | 新增 `_conn_family`（XH/PH/ZH/SH/EH/GH/KF2510/KF128-2EDG/VH/MX1.25），BOM 与候选中都认得出且不同族 → veto；KF128 与 2EDG 归同族放行 | `_conn_family`、`_CONN_FAMILY_PATTERNS` |
| 30 | 尺寸校验只在标题层做不够：`80v220uf 体积：13*20mm` 这种只写在 SKU 选项里 | `_sku_veto` 同样跑尺寸/耐压与系列校验 | `_sku_veto` |

**v0.7（2026-11-07 深夜）**：#15–#18 来自独立评审的问题报表（P0-1/3/4、P2-1/3/4）；前端已接 `rows` 与错误码。

**v0.5 遗留的三条（§17 末尾「还没做的」）在 v0.6 全部关闭**：空结果快返（#3）、
`cart_view` 行明细（#8）、`cart-remove` 误报（#9）。

---

## 19. 执行层契约（v0.6）

### 19.1 `wait_for_cards` 四态

| 返回 | 含义 | 上层动作 |
|---|---|---|
| `>= 0` | 已有商品卡，值是耗时 ms | 直接抽候选 |
| `-1` | 没卡、也没明确空结果（慢渲染/渲染丢失） | 先重导航一次；仍 `-1` 才换词 |
| `-2` | 页面明确写「没有找到 / 暂无相关 / 0 个宝贝」 | 换词，**不要**再叠 `wait_for_results()` |
| `-3` | 结果接口被风控中间页 `_____tmd_____` 接管（页面只显示「加载中…」） | **先刷新自恢复**（`_recover_cards` → `recover_from_verification`，2 次）；刷出商品就继续，仍无商品才 `VerificationRequired` 等人工；**不许换词硬撞** |

### 19.2 `search` 返回的 `attempts`

```json
{"keyword": "0603 10kΩ 电阻", "used_keyword": "0603 10k 电阻",
 "attempts": [{"keyword": "0603 10kΩ 电阻", "found": 0},
              {"keyword": "0603 10k 电阻", "found": 20, "ms": 2140}]}
```

### 19.3 `cart-view` 返回结构（I4）

```json
{"cart_item_count": 6, "cart_empty": false, "rows_page": 6, "rows_total": 6, "pages_read": 1,
 "rows_degraded": false, "rows_selector": "[class*=\"cartItemInfoContainer\"]",
 "rows": [{"index": 0, "title": "原装 0603贴片电容 10nF(103) ±10% 50V X7R",
           "spec": "封装规格：100只/包 颜色分类：30PF",
           "price_text": "券后价 ￥ 0 . 97 ￥ 1", "price": 0.97, "qty": 1,
           "url": "https://item.taobao.com/item.htm?id=..."}]}
```

`cart_empty: true` 表示页面明确说「购物车空空如也」——这是**正常状态**，不是降级：
此时 `rows_degraded=false`、`cart_item_count=0`、`rows=[]`，前端显示「购物车是空的」。

`rows_degraded: true` 表示一个行容器都没找到**且页面也没说空车**（DOM 改名），此时**必须**按「读不到」
处理，不能当成空车。`rows_page` 是**本页**行数，`rows_total` 是跨页合计（P1-3 口径修正）。

### 19.3.1 加购确认与收尾对账（P0-1）

- `cart-add` 成功判定只认三处**强信号**：页面成功提示 / 购物车角标 +1 /
  **加购接口**请求 2xx（`addcart`/`addCartSuccess` 类 URL）。
  `querybagcount` 等**查询**接口不算证据；三处都没有 → `CART_UNCONFIRMED`
  （批量侧记 `uncertain`，不再换候选重试，因为已经点过会重复加购）。
  「点了就算成功」的 `via="click"` 兜底已删除。
- 批量收尾**对账**：读一次 `cart-view`，把每个 `ok` 行的商品和购物车实际行比对。
  比对**不靠标题相似度**，靠规格原子（§19.3.2）；找不到 → 降级 `uncertain` 并写
  「收尾对账：购物车里没找到该商品」；结果记进 `batch.reconcile`
  （`checked/matched/downgraded/cart_rows/cart_degraded/anomaly`）。
  明细 `degraded` 时不降级（宁可不冤枉），只记 skipped。
- **读车异常保护**：整批都没匹配上（≥5 行检查且 0 命中）、匹配率过低（≥8 行且 <35%）、
  购物车读成空行、或抓到的行全没有标题时，判定为「读车异常」而不是「N 行同时失败」：
  `ok` 行**保持 ok** 并打 `cart_unverified=true`，message 写明「未核实，请人工核对」，
  `reconcile.anomaly=true` + `anomaly_reason`。理由：18 行同时加购失败的概率远低于
  「车被清空 / 页面没渲染完 / 选择器抓空」，逐行降级会把「买成了」和「没买成」显示成一个样。
- `uncertain` 行也会对账：找到就记 `cart_found=true`、message 补「购物车里找到该商品」
  （**状态仍是 uncertain**，不伪装成 ok）；统计在 `batch.reconcile.uncertain_found`。
- 加购确认的观察窗是 550 ms，没信号时再补看 1.2 s（角标 +1 也作数）——
  慢页面上的加购请求常常 1~2 s 才回来。
- 演练（`dry_run=true`）**不跑**收尾对账：本来就不点加购，车上当然找不到。
- 演练结论**绝不落盘**：`_save_progress()` 见到 `dry_run` 直接 return（`stop_batch` / 会话停止
  都会调它）。演练的 `ok` 意思是「选型+闸门都过了」，不是「购物车里有了」；写进
  `batch-progress.json` 之后，页面会显示「已加购 N 行」、下次「继续加购」把它们当已买跳过、
  「删除上一批」还会拿这份名单去删车 —— 等于把演练当成真单（2026-11-16 实测踩到）。
  旧版本残留的带 `dry_run` 标记的进度在装载时也会被忽略。
- 加购**无回执**时不再直接落 uncertain：先查购物车三态判定 —— 有（记 ok，
  `cart_verified_via="cart_check"`）/ 没有（明细可信才重试一次）/ 读不到（保持 uncertain）。
  **购物车读成 0 行也算「读不到」**（返回 `None` 而不是 `False`）：读车失败时重试等于重复下单。
- 点击守卫：`CART_CLICK_BLOCKED` = 按钮禁用或被弹层遮挡（Esc 后重点一次仍失败），
  此时**什么都没加进去**，允许换候选；`CART_FULL` 不重试。

### 19.3.2 「车里的是不是这件」怎么判（标题比对口径）

搜索结果标题和购物车标题经常被打乱、截断、加包装后缀；而 `0603 10kΩ` 与 `0603 1kΩ`
的字符串相似度高达 **0.96** —— 纯相似度必然误判。所以先抽**规格原子**再比：

| 原子 | 抽取方式 | 冲突时 |
|---|---|---|
| 料号 | 保留分隔符后 `[a-z0-9]{4,}` 且同时含字母+数字；剔掉封装代号与纯参数值（`100nf`/`100v`/`10k`） | 交集非空 → 直接判同一件 |
| 封装 | `0603/0805/SOT-23/SOD-123/SMA/QFN…` | 两边都有且无交集 → 判不同 |
| 阻值 | `10kΩ / 4K7 / 2R2 / R24 / 100R / 裸 10k` | 两边都有且无交集 → 判不同 |
| 容值/感值/耐压/功率/电流/频率/针数/间距 | 数字+单位归一（`100nF` / `15uH` / `50V` / `1/4W` / `6P` / `2.54mm`） | 同上 |
| 介质代码 | `C0G/NP0/X7R/X5R…` 先剔除（否则 `X7R` 会被当成 7Ω） | 不参与 |

判定顺序：料号交集 → 任一单位冲突则否 → 封装冲突则否 → 主规格（阻/容/感）相等则是 →
最后才退回「短标题完整出现在长标题里」（≥6 字）。`_titles_match()` 是唯一入口，
`_cart_contains()`（加购回执补查）与 `_reconcile_cart()`（收尾对账）共用。

### 19.4 `cart-remove` 错误码（I5）

| code | 含义 | 是否删了东西 |
|---|---|---|
| `null` + `verified:true` | `after < before`，删除真实生效 | 是 |
| `null` + 「购物车是空的」 | `before == 0` | 否 |
| `CART_ROWS_NOT_RENDERED` | 角标 >0 但一行都读不到（DOM 改名/未渲染） | 否 |
| `CART_NO_MATCH` | 读到了行，但没有标题含 keyword 的 | 否 |
| `CART_MATCH_FOUND_BUT_NO_ROW` | 标题匹配上了，但行内「删除」按钮没点上 | 否 |
| `CART_DELETE_NOT_EFFECTIVE` | 点了删除，`after` 没变小 | 否（可能部分） |
| `CART_REMOVE_AMBIGUOUS` | 既没给 `keyword` 也没给 `all:true` | 否（拒绝执行） |

### 19.4.1 加购与会话类错误码

| code | 含义 | 上层动作 |
|---|---|---|
| `CART_ADD_REJECTED` | 点击后页面明确提示失败（下架 / 库存不足 / 不能购买 / 请稍后再试） | `retryable: false`，换下一个候选；**不重试同一家** |
| `INVALID_SELECTIONS` | `select-sku` 收到的 selections 里有该商品不存在的规格组或值 | 不点加购，回到 SKU 面板重新选 |
| `SESSION_ERROR` | 会话进程已退出、命令通道断开或返回体无法解析 | 批量暂停并落盘，提示重启会话；**不当作该行失败** |

### 19.4.2 只删本批：`cart-remove` 的 `items` 模式

不是「清空购物车」，也不是「按关键字删」，而是**只删上一批加购进去的那些**，
用户自己的商品和更早批次加的一件都不碰。前端按钮叫「删上次加购的」。

```json
{"cmd": "cart-remove", "dry": true,
 "items": [{"item_id": "9513106599", "title": "…", "qty": 2}]}
```

| 环节 | 规则 |
|---|---|
| 目标来源 | `batch-progress.json` 里 **status=ok** 的行；`uncertain` 连加没加上都不确定，不参与删除 |
| 匹配 | 优先按 `candidate.url` 的商品 id 精确匹配；记录没存 url 的老批次退回标题归一化匹配 |
| **数量守卫** | 购物车里这一行的件数 **多于** 记录里的份数 → 说明用户自己也加过同款，**跳过不删**，原因写进 `skip` |
| 同款合并 | 同一商品买过多次时购物车只有一行：第二个目标记为 `merged`，不当成丢件 |
| 两段式 | `dry:true` 只扫描并返回 `delete/skip/merged/missing` 四张清单，一个按钮都不点 |
| 核对 | 删完回读「全部商品(N)」，`after < before` 才算 `verified` |

返回结构：`delete[]`（将删/已删）、`skip[]`（含 reason）、`merged[]`、`missing[]`、
`removed` / `removed_titles` / `before` / `after` / `verified`。
失败码沿用 §19.4：`CART_ROWS_NOT_RENDERED`、`CART_DELETE_NOT_EFFECTIVE`；
本模式独有的 `NO_BATCH_RECORD`（没有任何 ok 行可删）。

> 实测（2026-11-11）：购物车 24 件（含用户自己的鼠标、键盘）→ 删 21 件，
> 24 → 3。剩下 3 件 = 用户 2 件 + 一件**更早批次**加的 0402 100nF（不在本批记录里），
> 正是「只删本批」的预期行为。重复点击第二次返回 0 件待删（幂等）。

### 19.5 批量状态机（I7）

`GET /api/taobao/batch` 关键字段：`running`、`total`、`done`、`has_progress`、
`resumable`、`restored_from_disk`、`stopped_reason`、`finished_at`。

服务重启后：`running=false`、`done/total` 来自 `runs/batch-progress.json`、
`resumable=true`（可直接「继续加购」）。上次**没跑完**才写
`stopped_reason="server restarted"`（P2-4）；已跑完的批 phase 写「上一轮已跑完」。

---

## 20. 已知限制与人工清单（v0.6）

1. **风控不再「一碰就停」（v0.8 规则）。** 2026-11-07 实测：搜索导航约每 10 次触发一次验证；
   触发后结果接口被 `_____tmd_____` 接管，页面只显示「加载中…」。
   **现在的处置顺序**：执行层先刷新自恢复（2 次）→ 批量层立刻重试该行（2 次、间隔 12 s，
   下一行的搜索会再触发一次自恢复）→ 都失败（= 刷新后仍出不来商品）才进入
   「刷新后仍无商品，等人工验证（剩余 Ns）」，最多等 `TAOBAO_VERIFY_WAIT`（默认 1800 s），
   人工解决后从该行重试（最多 `TAOBAO_VERIFY_HUMAN_RETRY`=3 次）。
   **已点过「加入购物车」的行绝不重跑**，避免重复下单。刷新不等于绕过：不自动点验证码、
   不破解、不改指纹，只是把被扣住的那次请求重来一次。
   **等待期间不自动把浏览器窗口翻到屏幕内**（v0.9.1 起，太突兀会打断用户正在做的事）：
   批量状态里挂 `verification: {waiting, reason, total_s, remaining_s, since}`，
   前端据此在页面顶部常驻提示条（含倒计时、标签页标题闪烁），用户点「打开淘宝窗口」
   才移窗口（`POST /api/taobao/window {action:"show"}`），点「我已完成，重新检测」
   立刻复核（`POST /api/session/recheck`）。验证解除后提示条自动消失、标题复原。
2. **节奏参数**：`TAOBAO_LINE_GAP`（默认 1.2 s/行）、`TAOBAO_COOLDOWN_EVERY`（20 行）、
   `TAOBAO_COOLDOWN_SEC`（40 s）。实测 0.7 s/行连跑几十行会被盯上，1.2 s 稳。
3. **精密容差**：BOM 要求 ≤0.5% 而全店只有 ±1%/±5% 时判 `unresolved`（不自动买）。
   想放宽可设 `TAOBAO_TOL_PRECISION_VETO=0`（退回「只降权」）。
4. **购物车分页**：一页 30 件，删除/翻页依赖 `.ant-pagination-next`；
   分页 DOM 改名时只能处理当前页，`pages_touched` 会如实反映点了几页。
5. **顶栏「购物车 N」不可信**：它是懒加载占位，唯一真相是购物车页的 `全部商品(N)`。
6. **加购没有 toast**：详情页点「加入购物车」后页面无任何提示，成功证据是
   顶栏角标增量或网络侧 cart 请求 2xx（`verified_via="xhr"`）。

### 20.1 三条历史 uncertain 的结论（#1 / #25 / #53）

2026-11-07 离线复现（用 `runs/bom-lines-master.json` 的原始行跑 `score_candidates` /
`suggest_sku` / `_candidate_veto`）。结论：**三条都不是规则缺口**，是「那一刻没有候选
过得了标题门槛」（风控期结果接口被接管）。判定结果如下，写进人工清单，不再自动猜。

| # | BOM 行 | 规则侧现状（已复现） | 结论 |
|---|---|---|---|
| 1 | `47uF 10V X5R ±20%`，0805，×3 | 容值/耐压/封装三闸齐全：`0805 47uF 10V` 得分 75.4；`1210 47uF 10V` 判 0（封装不符）；`0805 47uF 16V` 也放行（耐压更高可接受）；耐压组选 10V、容值组选 47uF | **供货现实**：0805 47µF 10V 是稀有组合，多数卖家只有 1210。买不到就是 `failed`，**不许改封装**；要改 1210 或并联，走人工确认 |
| 25 | `B5B-XH-A`，XH2.50mm-5P，×1 | 形态否决生效：`带线/连接线/母头`、`单头渡锡线` 都被 `_candidate_veto` 拒；针数组 `[2P…6P]` 正确选 5P；`pitch_match(2.54, 2.5)=True` | **规则无缺口**。此前 uncertain 是命中不到合适的板端针座；优先 `XH2.54-5P 直插针座`，JST 原装可换国产同规格（同针数同间距同形态） |
| 53 | `AO3400A`，SOT-23，×1 | `params` 为空（BOM 只给型号），只剩「料号 + 封装」两道闸：`AO3400A …` 得分 79.6、`AO3400`（无 A）判 0、`A09T AO3400A` 62.9；型号组 `[AO3400 AO3400A AO3402 AO3407]` 正确选 `AO3400A` | **可自动买**，但必须记一条风险：`BOM 未给 Vds/Id，仅按料号+封装校验`。丝印 `A09T` 是 AO3400A 的真实丝印，不算错料 |

---

## 21. v0.7 → v0.8 变更表（验证策略：先自救，再找人）

> 触发原因：2026-11-07 那轮批量在第 11 行撞上**图片点选验证码**，旧规则「一碰就停」
> 让整批挂了 30 分钟等人 —— 而实际上刷新一次就能继续跑。

| # | 改前 | 改后 | 位置 |
|---|---|---|---|
| 1 | 见验证就 `raise VerificationRequired()`，会话进阻断态、批量挂起等人工 | 先 `recover_from_verification()` 刷新自恢复（默认 2 次）；**刷出商品就继续**，只有刷新后仍无商品才抛 | `browser_adapter.recover_from_verification` |
| 2 | `-3`（`_____tmd_____` 中间页）直接抛异常 | `-3` → `_recover_cards()`：刷新 + 重新数卡，刷出来就当正常结果用 | `browser_adapter._recover_cards`、`search` |
| 3 | `_handle_captcha_if_present` 命中即抛 | 命中先刷新（`recover_from_verification`），失败才抛 | `browser_adapter._handle_captcha_if_present` |
| 4 | 阻断态下任何搜索类命令直接回 `VERIFICATION_REQUIRED` | 先让执行层试着刷新恢复；恢复成功则**照常执行这条命令** | `taobao.SessionHandler.run` |
| 5 | 批量层撞验证先 `wait_verification()` 等人工（最多 1800 s） | 先**立刻重试同一行**（`verification_next_step → auto`，默认 2 次 / 间隔 12 s），用完才等人工 | `taobao_runner.verification_next_step`、`_run_batch` |
| 6 | 「已点过加购的行」与「普通撞验证」共用一条重试路径，靠布尔判断易漏 | 抽成纯函数：`added=True → stop`（绝不重跑），`auto_retries`/`human_retries` 两段额度 | `taobao_runner.verification_next_step` |
| 7 | 文案统一写「等待人工过滑块」 | 按实际原因写：「刷新后仍无商品，等人工验证（剩余 Ns）」；暂停原因也带「刷新后仍无商品」 | `_wait_verification_loop`、`_run_batch` |
| 8 | 执行层零测试 | 新增 `test_verification_recovery.py`（26 条断言）：假 Page 覆盖恢复分支 + 纯函数覆盖重试策略 | `bom-site/test_verification_recovery.py` |

**新增环境变量**：`TAOBAO_RECOVER_TRIES`（默认 2）、`TAOBAO_RECOVER_PAUSE`（默认 1.2 s）、
`TAOBAO_VERIFY_AUTO_RETRY`（默认 2）、`TAOBAO_VERIFY_RETRY_GAP`（默认 12 s）、
`TAOBAO_VERIFY_HUMAN_RETRY`（默认 3）。

**没有改变的边界**：不自动点验证码、不破解、不改指纹/代理/频率；刷新只是重发一次被拦住的请求。

---

## 22. v0.8.1 补丁（文档与代码对齐审计）

2026-11-11 对「文档声明 vs 代码实现」做了一次逐条核对，只改文档，不改行为：

| 项 | 发现 | 处理 |
|---|---|---|
| 错误码覆盖 | 代码会返回 `CART_ADD_REJECTED` / `CART_REMOVE_AMBIGUOUS` / `INVALID_SELECTIONS` / `SESSION_ERROR`，文档一处没写 | §19.4 补 `CART_REMOVE_AMBIGUOUS`，新增 §19.4.1 收录另外三个 |
| `cart-view` 示例 | 正文讲了 `cart_empty`，JSON 示例里没有 | 示例补上该字段 |
| §18 变更表 | 30 条编号齐全但顺序错乱（1-13,15-17,28-30,24-27,19-23,18,14） | 按编号重排 |
| `wait_for_cards` 注释 | docstring 写「三态返回」且称 `-3` 必须等人工，与 v0.8 实际行为（先刷新自救）相反 | 代码注释改为四态并说明上层动作 |

**核对结果**：11 个绑定函数全部存在；10 个环境变量全部被读取且默认值与文档一致；
文档里的错误码除 1 个封装名误匹配外全部能在代码中定位。
