# -*- coding: utf-8 -*-
"""全市場日資料層：直接抓證交所 / 櫃買中心的公開資料。

跟 FinMind 的差別：FinMind 免費層一次只能拿一檔（全市場要 Backer 以上），
證交所自己的端點是「一天一個檔案、涵蓋全市場」，所以成本是
    每個交易日 4 個 request，而不是 每檔股票 6 個 request。

歷史資料不會變，抓過就永久快取在 cache_mkt/。
"""
import json, os, re, sys, time
from datetime import datetime, timedelta

import pandas as pd
import requests

CACHE = "cache_mkt"
UA = {"User-Agent": "Mozilla/5.0"}
TWSE = "https://www.twse.com.tw/rwd/zh"
TPEX = "https://www.tpex.org.tw/openapi/v1"


def _cache_path(kind, date):
    os.makedirs(CACHE, exist_ok=True)
    return os.path.join(CACHE, f"{kind}_{date}.json")


def _get_json(url, params=None, retries=2):
    for i in range(retries + 1):
        try:
            r = requests.get(url, params=params, timeout=60, headers=UA)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        if i < retries:
            time.sleep(1.5)
    return None


def _cached(kind, date, fetch):
    """歷史資料抓過就不再抓（永久快取）。空結果不寫入，留待重試。

    存檔前先濾掉權證：證交所一天回 34,000 列，其中三萬多是權證，
    不濾的話 70 天的快取會膨脹到將近 400MB。
    """
    p = _cache_path(kind, date)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return pd.DataFrame(json.load(f))
    df = fetch(date)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df[df.code.map(is_security)].reset_index(drop=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(df.to_dict("records"), f, ensure_ascii=False)
    return df


def _num(s):
    """把 '1,234' / '--' / '' 轉成數字，轉不動就 NaN。"""
    if s is None:
        return float("nan")
    t = re.sub(r"[,\s%]", "", str(s))
    t = re.sub(r"<[^>]*>", "", t)
    if t in ("", "--", "---", "X", "N/A"):
        return float("nan")
    try:
        return float(t)
    except ValueError:
        return float("nan")


# ---------------------------------------------------------------- 上市

def _twse_price(date):
    d = _get_json(f"{TWSE}/afterTrading/MI_INDEX",
                  {"date": date.replace("-", ""), "type": "ALL", "response": "json"})
    if not d or d.get("stat") != "OK":
        return pd.DataFrame()
    tbl = next((t for t in d.get("tables", [])
                if "每日收盤行情" in str(t.get("title", ""))), None)
    if not tbl:
        return pd.DataFrame()
    f = tbl["fields"]
    ix = {k: f.index(k) for k in ("證券代號", "證券名稱", "成交股數", "成交金額",
                                  "開盤價", "最高價", "最低價", "收盤價", "漲跌價差") if k in f}
    sign_i = f.index("漲跌(+/-)") if "漲跌(+/-)" in f else None
    rows = []
    for r in tbl["data"]:
        chg = _num(r[ix["漲跌價差"]])
        if sign_i is not None and "-" in re.sub(r"<[^>]*>", "", str(r[sign_i])):
            chg = -chg
        rows.append({"code": str(r[ix["證券代號"]]).strip(),
                     "name": str(r[ix["證券名稱"]]).strip(),
                     "volume": _num(r[ix["成交股數"]]),
                     "value": _num(r[ix["成交金額"]]),
                     "open": _num(r[ix["開盤價"]]) if "開盤價" in ix else float("nan"),
                     "high": _num(r[ix["最高價"]]) if "最高價" in ix else float("nan"),
                     "low": _num(r[ix["最低價"]]) if "最低價" in ix else float("nan"),
                     "close": _num(r[ix["收盤價"]]),
                     "chg": chg, "date": date, "mkt": "twse"})
    return pd.DataFrame(rows)


def _twse_inst(date):
    d = _get_json(f"{TWSE}/fund/T86",
                  {"date": date.replace("-", ""), "selectType": "ALL", "response": "json"})
    if not d or d.get("stat") != "OK":
        return pd.DataFrame()
    f = d["fields"]

    def col(*keys):
        for k in keys:
            for i, name in enumerate(f):
                if k in str(name):
                    return i
        return None

    i_code = col("證券代號")
    i_fore = col("外陸資買賣超股數(不含外資自營商)", "外資買賣超股數")
    i_trust = col("投信買賣超股數")
    i_deal = col("自營商買賣超股數")
    if i_code is None or i_fore is None:
        return pd.DataFrame()
    rows = []
    for r in d["data"]:
        rows.append({"code": str(r[i_code]).strip(),
                     "foreign": _num(r[i_fore]),
                     "trust": _num(r[i_trust]) if i_trust is not None else float("nan"),
                     "dealer": _num(r[i_deal]) if i_deal is not None else float("nan"),
                     "date": date})
    return pd.DataFrame(rows)


def _twse_margin(date):
    d = _get_json(f"{TWSE}/marginTrading/MI_MARGN",
                  {"date": date.replace("-", ""), "selectType": "ALL", "response": "json"})
    if not d or d.get("stat") != "OK":
        return pd.DataFrame()
    tbl = next((t for t in d.get("tables", [])
                if "融資融券彙總" in str(t.get("title", ""))), None)
    if not tbl:
        return pd.DataFrame()
    # 欄位是 代號/名稱/融資(買進,賣出,現金償還,前日餘額,今日餘額,限額)/融券(...)，
    # 名稱有重複（融資融券都叫「買進」），所以用位置而不是名稱取。
    rows = [{"code": str(r[0]).strip(),
             "margin_prev": _num(r[5]), "margin": _num(r[6]),
             "date": date} for r in tbl.get("data") or [] if len(r) > 6]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 上櫃
# 櫃買的 OpenAPI 只給最新一日，歷史要走它的 web 端點。

def _tpex_price(date):
    roc = f"{int(date[:4]) - 1911}/{date[5:7]}/{date[8:10]}"
    d = _get_json("https://www.tpex.org.tw/www/zh-tw/afterTrading/otc",
                  {"date": roc, "type": "AL", "response": "json"})
    rows = []
    if d and d.get("tables"):
        t = d["tables"][0]
        f = t.get("fields") or []
        def col(k):
            for i, n in enumerate(f):
                if k in str(n):
                    return i
            return None
        i_code, i_name = col("代號"), col("名稱")
        i_close, i_chg = col("收盤"), col("漲跌")
        i_o, i_h, i_l = col("開盤"), col("最高"), col("最低")
        i_vol, i_val = col("成交股數"), col("成交金額")
        if i_code is not None and i_close is not None:
            for r in t.get("data") or []:
                rows.append({"code": str(r[i_code]).strip(),
                             "name": str(r[i_name]).strip() if i_name is not None else "",
                             "volume": _num(r[i_vol]) if i_vol is not None else float("nan"),
                             "value": _num(r[i_val]) if i_val is not None else float("nan"),
                             "open": _num(r[i_o]) if i_o is not None else float("nan"),
                             "high": _num(r[i_h]) if i_h is not None else float("nan"),
                             "low": _num(r[i_l]) if i_l is not None else float("nan"),
                             "close": _num(r[i_close]),
                             "chg": _num(r[i_chg]) if i_chg is not None else float("nan"),
                             "date": date, "mkt": "tpex"})
    return pd.DataFrame(rows)


def _twse_index(date):
    """加權指數、漲跌家數、大盤成交金額——算 Market Regime 的原料。"""
    d = _get_json(f"{TWSE}/afterTrading/MI_INDEX",
                  {"date": date.replace("-", ""), "type": "ALL", "response": "json"})
    if not d or d.get("stat") != "OK":
        return pd.DataFrame()
    rec = {"date": date}
    for t in d.get("tables", []):
        title, rows = str(t.get("title", "")), t.get("data") or []
        f = t.get("fields") or []
        if "指數" in str(f[:1]) and rows:
            for r in rows:
                if str(r[0]).strip() == "發行量加權股價指數":
                    # 漲跌點數是無號的，方向要看 r[2] 的紅綠標記；
                    # 但漲跌百分比欄位本身已經帶負號，不能再乘一次。
                    up = "red" in re.sub(r"\s", "", str(r[2]))
                    rec["index"] = _num(r[1])
                    rec["index_chg"] = abs(_num(r[3])) * (1 if up else -1)
                    rec["index_pct"] = _num(r[4])
        if "成交統計" in str(f[:1]) and rows:
            for r in rows:
                if "一般股票" in str(r[0]):
                    rec["turnover"] = _num(r[1])
        if "類型" in str(f[:1]) and rows:
            # 「股票」欄形如 "762(15)"：上漲家數(漲停家數)
            for r in rows:
                head, stock = str(r[0]), str(r[2] if len(r) > 2 else "")
                n = _num(stock.split("(")[0])
                if "上漲" in head:
                    rec["adv"] = n
                elif "下跌" in head:
                    rec["dec"] = n
                elif "持平" in head:
                    rec["flat"] = n
    return pd.DataFrame([rec]) if "index" in rec else pd.DataFrame()


def index_history(end_date, days=70, verbose=False):
    rows = []
    for i, d in enumerate(trading_days(end_date, days), 1):
        if verbose:
            print(f"  [{i}/{days}] {d}", file=sys.stderr)
        df = _cached_raw("twse_index", d, _twse_index)
        if not df.empty:
            rows.append(df)
    return pd.concat(rows, ignore_index=True).sort_values("date") if rows else pd.DataFrame()


def _cached_raw(kind, date, fetch):
    """跟 _cached 一樣，但不套用個股代號過濾（指數資料沒有 code 欄）。"""
    p = _cache_path(kind, date)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return pd.DataFrame(json.load(f))
    df = fetch(date)
    if df is None or df.empty:
        return pd.DataFrame()
    with open(p, "w", encoding="utf-8") as f:
        json.dump(df.to_dict("records"), f, ensure_ascii=False)
    return df


def _roc(date):
    return f"{int(date[:4]) - 1911}/{date[5:7]}/{date[8:10]}"


def _tpex_inst(date):
    """上櫃三大法人。

    欄位名稱全是重複的「買進股數/賣出股數/買賣超股數」，沒有分組標籤，
    所以只能用位置取。位置對照已用 6488 / 8069 / 3105 三檔跟 FinMind
    逐筆核對過：idx4=外資(不含外資自營商)、idx13=投信、idx22=自營商合計。
    """
    d = _get_json("https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade",
                  {"type": "Daily", "sect": "EW", "date": _roc(date), "response": "json"})
    if not d or not d.get("tables"):
        return pd.DataFrame()
    rows = [{"code": str(r[0]).strip(), "foreign": _num(r[4]),
             "trust": _num(r[13]), "dealer": _num(r[22]), "date": date}
            for r in d["tables"][0].get("data") or [] if len(r) > 22]
    return pd.DataFrame(rows)


def _tpex_margin(date):
    """上櫃融資融券。欄位: 代號,名稱,前資餘額(張),資買,資賣,現償,資餘額,..."""
    d = _get_json("https://www.tpex.org.tw/www/zh-tw/margin/balance",
                  {"date": _roc(date), "response": "json"})
    if not d or not d.get("tables"):
        return pd.DataFrame()
    rows = [{"code": str(r[0]).strip(), "margin_prev": _num(r[2]),
             "margin": _num(r[6]), "date": date}
            for r in d["tables"][0].get("data") or [] if len(r) > 6]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 對外

def trading_days(end, n):
    """從 end 往回抓 n 個「證交所有回資料」的日子。"""
    out, d = [], datetime.strptime(end, "%Y-%m-%d")
    tried = 0
    while len(out) < n and tried < n * 2 + 20:
        tried += 1
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    return sorted(out)


# 上市櫃普通股(2330)、特別股(2881A)、ETF(0050 / 00878 / 006208 / 00400A)。
# 權證是 6 碼(030481、715123)，會被這個規則排除——不排掉的話全市場會多出四萬個標的。
CODE_RE = re.compile(r"^(\d{4}[A-Z]?|00\d{2,4}[A-Z]?)$")


def is_security(code):
    return bool(CODE_RE.match(str(code).strip()))


def load_panel(end_date, days=70, verbose=True):
    """回傳整個市場 days 個交易日的面板資料。"""
    px, inst, mar = [], [], []
    for i, d in enumerate(trading_days(end_date, days), 1):
        if verbose:
            print(f"  [{i}/{days}] {d}", file=sys.stderr)
        pairs = ((px, "twse_px", _twse_price), (px, "tpex_px", _tpex_price),
                 (inst, "twse_inst", _twse_inst), (inst, "tpex_inst", _tpex_inst),
                 (mar, "twse_margin", _twse_margin), (mar, "tpex_margin", _tpex_margin))
        for lst, kind, fn in pairs:
            df = _cached(kind, d, fn)
            if not df.empty:
                lst.append(df)
    def merge(lst):
        if not lst:
            return pd.DataFrame()
        df = pd.concat(lst, ignore_index=True)
        return df[df.code.map(is_security)].reset_index(drop=True)

    return {"price": merge(px), "inst": merge(inst), "margin": merge(mar)}


if __name__ == "__main__":
    end = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    p = load_panel(end, n)
    for k, v in p.items():
        print(f"{k:8} {len(v):>7,} 列", end="")
        if not v.empty:
            print(f"  {v.date.nunique()} 天  {v.code.nunique():,} 檔")
        else:
            print()
