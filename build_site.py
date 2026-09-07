# -*- coding: utf-8 -*-
"""產生全市場早報網站 -> docs/index.html

    python build_site.py [--date YYYY-MM-DD] [--days 70]

跟 universe.py 的差別：那個是一檔一檔打 FinMind（2,300 檔要 31 小時），
這個直接用 market.py 的全市場面板，一次把所有股票的訊號向量化算完。
"""
import argparse, json, os, sys
from datetime import datetime

import numpy as np
import pandas as pd

import market

TEMPLATE = "site.template.html"
OUT = os.path.join("docs", "index.html")
WIN = 20             # z 分數的回看天數
MIN_DAYS = 25        # 樣本不足這個天數的股票不給訊號
MIN_TURNOVER = 1e7   # 20 日成交金額中位數低於 1,000 萬的不給訊號（流動性太差）
MIN_ACTIVE_DAYS = 8  # 20 天內外資實際有進出的天數，太少則 z 分數沒有意義
Z_CAP = 8.0          # z 超過這個值幾乎都是分母趨近 0 造成的假訊號


def _pivot(df, col):
    if df.empty or col not in df:
        return pd.DataFrame()
    d = df.drop_duplicates(["code", "date"], keep="last")
    return d.pivot(index="date", columns="code", values=col).sort_index()


def _z(df, win=WIN):
    """跟自己過去 win 天比的 z 分數（不含當日，避免自己影響自己的基準）。"""
    prev = df.shift(1)
    m, s = prev.rolling(win).mean(), prev.rolling(win).std()
    return (df - m) / s.replace(0, np.nan)


def row_of(df, asof, cols, default=np.nan):
    """取某一天那一列，缺就補 default。"""
    if df.empty or asof not in df.index:
        return pd.Series(default, index=cols)
    return df.loc[asof].reindex(cols)


def _streak_last(df):
    """最後一天為止，連續同方向的天數（正=連續為正，負=連續為負）。"""
    sign = np.sign(df.fillna(0))
    last = sign.iloc[-1]
    out = pd.Series(0, index=df.columns, dtype=float)
    run = last != 0
    for i in range(len(df) - 1, -1, -1):
        match = (sign.iloc[i] == last) & run & (last != 0)
        out += match.astype(int)
        run = run & match
        if not run.any():
            break
    return out * last


def compute(panel, asof):
    P, I, M = panel["price"], panel["inst"], panel["margin"]
    if P.empty:
        sys.exit("沒有行情資料")

    close, vol, chg = _pivot(P, "close"), _pivot(P, "volume"), _pivot(P, "chg")
    fore, trust = _pivot(I, "foreign"), _pivot(I, "trust")
    mar, marp = _pivot(M, "margin"), _pivot(M, "margin_prev")

    for name, df in (("close", close), ("inst", fore)):
        if df.empty or asof not in df.index:
            sys.exit(f"{name} 沒有 {asof} 的資料")

    prev_close = close - chg
    chg_pct = (chg / prev_close.replace(0, np.nan)) * 100
    vol_ratio = vol / vol.shift(1).rolling(WIN).mean().replace(0, np.nan)
    fz, tz = _z(fore), _z(trust)
    mchg = (mar - marp) if not mar.empty else pd.DataFrame()
    mz = _z(mchg) if not mchg.empty else pd.DataFrame()
    tstreak = _streak_last(trust) if not trust.empty else pd.Series(dtype=float)

    # 流動性與參與度門檻。沒有這兩道，冷門股會產生大量假訊號：
    # 外資 20 天裡有 19 天是 0，標準差趨近 0，z 分數就會爆到 -48 這種
    # 不可能的數字，然後佔滿整個排行榜。
    val = _pivot(P, "value")
    turnover20 = val.rolling(WIN).median()
    active = (fore.fillna(0) != 0).rolling(WIN).sum()   # 20 天內外資真的有進出的天數

    enough = (close.notna().sum() >= MIN_DAYS)
    liquid = row_of(turnover20, asof, close.columns) >= MIN_TURNOVER
    participating = row_of(active, asof, close.columns) >= MIN_ACTIVE_DAYS

    row = lambda df: row_of(df, asof, close.columns)

    meta = (P[P.date == asof].drop_duplicates("code", keep="last")
            .set_index("code")[["name", "mkt"]])

    out = pd.DataFrame({
        "close": row(close), "chg": row(chg_pct), "volRatio": row(vol_ratio),
        "fnet": row(fore), "fz": row(fz), "tnet": row(trust), "tz": row(tz),
        "marginChg": row(mchg), "mz": row(mz),
        "tstreak": tstreak.reindex(close.columns),
        "turnover": row(turnover20),
        "enough": (enough.reindex(close.columns).fillna(False)
                   & liquid.fillna(False) & participating.fillna(False)),
    })
    out["name"] = meta["name"].reindex(out.index)
    out["mkt"] = meta["mkt"].reindex(out.index)
    out = out[out.close.notna() & out.name.notna()]
    return out


def signals_for(r):
    """一檔股票的訊號清單。條件與門檻跟 brief.py 保持一致。"""
    sigs = []
    if not r.enough:
        return sigs
    name = r["name"]
    fz, tz, mz = r.fz, r.tz, r.mz
    fnet, tnet, mchg, chg = r.fnet, r.tnet, r.marginChg, r.chg
    k = lambda v: f"{abs(v)/1000:,.0f} 張"

    if pd.notna(fz) and pd.notna(mz) and pd.notna(fnet) and pd.notna(mchg):
        if fnet < 0 and mchg > 0 and mz >= 1.0 and fz <= -1.0:
            sigs.append(dict(score=105 + min(mz, 4), kind="retail",
                head=f"{name}：法人在出，散戶用融資在接",
                body=f"外資賣超 {k(fnet)}（z={fz:+.2f}），同一天融資餘額增加 "
                     f"{abs(mchg):,.0f} 張（z={mz:+.2f}）。當日股價 {chg:+.2f}%。"))
        elif fnet > 0 and mchg < 0 and mz <= -1.0 and fz >= 1.0:
            sigs.append(dict(score=102, kind="retail",
                head=f"{name}：法人在進，散戶在減碼",
                body=f"外資買超 {k(fnet)}（z={fz:+.2f}），融資餘額反而減少 "
                     f"{abs(mchg):,.0f} 張。當日股價 {chg:+.2f}%。"))

    if pd.notna(fz) and pd.notna(tz) and pd.notna(fnet) and pd.notna(tnet):
        if fnet * tnet < 0 and min(abs(fz), abs(tz)) >= 0.8:
            st = int(r.tstreak) if pd.notna(r.tstreak) else 0
            sigs.append(dict(score=100 + min(abs(fz), abs(tz)), kind="diverge",
                head=f"{name} 外資與投信對做",
                body=f"外資{'買' if fnet>0 else '賣'}超 {k(fnet)}（z={fz:+.2f}），"
                     f"投信反向{'買' if tnet>0 else '賣'}超 {k(tnet)}"
                     + (f"（已連 {abs(st)} 天）" if abs(st) >= 2 else "")
                     + "。兩邊都不是散戶，而看法相反。"))

    if pd.notna(fz) and 2 <= abs(fz) <= Z_CAP and pd.notna(fnet):
        sigs.append(dict(score=80 + abs(fz) * 5, kind="z",
            head=f"{name} 外資{'買' if fz>0 else '賣'}超 z={fz:+.2f}",
            body=f"{'買' if fz>0 else '賣'}超 {k(fnet)}，是 20 個交易日內沒出現過的量級。"
                 f"當日股價 {chg:+.2f}%。"))

    st = int(r.tstreak) if pd.notna(r.tstreak) else 0
    if abs(st) >= 5:
        against = (st < 0 and chg > 1) or (st > 0 and chg < -1)
        sigs.append(dict(score=60 + abs(st) + (15 if against else 0), kind="streak",
            head=f"{name} 投信連 {abs(st)} 天{'買' if st>0 else '賣'}超",
            body=f"少見的長連續。當日股價 {chg:+.2f}%"
                 + ("——連續賣超卻收漲，賣壓被吃下來了。" if st < 0 and chg > 1
                    else "——連續買超卻收跌，有人在對面倒貨。" if st > 0 and chg < -1 else "。")))

    vr = r.volRatio
    if pd.notna(vr) and (vr >= 2.5 or vr <= 0.4):
        sigs.append(dict(score=55, kind="vol",
            head=f"{name} 量能{'暴增' if vr>=2.5 else '極縮'}至 20 日均量的 {vr:.2f} 倍",
            body=f"當日股價 {chg:+.2f}%，收 {r.close:,.2f}。"))

    sigs.sort(key=lambda s: -s["score"])
    return sigs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--days", type=int, default=70)
    args = ap.parse_args()
    asof = args.date or datetime.now().strftime("%Y-%m-%d")

    print(f"載入全市場面板（{args.days} 天，截至 {asof}）", file=sys.stderr)
    panel = market.load_panel(asof, args.days, verbose=False)

    # 收盤資料最新的那天當作基準日（今天可能還沒收盤 / 是假日）
    dates = sorted(panel["price"].date.unique())
    if not dates:
        sys.exit("面板是空的")
    real = max(d for d in dates if d <= asof)
    if real != asof:
        print(f"  {asof} 沒有收盤資料，改用 {real}", file=sys.stderr)
    asof = real

    df = compute(panel, asof)
    print(f"  {len(df):,} 檔有行情", file=sys.stderr)

    stocks, total = {}, 0
    for code, r in df.iterrows():
        sigs = signals_for(r)
        total += len(sigs)
        f = lambda v, n=2: (None if pd.isna(v) else round(float(v), n))
        stocks[code] = {
            "name": r["name"], "mkt": r["mkt"], "etf": str(code).startswith("00"),
            "close": f(r.close), "chg": f(r.chg), "volRatio": f(r.volRatio),
            "fz": f(r.fz), "fnet": None if pd.isna(r.fnet) else int(r.fnet),
            "tz": f(r.tz), "tstreak": None if pd.isna(r.tstreak) else int(r.tstreak),
            "marginChg": None if pd.isna(r.marginChg) else int(r.marginChg),
            "signals": [{"kind": s["kind"], "head": s["head"], "body": s["body"],
                         "score": round(s["score"], 1)} for s in sigs],
        }
    print(f"  {total:,} 個訊號", file=sys.stderr)

    data = {"asof": asof, "generated": datetime.now().isoformat(timespec="seconds"),
            "count": len(stocks), "stocks": stocks}

    if not os.path.exists(TEMPLATE):
        sys.exit(f"缺少 {TEMPLATE}")
    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()
    html = html.replace("__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    if "__DATA__" in html:
        sys.exit("樣板替換失敗")

    os.makedirs("docs", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"{OUT}  {os.path.getsize(OUT)/1024:,.0f} KB  {len(stocks):,} 檔 / "
          f"{total:,} 訊號 / 資料日 {asof}", file=sys.stderr)


if __name__ == "__main__":
    main()
