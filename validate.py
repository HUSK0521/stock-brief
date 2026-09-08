# -*- coding: utf-8 -*-
"""訊號有效性驗證：訊號出現後，股價實際走了什麼？

    python validate.py [--days 70]

方法：在整個面板上向量化重算每一天的訊號條件，然後看訊號當日之後
1 / 3 / 5 個交易日的報酬，跟「同期間全市場平均」比較。

樣本很小（面板只有 68 個交易日），所以結論只能當作方向性參考，
不能當成統計證據。這一點會直接印在輸出裡，不藏。
"""
import argparse, json, sys
import numpy as np
import pandas as pd

import market
import build_site as bs

WIN = bs.WIN


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=70)
    ap.add_argument("--date", default="2026-09-04")
    ap.add_argument("--json", default="validation.json", help="把結果寫成 JSON 給前端用")
    args = ap.parse_args()

    panel = market.load_panel(args.date, args.days, verbose=False)
    P, I, M = panel["price"], panel["inst"], panel["margin"]

    close = bs._pivot(P, "close")
    chg = bs._pivot(P, "chg")
    vol = bs._pivot(P, "volume")
    val = bs._pivot(P, "value")
    fore, trust = bs._pivot(I, "foreign"), bs._pivot(I, "trust")
    mar, marp = bs._pivot(M, "margin"), bs._pivot(M, "margin_prev")

    fz, tz = bs._z(fore), bs._z(trust)
    mchg = mar - marp
    mz = bs._z(mchg)
    vratio = vol / vol.shift(1).rolling(WIN).mean().replace(0, np.nan)
    turn20 = val.rolling(WIN).median()
    active = (fore.fillna(0) != 0).rolling(WIN).sum()

    # 合格母體：跟正式訊號用同一套流動性與參與度門檻
    ok = (turn20 >= bs.MIN_TURNOVER) & (active >= bs.MIN_ACTIVE_DAYS) & close.notna()

    # 未來報酬（以收盤對收盤計算）
    fwd = {h: (close.shift(-h) / close - 1) * 100 for h in (1, 3, 5)}

    KEYS = {"retail 散戶接刀":"retail_catch", "retail 反向(法人進)":"inst_in",
            "diverge 法人對做":"diverge", "z 外資大買":"fz_buy", "z 外資大賣":"fz_sell",
            "vol 量能暴增":"vol_up", "vol 量能極縮":"vol_dn"}
    conds = {
        "retail 散戶接刀": (fore < 0) & (mchg > 0) & (mz >= 1.0) & (fz <= -1.0),
        "retail 反向(法人進)": (fore > 0) & (mchg < 0) & (mz <= -1.0) & (fz >= 1.0),
        "diverge 法人對做": (fore * trust < 0) & (fz.abs() >= 0.8) & (tz.abs() >= 0.8),
        "z 外資大買": (fz >= 2) & (fz <= bs.Z_CAP),
        "z 外資大賣": (fz <= -2) & (fz >= -bs.Z_CAP),
        "vol 量能暴增": vratio >= 2.5,
        "vol 量能極縮": vratio <= 0.4,
    }

    base = {h: fwd[h].where(ok).stack().mean() for h in (1, 3, 5)}
    n_base = int(ok.sum().sum())

    print("=" * 74)
    print(f"訊號有效性驗證　{close.index.min()} ~ {close.index.max()}　"
          f"{len(close)} 個交易日　合格樣本 {n_base:,} 個(股,日)")
    print("=" * 74)
    print(f"{'全市場基準':<22}{'N':>8}  " + "  ".join(f"{'+'+str(h)+'D':>8}" for h in (1, 3, 5)))
    print(f"{'（合格母體平均）':<20}{n_base:>8}  " + "  ".join(f"{base[h]:>+8.2f}" for h in (1, 3, 5)))
    print("-" * 74)
    print(f"{'訊號':<22}{'N':>8}  " + "  ".join(f"{'+'+str(h)+'D':>8}" for h in (1, 3, 5))
          + "   超額(+5D)")
    print("-" * 74)

    rows = []
    for name, c in conds.items():
        m = (c & ok).fillna(False).infer_objects(copy=False).astype(bool)
        n = int(m.sum().sum())
        if n == 0:
            print(f"{name:<22}{0:>8}   —")
            continue
        r = {h: fwd[h].where(m).stack().mean() for h in (1, 3, 5)}
        excess = r[5] - base[5]
        rows.append({"key": KEYS.get(name, name), "label": name, "n": n,
                     "r1": round(r[1], 3), "r3": round(r[3], 3), "r5": round(r[5], 3),
                     "excess": round(excess, 3)})
        print(f"{name:<22}{n:>8}  " + "  ".join(f"{r[h]:>+8.2f}" for h in (1, 3, 5))
              + f"   {excess:>+7.2f}")

    print("-" * 74)
    with open(args.json, "w", encoding="utf-8") as fh:
        json.dump({"from": str(close.index.min()), "to": str(close.index.max()),
                   "days": len(close), "n_base": n_base,
                   "base": {str(h): round(base[h], 3) for h in (1, 3, 5)},
                   "rules": rows}, fh, ensure_ascii=False, indent=1)
    print(f"\n-> {args.json}")

    print("\n【怎麼讀這張表】")
    print("  · 數字是訊號出現後 N 個交易日的平均報酬（%），對照組是同期間全市場合格股票。")
    print("  · 「超額」為正 = 訊號出現後表現比大盤好；為負 = 比大盤差（反向訊號也是資訊）。")
    print("\n【這份驗證的限制 —— 請務必連同結論一起看】")
    print(f"  · 只有 {len(close)} 個交易日，且期間大盤是上漲的，樣本嚴重不足。")
    print("  · 沒有考慮手續費、交易稅、滑價，實際執行會比這裡差。")
    print("  · 同一檔股票在相鄰幾天可能重複觸發，樣本不獨立，會高估顯著性。")
    print("  · 這不是回測，只是條件發生後的平均走勢，沒有部位管理與停損。")
    print("  · 要當作有意義的證據，至少需要數年資料與逐筆交易模擬。")


if __name__ == "__main__":
    main()
