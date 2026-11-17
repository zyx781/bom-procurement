"""从真机 SKU 观测流水里生成「选型回放」夹具（人工标注期望值）。

用法：python tests/build_sku_replay.py
输入：runs/sku-observations.jsonl（真机跑批时自动追加，一行一家店）
输出：tests/fixtures/sku-replay.json

为什么要有这一天：标题闸门能挡住「标题写错了」的候选，但挡不住「标题对、SKU 选错」——
一个链接挂 20 个档位、店里根本没有 BOM 要的那一档时，能不能拒买全看 SKU 层。
这一层以前没有任何回归保护。
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OBS = ROOT / "runs" / "sku-observations.jsonl"
OUT = ROOT / "tests" / "fixtures" / "sku-replay.json"

# 按「观测流水里的下标」标注期望：pick 为空表示必须拒买，reason 是拒买理由里必须出现的字样。
EXPECT = {
    # 5.1kΩ：整店只有一个「100个」选项，阻值写在标题里 —— 应当放行
    21: {"id": "power-01-5k1-single", "pick": {"颜色分类": "100个"}, "why": "标题写明 5.1kΩ，SKU 只有包装"},
    # 33kΩ：「阻值」组只有「默认项」，值同样在标题里
    22: {"id": "power-02-33k-default", "pick": {"阻值": "默认项"}, "why": "默认项 + 标题 33Kohms"},
    # 22pF：容值/电压都在 SKU 里，选 22PF、50V（≥ 6.3V）
    23: {"id": "power-03-22p", "pick": {"容值": "22PF（220J）NPO", "电压": "±2% 50V（50个）"},
         "why": "容值组正好有 22PF"},
    # 200kΩ：这家只卖 60R~600K 的 0.1% 货，没有 200K
    24: {"id": "power-04-200k-not-sold", "pick": None, "reason": "阻值",
         "why": "整组没有 200kΩ"},
    # 200kΩ：这家只卖 0.68R
    25: {"id": "power-04-200k-wrong-store", "pick": None, "reason": "容差",
         "why": "标题和 SKU 都读不到 200kΩ"},
    # 200kΩ：30K~62K 那家，且带「下单备注型号」型选项 —— 备注型必须否掉
    27: {"id": "power-04-200k-near-miss", "pick": None, "reason": "阻值",
         "why": "只有 33K~62K，另有「下单备注型号」不能买"},
    # 75kΩ：这家只卖 0.15R/0.68R，靠「封装规格」也能凑出一套选择 —— 必须拒
    29: {"id": "power-05-75k-wrong-store", "pick": None, "reason": "没有这一档",
         "why": "只卖低阻值，挑选集齐全也不行"},
    # 330pF：这家只有 NPO/COG，BOM 要 X7R —— 介质不能顶
    31: {"id": "power-07-330p-npo-only", "pick": None, "reason": "介质",
         "why": "介质等级不能互顶"},
    # 330pF：这家 330PF 明写 0402/50V —— 放行
    32: {"id": "power-07-330p-ok", "pick": {"颜色分类": "0402 330PF ±10% 50V（100只）"},
         "why": "值/封装/耐压都在"},
    # 1uF：选项里有「咨询客服/询价」型，且全店读不到 1uF
    34: {"id": "power-09-1u-inquiry", "pick": None, "reason": "询价",
         "why": "询价型 SKU 不能下单"},
    # 39kΩ：这家挂 100R 且是 2010 封装 —— 值、封装双错，必须拒
    40: {"id": "power-11-39k-2010", "pick": None, "reason": "阻值",
         "why": "2010 100R 顶 0402 39kΩ"},
    # 39kΩ：这家 7.5R~22R，没有 39k
    41: {"id": "power-11-39k-not-sold", "pick": None, "reason": "阻值",
         "why": "只卖小阻值"},
}


def main() -> int:
    if not OBS.exists():
        print(f"没有观测流水：{OBS}（先跑一次真机批）")
        return 1
    rows = [json.loads(line) for line in OBS.read_text(encoding="utf-8").splitlines() if line.strip()]
    cases = []
    for idx, exp in EXPECT.items():
        if idx >= len(rows):
            print(f"下标 {idx} 超出流水长度 {len(rows)}")
            return 1
        r = rows[idx]
        cases.append(
            {
                "id": exp["id"],
                "source": "real",
                "note": exp["why"],
                "line_no": r.get("line_no"),
                "title": r.get("title"),
                "line": r.get("line"),
                "sku_options": r.get("sku_options"),
                "expect": {
                    "pick": exp["pick"],
                    "reason_contains": exp.get("reason"),
                },
            }
        )
    OUT.write_text(json.dumps(cases, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已写 {OUT}（{len(cases)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
