# -*- coding: utf-8 -*-
"""每日持股早報產生器。

用法:
    python brief.py                 # 產出今天的早報 (text + html)
    python brief.py --date 2026-09-04
    python brief.py --portfolio 2330,2317,0050

輸出:
    out/brief-YYYY-MM-DD.txt   給 LINE / Telegram 手動貼上
    out/brief-YYYY-MM-DD.html  給網頁版
"""
import argparse, json, os, sys, time
from datetime import datetime, timedelta
import requests
import pandas as pd

API = "https://api.finmindtrade.com/api/v4/data"
CACHE = "cache"
OUT = "out"

PORTFOLIO = {
    "2330": "台積電", "2317": "鴻海", "2454": "聯發科",
    "2382": "廣達", "2603": "長榮", "0050": "元大台灣50",
    "00878": "國泰永續高股息",
}
# 台股代號 -> (ADR 代號, 1 ADR 等於幾股普通股)
ADR_MAP = {"2330": ("TSM", 5), "2303": ("UMC", 5), "2412": ("CHT", 10), "3711": ("ASX", 2)}
# 一定會看的美股，用來判斷族群方向
US_CONTEXT = ["NVDA", "AAPL", "AVGO", "AMD", "MU"]


# ---------------------------------------------------------------- fetch

if os.path.exists(".env"):  # 讓 FINMIND_TOKEN 不用每次手動 export
    for line in open(".env", encoding="utf-8"):
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

FAILED = []          # 這次執行抓失敗的 (dataset, data_id, 原因)
RATE_LIMITED = False  # 只要撞到一次流量上限就記著


def fetch(dataset, data_id, start, ttl=43200):
    """打 FinMind，結果存 cache/ 半天，避免重跑時浪費免費額度。

    設環境變數 FINMIND_TOKEN 可以提高額度（免費註冊即可拿到）。
    """
    global RATE_LIMITED
    os.makedirs(CACHE, exist_ok=True)
    key = f"{dataset}_{data_id}_{start}".replace("/", "_")
    path = os.path.join(CACHE, key + ".json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl:
        with open(path, encoding="utf-8") as f:
            return pd.DataFrame(json.load(f))

    params = {"dataset": dataset, "data_id": data_id, "start_date": start}
    token = os.environ.get("FINMIND_TOKEN")
    if token:
        params["token"] = token
    try:
        r = requests.get(API, params=params, timeout=30).json()
    except Exception as e:
        FAILED.append((dataset, data_id, f"連線失敗: {e}"))
        return pd.DataFrame()
    if r.get("status") != 200:
        msg = str(r.get("msg"))
        if "upper limit" in msg or "limit" in msg.lower():
            RATE_LIMITED = True
        FAILED.append((dataset, data_id, msg))
        return pd.DataFrame()
    data = r.get("data") or []
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return pd.DataFrame(data)


def load(portfolio, asof):
    """把一份持股需要的所有資料抓齊。"""
    start = (datetime.strptime(asof, "%Y-%m-%d") - timedelta(days=120)).strftime("%Y-%m-%d")
    lend_start = (datetime.strptime(asof, "%Y-%m-%d") - timedelta(days=300)).strftime("%Y-%m-%d")
    d = {"asof": asof, "tw": {}, "us": {}, "fx": None, "portfolio": portfolio}

    fx = fetch("TaiwanExchangeRate", "USD", start)
    if not fx.empty:
        fx["fx"] = (fx.spot_buy + fx.spot_sell) / 2
        d["fx"] = fx[["date", "fx"]].sort_values("date")

    for sid, name in portfolio.items():
        print(f"  抓 {sid} {name}", file=sys.stderr)
        rec = {"name": name}
        px = fetch("TaiwanStockPrice", sid, start)
        if not px.empty:
            rec["px"] = px.sort_values("date").reset_index(drop=True)
        inst = fetch("TaiwanStockInstitutionalInvestorsBuySell", sid, start)
        if not inst.empty:
            inst["net"] = inst["buy"] - inst["sell"]
            names = {"Foreign_Investor": "外資", "Investment_Trust": "投信"}
            piv = (inst[inst["name"].isin(names)]
                   .pivot_table(index="date", columns="name", values="net", aggfunc="sum")
                   .rename(columns=names).sort_index())
            for c in ("外資", "投信"):
                if c not in piv:
                    piv[c] = 0
            rec["inst"] = piv.fillna(0)
        # 借券不是每天都有成交，要湊到 60 個樣本得往回拉得比其他資料長
        lend = fetch("TaiwanStockSecuritiesLending", sid, lend_start)
        if not lend.empty and "fee_rate" in lend:
            # 每筆借券有各自的費率，用成交量加權才是當天的真實借券成本
            lend = lend.copy()
            lend["fv"] = lend.fee_rate * lend.volume
            g = lend.groupby("date").agg(vol=("volume", "sum"), fv=("fv", "sum"),
                                         mx=("fee_rate", "max")).sort_index()
            g = g[g.vol > 0]
            g["rate"] = g.fv / g.vol
            rec["lend"] = g
        mar = fetch("TaiwanStockMarginPurchaseShortSale", sid, start)
        if not mar.empty and "MarginPurchaseTodayBalance" in mar:
            mar = mar.sort_values("date")
            # 融資餘額單位是張；當日變化 = 今日餘額 - 昨日餘額
            mar["margin_chg"] = mar.MarginPurchaseTodayBalance - mar.MarginPurchaseYesterdayBalance
            rec["margin"] = mar[["date", "MarginPurchaseTodayBalance", "margin_chg"]].set_index("date")
        rev = fetch("TaiwanStockMonthRevenue", sid, "2024-01-01")
        if not rev.empty:
            rec["rev"] = rev.sort_values(["revenue_year", "revenue_month"])
        div = fetch("TaiwanStockDividend", sid, "2026-01-01")
        if not div.empty:
            rec["div"] = div
        d["tw"][sid] = rec

    tickers = set(US_CONTEXT) | {ADR_MAP[s][0] for s in portfolio if s in ADR_MAP}
    for t in sorted(tickers):
        u = fetch("USStockPrice", t, start)
        if not u.empty:
            d["us"][t] = u.sort_values("date").reset_index(drop=True)
    return d


# ---------------------------------------------------------------- analyze

def _pct(a, b):
    return (a / b - 1) * 100 if b else 0.0


def us_move(d, ticker):
    """回傳 (收盤, 漲跌%)，只看 asof 當天以前的資料。"""
    u = d["us"].get(ticker)
    if u is None or u.empty:
        return None
    u = u[u.date <= d["asof"]]
    if len(u) < 2:
        return None
    c, p = float(u.iloc[-1].Close), float(u.iloc[-2].Close)
    return c, _pct(c, p)


def _pad(s, n):
    """中文字在等寬字型佔兩格，用顯示寬度補空白才對得齊。"""
    import unicodedata
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))
    return str(s) + " " * max(1, n - w)


def signals(d):
    """把資料變成排序過的訊號。score 越高越該放前面。"""
    out = []
    asof = d["asof"]

    for sid, rec in d["tw"].items():
        name, px = rec["name"], rec.get("px")
        if px is None or px.empty:
            continue
        row = px[px.date <= asof]
        if row.empty:
            continue
        row = row.iloc[-1]
        prev_close = float(row.close) - float(row.spread)
        chg = _pct(float(row.close), prev_close)
        v20 = px[px.date < row.date].tail(20)["Trading_Volume"]
        vr = float(row.Trading_Volume) / v20.mean() if len(v20) else None
        rec["_last"] = {"date": row.date, "close": float(row.close), "chg": chg, "vol_ratio": vr}

        # --- ADR 溢價
        if sid in ADR_MAP and d["fx"] is not None:
            tick, ratio = ADR_MAP[sid]
            adr = d["us"].get(tick)
            if adr is not None and not adr.empty:
                m = (px[["date", "close"]].merge(adr[["date", "Close"]], on="date")
                     .merge(d["fx"], on="date").sort_values("date"))
                m = m[m.date <= asof]
                if len(m) >= 2:
                    m["prem"] = (m.Close * m.fx / ratio / m.close - 1) * 100
                    cur, prv = m.iloc[-1], m.iloc[-2]
                    delta = cur.prem - prv.prem
                    implied = cur.Close * cur.fx / ratio
                    rec["_prem"] = {"series": m.tail(8)[["date", "prem"]].values.tolist(),
                                    "cur": cur.prem, "delta": delta,
                                    "implied": implied, "tick": tick}
                    if abs(delta) >= 1.0:
                        hi = "最高" if cur.prem >= m.tail(10).prem.max() else ""
                        out.append(dict(score=95 + abs(delta), kind="adr", sid=sid,
                            head=f"{name} ADR 溢價單日{'擴大' if delta>0 else '收斂'} {abs(delta):.2f} 個百分點",
                            body=f"{tick} 溢價來到 {cur.prem:.2f}%（前日 {prv.prem:.2f}%）{'，是近十個交易日'+hi if hi else ''}。"
                                 f"ADR 隱含台股價 {implied:,.0f} 元，實際收 {cur.close:,.0f} 元。"
                                 + (f" 而台股當天量能只有 20 日均量的 {vr:.2f} 倍，買方沒跟上。" if vr and vr < 0.8 else "")))

        # --- 法人 z / 連續 / 背離
        inst = rec.get("inst")
        if inst is not None and len(inst) > 5:
            inst = inst[inst.index <= asof]
            if inst.empty:
                continue
            stats = {}
            for col in ("外資", "投信"):
                s = inst[col]
                last = float(s.iloc[-1])
                hist = s.iloc[:-1].tail(20)
                z = (last - hist.mean()) / hist.std() if len(hist) > 3 and hist.std() > 0 else 0.0
                sign = 1 if last > 0 else (-1 if last < 0 else 0)
                n = 0
                for v in reversed(s.tolist()):
                    if sign and (v > 0) == (sign > 0) and v != 0:
                        n += 1
                    else:
                        break
                stats[col] = {"net": last, "z": float(z), "streak": n * sign}
            rec["_inst"] = stats

            fz, tz = stats["外資"]["z"], stats["投信"]["z"]
            if abs(fz) >= 2:
                out.append(dict(score=80 + abs(fz) * 5, kind="z", sid=sid,
                    head=f"{name} 外資{'買' if fz>0 else '賣'}超 z={fz:+.2f}",
                    body=f"{'買' if fz>0 else '賣'}超 {abs(stats['外資']['net'])/1e4:,.0f} 萬股，"
                         f"是 20 個交易日內沒出現過的量級。當日股價 {chg:+.2f}%。"))
            if stats["外資"]["net"] * stats["投信"]["net"] < 0 and min(abs(fz), abs(tz)) >= 0.8:
                # 背離最難從別的地方看到，優先度給高於單純的大額買賣超
                out.append(dict(score=100 + min(abs(fz), abs(tz)), kind="diverge", sid=sid,
                    head=f"{name} 外資與投信對做",
                    body=f"外資{'買' if fz>0 else '賣'}超 {abs(stats['外資']['net'])/1e4:,.0f} 萬股（z={fz:+.2f}），"
                         f"投信反向{'買' if tz>0 else '賣'}超 {abs(stats['投信']['net'])/1e4:,.0f} 萬股"
                         + (f"（已連 {abs(stats['投信']['streak'])} 天）" if abs(stats['投信']['streak']) >= 2 else "")
                         + "。兩邊都不是散戶，而看法相反。"))
            # --- 借券費率飆高：有人急著做空，急到願意付溢價
            # 各股的常態費率差很多（台積電中位 0.4%、廣達 5%），所以門檻用
            # 「跟自己過去 60 天比的第 90 百分位」，不用絕對數字。
            ld = rec.get("lend")
            if ld is not None:
                ld = ld[ld.index <= asof]
                if len(ld) >= 61:
                    hist = ld.rate.iloc[:-1].tail(60)
                    vhist = ld.vol.iloc[:-1].tail(60)
                    cur = ld.iloc[-1]
                    p90, med = hist.quantile(.90), hist.median()
                    mult = cur.rate / med if med > 0 else 0
                    rec["_lend"] = {"rate": float(cur.rate), "med": float(med),
                                    "mx": float(cur.mx), "vol": float(cur.vol)}
                    if cur.rate >= p90 and cur.rate >= 1.0 and cur.vol >= vhist.median():
                        out.append(dict(score=100 + min(mult, 6), kind="lend", sid=sid,
                            head=f"{name} 借券費率飆到 {cur.rate:.2f}%",
                            body=f"平常中位數只有 {med:.2f}%（{mult:.1f} 倍），"
                                 f"單筆最高 {cur.mx:.2f}%，當日借券 {cur.vol:,.0f} 張。"
                                 f"借券是拿來放空的，費率被推高代表有人急到願意付溢價。"
                                 f"當日股價 {chg:+.2f}%。"))

            # --- 散戶接刀：法人在出，融資（散戶槓桿）在進
            mar = rec.get("margin")
            if mar is not None and len(mar) > 5:
                mar = mar[mar.index <= asof]
                if len(mar) > 5:
                    mc = float(mar.margin_chg.iloc[-1])
                    hist = mar.margin_chg.iloc[:-1].tail(20)
                    mz = (mc - hist.mean()) / hist.std() if len(hist) > 3 and hist.std() > 0 else 0.0
                    bal = float(mar.MarginPurchaseTodayBalance.iloc[-1])
                    rec["_margin"] = {"chg": mc, "z": float(mz), "bal": bal}
                    fn = stats["外資"]["net"]
                    if fn < 0 and mc > 0 and mz >= 1.0 and fz <= -1.0:
                        out.append(dict(score=105 + min(mz, 4), kind="retail", sid=sid,
                            head=f"{name}：法人在出，散戶用融資在接",
                            body=f"外資賣超 {abs(fn)/1000:,.0f} 張（z={fz:+.2f}），"
                                 f"同一天融資餘額增加 {mc:,.0f} 張（z={mz:+.2f}），"
                                 f"餘額來到 {bal:,.0f} 張。當日股價 {chg:+.2f}%。"))
                    elif fn > 0 and mc < 0 and mz <= -1.0 and fz >= 1.0:
                        out.append(dict(score=102, kind="retail", sid=sid,
                            head=f"{name}：法人在進，散戶在減碼",
                            body=f"外資買超 {fn/1000:,.0f} 張（z={fz:+.2f}），"
                                 f"融資餘額反而減少 {abs(mc):,.0f} 張。當日股價 {chg:+.2f}%。"))

            for col in ("外資", "投信"):
                st = stats[col]["streak"]
                if abs(st) >= 5:
                    # 連續賣超但股價漲（或反之）比單純的長連續更值得講
                    against = (st < 0 and chg > 1) or (st > 0 and chg < -1)
                    out.append(dict(score=60 + abs(st) + (15 if against else 0), kind="streak", sid=sid,
                        head=f"{name} {col}連 {abs(st)} 天{'買' if st>0 else '賣'}超",
                        body=f"少見的長連續。當日股價 {chg:+.2f}%"
                             + ("——連續賣超卻收漲，賣壓被吃下來了。" if st < 0 and chg > 1
                                else "——連續買超卻收跌，有人在對面倒貨。" if st > 0 and chg < -1
                                else "。")))

        # --- 量能異常
        if vr and (vr >= 2 or vr <= 0.5):
            out.append(dict(score=55, kind="vol", sid=sid,
                head=f"{name} 量能{'暴增' if vr>=2 else '極縮'}至 20 日均量的 {vr:.2f} 倍",
                body=f"當日股價 {chg:+.2f}%，收 {float(row.close):,.2f}。"))

    # --- 美股族群動向（跟持股有連動的才進訊號）
    big = []
    for t in US_CONTEXT:
        mv = us_move(d, t)
        if mv and abs(mv[1]) >= 3:
            big.append((t, mv[0], mv[1]))
    if big:
        big.sort(key=lambda x: -abs(x[2]))
        desc = "、".join(f"{t} {ch:+.2f}%" for t, _, ch in big)
        up = sum(1 for _, _, ch in big if ch > 0)
        out.append(dict(score=85 + abs(big[0][2]), kind="us", sid=None,
            head=f"昨夜美股：{desc}",
            body=("半導體族群整體走強，不是單一個股事件。" if up == len(big)
                  else "美股個股方向分歧，族群沒有一致訊號。")
                 + "台股相關供應鏈今天開盤會先反映這個方向。"))

    # --- 未來 7 天事件
    for sid, rec in d["tw"].items():
        name = rec["name"]
        div = rec.get("div")
        if div is not None and "CashExDividendTradingDate" in div:
            for _, r in div.iterrows():
                ed = str(r.get("CashExDividendTradingDate") or "")
                if not ed:
                    continue
                days = (datetime.strptime(ed, "%Y-%m-%d") - datetime.strptime(asof, "%Y-%m-%d")).days
                if 0 <= days <= 10:
                    rec.setdefault("_events", []).append(
                        (ed, f"{name}除息交易日", f"配息 {float(r.get('CashEarningsDistribution',0)):.2f} 元"))
        rev = rec.get("rev")
        if rev is not None and not rev.empty:
            last = rev.iloc[-1]
            prev = rev.iloc[-2] if len(rev) > 1 else None
            yoy_row = rev[(rev.revenue_year == last.revenue_year - 1) &
                          (rev.revenue_month == last.revenue_month)]
            rec["_rev"] = {
                "ym": f"{int(last.revenue_year)}/{int(last.revenue_month)}",
                "amt": float(last.revenue),
                "yoy": _pct(float(last.revenue), float(yoy_row.iloc[0].revenue)) if len(yoy_row) else None,
                "mom": _pct(float(last.revenue), float(prev.revenue)) if prev is not None else None,
            }
            # 月營收法定公布期限：次月 10 日
            a = datetime.strptime(asof, "%Y-%m-%d")
            due = datetime(a.year, a.month, 10)
            if due < a:
                due = datetime(a.year + 1, 1, 10) if a.month == 12 else datetime(a.year, a.month + 1, 10)
            behind = (a.year * 12 + a.month) - (int(last.revenue_year) * 12 + int(last.revenue_month))
            if behind >= 2 and 0 <= (due - a).days <= 10:
                rec.setdefault("_events", []).append(
                    (due.strftime("%Y-%m-%d"), f"{name} {a.month-1 or 12} 月營收公布", "法定期限"))

    out.sort(key=lambda x: -x["score"])
    return out


# ---------------------------------------------------------------- render

def render_text(d, sigs, top=4):
    a = d["asof"]
    L = [f"📊 你的持股早報 · {a}", "─" * 28, ""]
    if sigs:
        L += ["【今天最該知道的】", f"▸ {sigs[0]['head']}", f"  {sigs[0]['body']}", ""]
    rest = sigs[1:top]
    if rest:
        L.append("【其他訊號】")
        for s in rest:
            L += [f"▸ {s['head']}", f"  {s['body']}"]
        L.append("")

    us = []
    for t in US_CONTEXT:
        mv = us_move(d, t)
        if mv:
            us.append(f"{t} {mv[0]:,.2f} ({mv[1]:+.2f}%)")
    if us:
        L += ["【昨夜美股】", "  " + " · ".join(us), ""]

    rows = []
    for sid, rec in d["tw"].items():
        last, inst = rec.get("_last"), rec.get("_inst")
        if not last:
            continue
        z = f"  外資z {inst['外資']['z']:+.2f}" if inst else ""
        rows.append(f"  {_pad(rec['name'], 15)}{last['close']:>9,.2f} {last['chg']:>+6.2f}%{z}")
    if rows:
        L += ["【你的持股】"] + rows + [""]

    evs = sorted({e for rec in d["tw"].values() for e in rec.get("_events", [])})
    if evs:
        L.append("【未來 7 天】")
        for ed, title, note in evs:
            L.append(f"  {ed[5:]} {title} — {note}")
        L.append("")

    L += ["─" * 28, "公開資料整理，非投資建議。"]
    return "\n".join(L)


POST_HOOK = {
    "lend": "借券費率是放空的成本。費率被推高，代表有人願意付溢價也要空。這個數字幾乎沒人在看。",
    "retail": "融資餘額是散戶的槓桿部位。法人賣、融資買，代表接手的是誰。",
    "diverge": "外資和投信在同一檔股票上對做，通常代表有還沒公開的資訊分歧。",
    "z":       "「有買」和「買到 20 天內沒出現過的量級」是兩件事。",
    "adr":     "ADR 溢價擴大而台股沒跟，這個落差通常撐不過一個交易日。",
    "streak":  "單日買賣超看不出什麼，連續十幾天就不是雜訊了。",
    "us":      "美股族群的方向，台股供應鏈開盤第一個小時就會反映。",
    "vol":     "價格會騙人，量能比較難。",
}


def render_post(d, sigs, url=""):
    """把當天最強的訊號寫成一則貼文草稿。送出前自己改一遍語氣。"""
    if not sigs:
        return "（今天沒有夠強的訊號，建議不要硬發）"
    s = sigs[0]
    hook = POST_HOOK.get(s["kind"], "")
    body = s["body"].replace("。 ", "。")
    lines = [s["head"], "", body]
    if hook:
        lines += ["", hook]
    lines += ["", f"我每天早上 8:30 會把這種東西整理成一頁，只講自己的持股。",
              f"今天這份在這 👉 {url or '（放你的連結）'}"]
    return "\n".join(lines)


def render_html(d, sigs, top=4):
    """最小可用的 HTML，套用跟樣本頁同一組 token。詳細版排版見 brief-sample.html。"""
    esc = lambda s: (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    blocks = []
    for i, s in enumerate(sigs[:top]):
        lab = "今天最該知道的一件事" if i == 0 else s["kind"]
        blocks.append(f'<div class="sig"><span class="lab">{esc(lab)}</span>'
                      f'<h3>{esc(s["head"])}</h3><p>{esc(s["body"])}</p></div>')
    rows = []
    for sid, rec in d["tw"].items():
        last, inst = rec.get("_last"), rec.get("_inst")
        if not last:
            continue
        cls = "up" if last["chg"] >= 0 else "down"
        z = f'{inst["外資"]["z"]:+.2f}' if inst else "—"
        rows.append(f'<tr><td>{esc(rec["name"])}<span class="t">{sid}</span></td>'
                    f'<td class="n">{last["close"]:,.2f}</td>'
                    f'<td class="n {cls}">{last["chg"]:+.2f}%</td><td class="n">{z}</td></tr>')
    return f"""<title>持股早報 {d['asof']}</title>
<style>
:root{{--paper:#F5F7F8;--surface:#fff;--ink:#131A20;--ink-2:#3D4A55;--muted:#5F6D78;
--rule:#DCE2E6;--brand:#0F6E7D;--up:#D0342C;--down:#12886B}}
@media(prefers-color-scheme:dark){{:root:not([data-theme=light]){{--paper:#0B1015;--surface:#141C23;
--ink:#E4EAEF;--ink-2:#B3C0CA;--muted:#8494A0;--rule:#222E38;--brand:#46AEC0;--up:#F2564A;--down:#33BE97}}}}
:root[data-theme=dark]{{--paper:#0B1015;--surface:#141C23;--ink:#E4EAEF;--ink-2:#B3C0CA;
--muted:#8494A0;--rule:#222E38;--brand:#46AEC0;--up:#F2564A;--down:#33BE97}}
body{{background:var(--paper);color:var(--ink);font-family:"Noto Sans TC",-apple-system,"PingFang TC",sans-serif;
line-height:1.75;font-size:15px}}
.w{{max-width:680px;margin:0 auto;padding:32px 20px 64px}}
h1{{font-size:26px;margin:0 0 4px}} .date{{font-family:ui-monospace,monospace;color:var(--muted);font-size:13px;
margin-bottom:28px;display:block}}
.sig{{background:var(--surface);border-left:3px solid var(--brand);border-radius:0 6px 6px 0;
padding:14px 16px;margin:14px 0}}
.sig .lab{{font-family:ui-monospace,monospace;font-size:11px;letter-spacing:.1em;color:var(--brand);
font-weight:600;text-transform:uppercase}}
.sig h3{{font-size:16px;margin:5px 0 6px}} .sig p{{margin:0;font-size:14px;color:var(--ink-2)}}
table{{width:100%;border-collapse:collapse;font-size:14px;margin-top:24px}}
th{{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);text-align:left;
padding-bottom:6px;border-bottom:1px solid var(--rule)}}
td{{padding:9px 0;border-bottom:1px solid var(--rule)}}
.n{{text-align:right;font-family:ui-monospace,monospace;font-variant-numeric:tabular-nums}}
.t{{font-family:ui-monospace,monospace;font-size:11px;color:var(--muted);display:block}}
.up{{color:var(--up)}} .down{{color:var(--down)}}
.d{{margin-top:28px;font-size:11.5px;color:var(--muted);border-top:1px solid var(--rule);padding-top:12px}}
</style>
<div class="w"><h1>你的持股早報</h1><span class="date">{d['asof']}</span>
{''.join(blocks)}
<table><thead><tr><th>個股</th><th class="n">收盤</th><th class="n">漲跌</th><th class="n">外資 z</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<p class="d">公開資料整理與統計描述，非投資建議。資料來源：FinMind。</p></div>"""


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="資料截止日 YYYY-MM-DD，預設今天")
    ap.add_argument("--portfolio", default=None, help="逗號分隔的股票代號")
    ap.add_argument("--url", default="", help="貼文要附的連結")
    args = ap.parse_args()

    asof = args.date or datetime.now().strftime("%Y-%m-%d")
    port = PORTFOLIO
    if args.portfolio:
        ids = [s.strip() for s in args.portfolio.split(",") if s.strip()]
        port = {i: PORTFOLIO.get(i, i) for i in ids}

    print(f"產生 {asof} 的早報，{len(port)} 檔持股", file=sys.stderr)
    d = load(port, asof)
    sigs = signals(d)

    # 完整性檢查：殘缺的早報比沒有早報更危險——訂閱者會把「抓失敗」
    # 誤讀成「這檔今天沒事」。寧可不出，也不要出一半。
    got = [s for s, r in d["tw"].items() if r.get("_last")]
    missing = [f"{s} {port[s]}" for s in port if s not in got]
    if missing:
        print("\n" + "!" * 46, file=sys.stderr)
        print(f"資料不完整：{len(missing)}/{len(port)} 檔沒抓到 —— {'、'.join(missing)}", file=sys.stderr)
        if RATE_LIMITED:
            print("原因是 FinMind 流量上限。等一小時再跑，或設 FINMIND_TOKEN 提高額度：", file=sys.stderr)
            print("  export FINMIND_TOKEN=你的token   # https://finmindtrade.com 免費註冊", file=sys.stderr)
        print("這份早報不要發出去。", file=sys.stderr)
        print("!" * 46 + "\n", file=sys.stderr)
        sys.exit(1)

    print(f"找到 {len(sigs)} 個訊號，{len(got)}/{len(port)} 檔資料完整", file=sys.stderr)
    if FAILED:
        print(f"（{len(FAILED)} 個非必要欄位抓失敗，不影響主體）", file=sys.stderr)

    os.makedirs(OUT, exist_ok=True)
    txt, html = render_text(d, sigs), render_html(d, sigs)
    post = render_post(d, sigs, args.url)
    for ext, content in (("txt", txt), ("html", html), ("post.txt", post)):
        p = os.path.join(OUT, f"brief-{asof}.{ext}")
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"  -> {p}", file=sys.stderr)
    print(txt)
    print("\n" + "=" * 28 + "\n【今天的貼文草稿 — 送出前自己改一遍】\n")
    print(post)


if __name__ == "__main__":
    main()
