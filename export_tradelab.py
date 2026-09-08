# -*- coding: utf-8 -*-
"""把全市場面板整理成 TradeLab 前端要的資料 -> tradelab.json

    python export_tradelab.py [--date YYYY-MM-DD] [--top 400]

所有數字都來自真實資料（證交所 / 櫃買 / FinMind），沒有捏造。
拿不到的欄位一律留 null，由前端標示，不填假值。
"""
import argparse, json, os, sys
from datetime import datetime

import numpy as np
import pandas as pd
import requests

import market
import taifex
import build_site as bs

OUT = "tradelab.json"
FEATURED = ["2330", "2317", "2454", "2382", "2603", "2881"]


def pct_rank(series, value):
    """value 在 series 裡的百分位（0-100）。"""
    s = series.dropna()
    if len(s) < 5 or pd.isna(value):
        return None
    return float((s <= value).mean() * 100)


def clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))


def build_regime(idx):
    """把指數/廣度/量能歷史壓成一組 0-100 的分數與一個市場狀態。

    每個分項都寫得出「為什麼是這個數字」，不是黑箱。
    """
    idx = idx.sort_values("date").reset_index(drop=True)
    px = idx["index"]
    ret = px.pct_change()
    ma20, ma60 = px.rolling(20).mean(), px.rolling(60).mean()
    last = len(px) - 1

    # Trend：站上均線 + 均線本身在上升
    above20 = px.iloc[last] > ma20.iloc[last] if pd.notna(ma20.iloc[last]) else None
    above60 = px.iloc[last] > ma60.iloc[last] if pd.notna(ma60.iloc[last]) else None
    slope20 = (ma20.iloc[last] / ma20.iloc[last - 5] - 1) * 100 if last >= 5 and pd.notna(ma20.iloc[last - 5]) else None
    trend = 50.0
    if above20 is not None:
        trend += 15 if above20 else -15
    if above60 is not None:
        trend += 15 if above60 else -15
    if slope20 is not None:
        trend += clamp(slope20 * 8, -20, 20)
    trend = clamp(trend)

    # Momentum：5 日與 20 日報酬的百分位
    r5 = (px.iloc[last] / px.iloc[last - 5] - 1) * 100 if last >= 5 else None
    r20 = (px.iloc[last] / px.iloc[last - 20] - 1) * 100 if last >= 20 else None
    hist5 = px.pct_change(5) * 100
    mom = pct_rank(hist5, r5) if r5 is not None else 50.0
    mom = clamp((mom or 50) * 0.7 + (50 + clamp((r20 or 0) * 3, -50, 50)) * 0.3)

    # Breadth：近 5 日上漲家數占比
    br_series = idx["adv"] / (idx["adv"] + idx["dec"]).replace(0, np.nan)
    breadth = clamp(float(br_series.tail(5).mean() * 100)) if br_series.notna().any() else 50.0

    # Volume：成交金額相對 20 日均值
    tv = idx["turnover"]
    vratio = tv.iloc[last] / tv.tail(21).head(20).mean() if tv.notna().sum() > 21 else None
    volume = clamp(50 + (vratio - 1) * 100) if vratio else 50.0

    # Liquidity：成交金額在 60 日分布中的位置
    liquidity = pct_rank(tv, tv.iloc[last]) or 50.0

    # Volatility：20 日報酬標準差的百分位（數字越高＝波動越大）
    vol20 = ret.rolling(20).std() * 100
    volatility = pct_rank(vol20, vol20.iloc[last]) or 50.0

    comps = {"Trend": trend, "Momentum": mom, "Breadth": breadth,
             "Volume": volume, "Liquidity": liquidity, "Volatility": volatility}

    # 狀態判定：方向類分項的一致程度
    directional = [trend, mom, breadth]
    avg = sum(directional) / 3
    spread = max(directional) - min(directional)
    if spread > 38:
        state = "TRANSITION"
    elif avg >= 62:
        state = "BULL TREND"
    elif avg <= 38:
        state = "BEAR TREND"
    else:
        state = "RANGE"
    # Confidence：分項越一致、越遠離中性，信心越高
    confidence = clamp(round(100 - spread * 1.1 + abs(avg - 50) * 0.5))

    return {"state": state, "confidence": round(confidence),
            "components": {k: round(v) for k, v in comps.items()},
            "detail": {
                "above_ma20": bool(above20) if above20 is not None else None,
                "above_ma60": bool(above60) if above60 is not None else None,
                "ret5": round(r5, 2) if r5 is not None else None,
                "ret20": round(r20, 2) if r20 is not None else None,
                "vol_ratio": round(vratio, 2) if vratio else None,
                "breadth_pct": round(breadth, 1),
            }}


def panel_ohlc(panel, codes, n=70):
    """從面板取 OHLC。證交所行情表本來就有開高低，不必再去 FinMind 逐檔抓。

    回傳緊湊格式：共用一份日期陣列，每檔只存 [起始索引, [[o,h,l,c,量(張)], ...]]。
    每根 K 棒都重複存日期字串與鍵名的話，400 檔會膨脹到 1.9MB；
    改成這樣約可壓到三分之一，對手機載入差很多。
    """
    P = panel["price"]
    if "open" not in P:
        return [], {}
    d = P[P.code.isin(codes)].dropna(subset=["open", "high", "low", "close"])
    dates = sorted(d.date.unique())[-n:]
    ix = {v: i for i, v in enumerate(dates)}
    out = {}
    for code, g in d.sort_values("date").groupby("code"):
        g = g[g.date.isin(ix)]
        if g.empty:
            continue
        start = ix[g.date.iloc[0]]
        out[code] = [start, [[round(r.open, 2), round(r.high, 2), round(r.low, 2),
                              round(r.close, 2), int(r.volume / 1000)]
                             for r in g.itertuples()]]
    return dates, out


def fetch_ohlc(codes):
    """精選個股的 OHLC（畫 K 線用）。面板只存收盤價，這裡另外跟 FinMind 拿。"""
    tok = os.environ.get("FINMIND_TOKEN")
    if not tok:
        return {}
    out = {}
    for c in codes:
        try:
            r = requests.get("https://api.finmindtrade.com/api/v4/data", timeout=40,
                             params={"dataset": "TaiwanStockPrice", "data_id": c,
                                     "start_date": "2026-05-01", "token": tok}).json()
        except Exception:
            continue
        d = r.get("data") or []
        if not d:
            continue
        out[c] = [{"d": x["date"], "o": x["open"], "h": x["max"],
                   "l": x["min"], "c": x["close"], "v": x["Trading_Volume"]}
                  for x in d][-70:]
    return out


WIN_R = 20


def regime_with_futures(idx, tfx):
    """現貨 Regime 加上期貨/選擇權的兩個分項，並在兩邊背離時明講。"""
    r = build_regime(idx)
    extra = futures_components(tfx)
    r["components"].update(extra)
    if "Positioning" in extra:
        # 現貨趨勢強、但法人期貨部位偏空（或反之）＝值得注意的背離
        gap = r["components"]["Trend"] - extra["Positioning"]
        r["diverge"] = {"trend": r["components"]["Trend"],
                        "positioning": extra["Positioning"], "gap": round(gap)}
        if abs(gap) >= 40:
            r["confidence"] = max(0, r["confidence"] - 15)
    return r


def regime_history(idx, n=60):
    """逐日重算 Regime，讓使用者看得到市場狀態怎麼變化的。"""
    out = []
    for i in range(len(idx) - n, len(idx)):
        if i < 61:
            continue
        sub = idx.iloc[:i + 1]
        r = build_regime(sub)
        out.append({"d": sub["date"].iloc[-1], "s": r["state"],
                    "c": r["confidence"], "t": r["components"]["Trend"],
                    "b": r["components"]["Breadth"], "m": r["components"]["Momentum"]})
    return out


def build_futures(tfx, n=60):
    """期交所資料整理成前端要的形狀，並算出兩個現貨看不到的維度。"""
    if not tfx or tfx["fut"].empty:
        return None
    fut = tfx["fut"].drop_duplicates("date", keep="last").sort_values("date")
    pc = tfx["pc"].drop_duplicates("date", keep="last").sort_values("date")
    fi = tfx["futinst"]
    # 外資的期貨未平倉淨額——台灣交易者最看重的法人部位指標
    fgn = (fi[fi.who.str.contains("外資", na=False)]
           .drop_duplicates("date", keep="last").sort_values("date"))
    last = fut.iloc[-1]
    out = {
        "close": round(float(last.close), 0), "chg": round(float(last.chg), 0),
        "oi": int(last.oi) if pd.notna(last.oi) else None,
        "vol": int(last.vol) if pd.notna(last.vol) else None,
        "hist": [{"d": r.date, "c": float(r.close), "oi": (None if pd.isna(r.oi) else int(r.oi))}
                 for r in fut.tail(n).itertuples()],
    }
    if not fgn.empty:
        out["fgnNetOi"] = int(fgn.net_oi.iloc[-1])
        out["fgnNetTrade"] = int(fgn.net_trade.iloc[-1])
        out["fgnHist"] = [{"d": r.date, "v": int(r.net_oi)} for r in fgn.tail(n).itertuples()]
    inst_last = fi[fi.date == fi.date.max()]
    out["inst"] = [{"who": r.who, "oi": int(r.net_oi), "trade": int(r.net_trade)}
                   for r in inst_last.itertuples()]
    if not pc.empty:
        p = pc.iloc[-1]
        out["pcVol"] = float(p.pc_vol) if pd.notna(p.pc_vol) else None
        out["pcOi"] = float(p.pc_oi) if pd.notna(p.pc_oi) else None
        out["pcHist"] = [{"d": r.date, "v": float(r.pc_oi)}
                         for r in pc.tail(n).itertuples() if pd.notna(r.pc_oi)]
    return out


def futures_components(tfx):
    """把期貨/選擇權壓成兩個 0-100 分項。

    Positioning：外資期貨未平倉淨額在自身分布中的位置（越高＝法人越偏多）
    Sentiment  ：選擇權 P/C 未平倉比的反向百分位（P/C 越高＝避險越重＝情緒越差）
    """
    if not tfx or tfx["futinst"].empty:
        return {}
    out = {}
    fi = tfx["futinst"]
    fgn = (fi[fi.who.str.contains("外資", na=False)]
           .drop_duplicates("date", keep="last").sort_values("date"))
    if len(fgn) >= 20:
        out["Positioning"] = round(pct_rank(fgn.net_oi, fgn.net_oi.iloc[-1]) or 50)
    pc = tfx["pc"].drop_duplicates("date", keep="last").sort_values("date")
    if len(pc) >= 20 and pc.pc_oi.notna().sum() >= 20:
        out["Sentiment"] = round(100 - (pct_rank(pc.pc_oi, pc.pc_oi.iloc[-1]) or 50))
    return out


def load_validation():
    """訊號的實測超額報酬，讓前端可以把「這個訊號歷史上準不準」顯示出來。"""
    try:
        with open("validation.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-09-04")
    ap.add_argument("--top", type=int, default=400)
    ap.add_argument("--days", type=int, default=70)
    ap.add_argument("--idx-days", type=int, default=180,
                    help="指數歷史天數。Regime 需要 60 日均線暖身，抓短了狀態色帶會只有幾格")
    args = ap.parse_args()

    print("載入全市場面板…", file=sys.stderr)
    panel = market.load_panel(args.date, args.days, verbose=False)
    dates = sorted(panel["price"].date.unique())
    asof = max(d for d in dates if d <= args.date)

    print("載入期交所資料…", file=sys.stderr)
    try:
        tfx = taifex.load(args.date, args.days)
    except Exception as e:
        print(f"  期交所抓取失敗: {e}", file=sys.stderr)
        tfx = None

    print("載入指數與市場廣度…", file=sys.stderr)
    idx = market.index_history(args.date, args.idx_days)
    if idx.empty:
        sys.exit("沒有指數資料")
    idx = idx[idx.date <= asof]

    df = bs.compute(panel, asof)
    print(f"  {len(df):,} 檔", file=sys.stderr)

    # 產業分類
    industry = {}
    tok = os.environ.get("FINMIND_TOKEN")
    if tok:
        try:
            info = pd.DataFrame(requests.get(
                "https://api.finmindtrade.com/api/v4/data", timeout=60,
                params={"dataset": "TaiwanStockInfo", "token": tok}).json().get("data") or [])
            if not info.empty:
                industry = (info.drop_duplicates("stock_id")
                            .set_index("stock_id")["industry_category"].to_dict())
        except Exception as e:
            print(f"  產業分類抓取失敗: {e}", file=sys.stderr)

    # Signal Score：把幾個面向壓成 0-100，給 Scanner 排序用
    px = bs._pivot(panel["price"], "close")
    ret20 = (px.iloc[-1] / px.shift(20).iloc[-1] - 1) * 100
    df["ret20"] = ret20.reindex(df.index)
    df["industry"] = pd.Series({c: industry.get(c, "") for c in df.index})
    rs = df["ret20"].rank(pct=True) * 100          # 相對強弱
    fl = df["fz"].rank(pct=True) * 100             # 法人流向
    vr = df["volRatio"].rank(pct=True) * 100       # 相對量能
    mo = df["chg"].rank(pct=True) * 100
    df["rs"] = rs
    df["score"] = (rs.fillna(50) * .35 + fl.fillna(50) * .3
                   + vr.fillna(50) * .2 + mo.fillna(50) * .15)

    # 當日買賣超在「自己前 20 天」中的名次（1 = 20 天內最大）——白話顯示用
    fore = bs._pivot(panel["inst"], "foreign").abs()
    frank = fore.rolling(WIN_R + 1).apply(lambda w: (w[:-1] >= w[-1]).sum() + 1, raw=True)
    df["frank"] = bs.row_of(frank, asof, df.index)

    top = df.sort_values("turnover", ascending=False).head(args.top)

    def f(v, n=2):
        return None if v is None or (isinstance(v, float) and pd.isna(v)) else round(float(v), n)

    stocks = {}
    for code, r in top.iterrows():
        stocks[code] = {
            "name": r["name"], "mkt": r["mkt"], "ind": r["industry"] or "其他",
            "close": f(r.close), "chg": f(r.chg), "vol": f(r.volRatio),
            "turnover": f(r.turnover, 0), "fz": f(r.fz), "tz": f(r.tz),
            "fnet": None if pd.isna(r.fnet) else int(r.fnet),
            "mchg": None if pd.isna(r.marginChg) else int(r.marginChg),
            "ret20": f(r.ret20), "rs": f(r.rs, 1), "score": f(r.score, 1),
            "mz": f(r.mz),
            "frank": None if pd.isna(r.frank) else int(r.frank),
        }

    # 產業彙總（用成交金額當面積，因為公開資料裡沒有現成的市值欄位）
    inds = {}
    for code, s in stocks.items():
        k = s["ind"] or "其他"
        g = inds.setdefault(k, {"n": 0, "turnover": 0.0, "wsum": 0.0, "up": 0, "down": 0})
        g["n"] += 1
        t = s["turnover"] or 0
        g["turnover"] += t
        if s["chg"] is not None:
            g["wsum"] += s["chg"] * t
            g["up" if s["chg"] > 0 else "down"] += 1
    for k, g in inds.items():
        g["chg"] = round(g["wsum"] / g["turnover"], 2) if g["turnover"] else 0
        g.pop("wsum")
        g["turnover"] = round(g["turnover"])

    OHLC_D, OHLC_V = panel_ohlc(panel, list(stocks.keys()))
    hist = idx.tail(60)
    data = {
        "asof": asof,
        "generated": datetime.now().isoformat(timespec="seconds"),
        "market": {
            "index": f(idx["index"].iloc[-1]), "chg": f(idx["index_chg"].iloc[-1]),
            "pct": f(idx["index_pct"].iloc[-1]),
            "turnover": f(idx["turnover"].iloc[-1], 0),
            "adv": int(idx["adv"].iloc[-1]), "dec": int(idx["dec"].iloc[-1]),
            "flat": int(idx["flat"].iloc[-1]),
            "hist": [{"d": r["date"], "i": f(r["index"]), "p": f(r["index_pct"]),
                      "t": f(r["turnover"], 0),
                      "a": int(r["adv"]) if pd.notna(r["adv"]) else 0,
                      "x": int(r["dec"]) if pd.notna(r["dec"]) else 0}
                     for _, r in hist.iterrows()],
        },
        "regime": regime_with_futures(idx, tfx),
        "futures": build_futures(tfx),
        "stocks": stocks,
        "industries": inds,
        "ohlcDates": OHLC_D, "ohlc": OHLC_V,
        "validation": load_validation(),
        "regimeHist": regime_history(idx),
        # 其餘全市場個股：名稱/價格/漲跌 + 成交金額與產業（Heatmap 要用來排面積）
        "search": {c: [df.at[c, "name"], f(df.at[c, "close"]), f(df.at[c, "chg"]),
                       f(df.at[c, "turnover"], 0), df.at[c, "industry"] or "其他"]
                   for c in df.index if c not in stocks},
        "universe": int(len(df)),
    }

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    # 前端輪詢用的小檔：先比對這個，有變才去抓 1MB 的主檔
    with open("meta.json", "w", encoding="utf-8") as fh:
        json.dump({"asof": data["asof"], "generated": data["generated"],
                   "universe": data["universe"]}, fh, ensure_ascii=False)
    print(f"{OUT}  {os.path.getsize(OUT)/1024:,.0f} KB  "
          f"{len(stocks)} 檔 / {len(inds)} 產業 / regime={data['regime']['state']} "
          f"({data['regime']['confidence']})", file=sys.stderr)


if __name__ == "__main__":
    main()
