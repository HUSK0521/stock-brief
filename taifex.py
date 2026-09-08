# -*- coding: utf-8 -*-
"""期交所（TAIFEX）公開資料：台指期、三大法人未平倉、選擇權 P/C ratio。

跟證交所一樣是「一天一個檔案涵蓋全部」，不需要逐檔抓，也不需要金鑰。
這一層補上現貨資料沒有的兩個維度：法人的期貨部位，與選擇權反映的市場情緒。

    python taifex.py 2026-09-04
"""
import io, json, os, sys, time
from datetime import datetime, timedelta

import pandas as pd
import requests

CACHE = "cache_mkt"
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.taifex.com.tw/cht/3/futContractsDate"}


def _slash(d):
    return d.replace("-", "/")


def _post_csv(url, data, retries=2):
    """期交所回 Big5 編碼的 CSV。回 HTML 代表參數不對。"""
    for i in range(retries + 1):
        try:
            r = requests.post(url, data=data, timeout=60, headers=UA)
            txt = r.content.decode("big5", "ignore")
            if r.status_code == 200 and "<" not in txt[:40] and "," in txt[:200]:
                # 期交所每列結尾多一個逗號，欄位數比表頭多一欄；
                # 不指定 index_col=False 的話 pandas 會把第一欄當索引，整排位移。
                return pd.read_csv(io.StringIO(txt), index_col=False)
        except Exception:
            pass
        if i < retries:
            time.sleep(1.5)
    return pd.DataFrame()


def _cached(kind, date, fn):
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, f"{kind}_{date}.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return pd.DataFrame(json.load(f))
    df = fn(date)
    if df is None or df.empty:
        return pd.DataFrame()
    with open(p, "w", encoding="utf-8") as f:
        json.dump(df.to_dict("records"), f, ensure_ascii=False)
    return df


# ---------------------------------------------------------------- 各資料源

def _pc_ratio(date):
    df = _post_csv("https://www.taifex.com.tw/cht/3/pcRatioDown",
                   {"down_type": "1", "queryStartDate": _slash(date), "queryEndDate": _slash(date)})
    if df.empty:
        return df
    df.columns = [c.strip() for c in df.columns]
    ren = {"日期": "date", "賣權成交量": "put_vol", "買權成交量": "call_vol",
           "買賣權成交量比率%": "pc_vol", "賣權未平倉量": "put_oi",
           "買權未平倉量": "call_oi", "買賣權未平倉量比率%": "pc_oi"}
    df = df.rename(columns=ren)[[c for c in ren.values() if c in df.rename(columns=ren)]]
    df["date"] = date
    return df


def _fut_daily(date):
    """台指期（TX）日行情。只取一般交易時段的近月。"""
    df = _post_csv("https://www.taifex.com.tw/cht/3/futDataDown",
                   {"down_type": "1", "commodity_id": "TX",
                    "queryStartDate": _slash(date), "queryEndDate": _slash(date)})
    if df.empty:
        return df
    df.columns = [c.strip() for c in df.columns]
    if "交易時段" in df:
        df = df[df["交易時段"].astype(str).str.contains("一般")]
    if df.empty:
        return pd.DataFrame()
    r = df.iloc[0]
    num = lambda v: pd.to_numeric(str(v).replace(",", ""), errors="coerce")
    return pd.DataFrame([{"date": date, "open": num(r.get("開盤價")), "high": num(r.get("最高價")),
                          "low": num(r.get("最低價")), "close": num(r.get("收盤價")),
                          "chg": num(r.get("漲跌價")), "vol": num(r.get("成交量")),
                          "oi": num(r.get("未沖銷契約數"))}])


def _fut_inst(date):
    """三大法人期貨未平倉。只留臺股期貨。"""
    df = _post_csv("https://www.taifex.com.tw/cht/3/futContractsDateDown",
                   {"queryStartDate": _slash(date), "queryEndDate": _slash(date), "commodityId": "TXF"})
    if df.empty:
        return df
    df.columns = [c.strip() for c in df.columns]
    if "商品名稱" in df:
        df = df[df["商品名稱"].astype(str).str.contains("臺股期貨")]
    if df.empty:
        return pd.DataFrame()
    num = lambda v: pd.to_numeric(str(v).replace(",", ""), errors="coerce")
    out = []
    for _, r in df.iterrows():
        out.append({"date": date, "who": str(r.get("身份別", "")).strip(),
                    "net_trade": num(r.get("多空交易口數淨額")),
                    "net_oi": num(r.get("多空未平倉口數淨額"))})
    return pd.DataFrame(out)


def _opt_inst(date):
    """三大法人選擇權未平倉（臺指選擇權，分 CALL / PUT）。"""
    df = _post_csv("https://www.taifex.com.tw/cht/3/callsAndPutsDateDown",
                   {"queryStartDate": _slash(date), "queryEndDate": _slash(date), "commodityId": "TXO"})
    if df.empty:
        return df
    df.columns = [c.strip() for c in df.columns]
    if "商品名稱" in df:
        df = df[df["商品名稱"].astype(str).str.contains("臺指選擇權")]
    if df.empty:
        return pd.DataFrame()
    num = lambda v: pd.to_numeric(str(v).replace(",", ""), errors="coerce")
    return pd.DataFrame([{"date": date, "cp": str(r.get("買賣權別", "")).strip(),
                          "who": str(r.get("身份別", "")).strip(),
                          "net_oi": num(r.get("未平倉口數買賣淨額"))}
                         for _, r in df.iterrows()])


# ---------------------------------------------------------------- 對外

def load(end_date, days=70, verbose=False):
    import market
    out = {"pc": [], "fut": [], "futinst": [], "optinst": []}
    for i, d in enumerate(market.trading_days(end_date, days), 1):
        if verbose:
            print(f"  [{i}/{days}] {d}", file=sys.stderr)
        for key, kind, fn in (("pc", "tfx_pc", _pc_ratio), ("fut", "tfx_fut", _fut_daily),
                              ("futinst", "tfx_futi", _fut_inst), ("optinst", "tfx_opti", _opt_inst)):
            df = _cached(kind, d, fn)
            if not df.empty:
                out[key].append(df)
    return {k: (pd.concat(v, ignore_index=True).sort_values("date") if v else pd.DataFrame())
            for k, v in out.items()}


if __name__ == "__main__":
    end = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    d = load(end, n, verbose=True)
    for k, v in d.items():
        print(f"\n=== {k} ({len(v)} 列) ===")
        if not v.empty:
            print(v.tail(6).to_string(index=False))
