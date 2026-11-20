# skills/ — 两层技能

这个仓库的采购能力拆成两层，各自独立、边界清晰：**上层决定买什么，下层负责怎么点。**

```
skills/
├─ bom-procurement/     决策层：规则、硬门槛、采购口径
│   └─ SKILL.md         全部规则的唯一来源（硬门槛 / 容差 / 介电质 / 编号锚定 / 钱闸门）
└─ taobao-search/       执行层：Playwright over CDP
    ├─ SKILL.md         执行层命令协议（search / open / add / cart-view …）
    ├─ requirements.txt 只跑执行层时需要的最小依赖
    └─ scripts/         taobao.py（session 入口）、browser_adapter.py、slider_solver.py …
```

两个目录都是**自包含**的：`SKILL.md` 里不写死本机路径，脚本只从自身目录往上找文件。
本机另有 `skills/bom-procurement/蒸馏方案.md`、`skills/taobao-search/README.md` 两份内部笔记，
已被 `.gitignore` 与发布脚本排除，不随仓库和发布包出去。

## 谁调谁

```
bom-site/engine.py      读 BOM → 逐行给出「买什么」（调用决策层规则）
        ↓
bom-site/taobao_runner.py   逐行编排：搜索 → 评分 → 开详情 → 选 SKU → 加购 → 对账
        ↓
skills/taobao-search/scripts/taobao.py   session 子进程，只认 JSON 命令
```

- 决策层是**纯函数**：不碰浏览器、网络、文件，只回答「这个候选能不能买」「这家店的 SKU 选哪一档」；
- 执行层只管操作页面，不做采购判断；
- 层间的唯一契约是 JSON 命令与结果，所以换页面选择器不会影响规则，改规则也不会碰浏览器代码。

## 路径约定

`bom-site/taobao_runner.py` 里：

```python
REPO = Path(os.environ.get("TAOBAO_SKILL_REPO") or (BASE.parent / "skills" / "taobao-search")).resolve()
```

- `BASE` 是 `bom-site/`，`BASE.parent` 就是仓库根（发布包里是解压目录根）；
- 想指到别处（比如另一份 skill 副本）：设 `TAOBAO_SKILL_REPO`；
- 只换执行层脚本目录：设 `TAOBAO_SKILL_DIR`。

发布包会把整个 `skills/` 打进 zip，所以解压到任何路径都能找到。
