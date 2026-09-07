# -*- coding: utf-8 -*-
"""把一批熱門台股的訊號預先算好，輸出成 universe.json 給網頁用。

靜態頁不能打 FinMind（CSP 擋外部 fetch），所以資料要先烤進去。

用法:
    python universe.py                 # 全部算一遍
    python universe.py --date 2026-09-04
    python universe.py --limit 10      # 只算前 10 檔（測試用）
"""
import argparse, json, os, sys
from datetime import datetime

import brief

# 台灣散戶實際會持有的股票，不是只有台灣50
UNIVERSE = {
    # 半導體
    "2330": "台積電", "2454": "聯發科", "2303": "聯電", "3711": "日月光投控",
    "2379": "瑞昱", "3034": "聯詠", "3661": "世芯-KY", "5269": "祥碩", "3443": "創意",
    # 電子 / AI 伺服器
    "2317": "鴻海", "2382": "廣達", "2357": "華碩", "2356": "英業達", "3231": "緯創",
    "2376": "技嘉", "2377": "微星", "6669": "緯穎", "2301": "光寶科", "2308": "台達電",
    # 金融
    "2881": "富邦金", "2882": "國泰金", "2891": "中信金", "2886": "兆豐金",
    "2884": "玉山金", "2892": "第一金", "5880": "合庫金", "2887": "台新金", "2890": "永豐金",
    # 傳產 / 電信 / 航運
    "1301": "台塑", "1303": "南亞", "2002": "中鋼", "1216": "統一", "1101": "台泥",
    "2412": "中華電", "3045": "台灣大", "4904": "遠傳", "2207": "和泰車",
    "2603": "長榮", "2609": "陽明", "2615": "萬海", "2610": "華航", "2618": "長榮航",
    # 生技
    "6446": "藥華藥", "1707": "葡萄王",
    # ETF
    "0050": "元大台灣50", "0056": "元大高股息", "00878": "國泰永續高股息",
    "00919": "群益台灣精選高息", "00929": "復華台灣科技優息", "00940": "元大台灣價值高息",
    "006208": "富邦台50", "00713": "元大台灣高息低波",
}

OUT = "universe.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    asof = args.date or datetime.now().strftime("%Y-%m-%d")

    items = list(UNIVERSE.items())[: args.limit]
    print(f"計算 {asof} 的 {len(items)} 檔訊號", file=sys.stderr)

    stocks, skipped = {}, []
    for i, (sid, name) in enumerate(items, 1):
        print(f"[{i}/{len(items)}] {sid} {name}", file=sys.stderr)
        try:
            d = brief.load({sid: name}, asof)
            sigs = brief.signals(d)
        except Exception as e:
            skipped.append((sid, name, str(e)[:60]))
            continue
        rec = d["tw"].get(sid, {})
        last = rec.get("_last")
        if not last:
            skipped.append((sid, name, "沒有價格資料"))
            continue
        inst, mg, ld, prem = rec.get("_inst"), rec.get("_margin"), rec.get("_lend"), rec.get("_prem")
        stocks[sid] = {
            "name": name,
            "close": round(last["close"], 2),
            "chg": round(last["chg"], 2),
            "volRatio": round(last["vol_ratio"], 2) if last.get("vol_ratio") else None,
            "fz": round(inst["外資"]["z"], 2) if inst else None,
            "fnet": int(inst["外資"]["net"]) if inst else None,
            "tz": round(inst["投信"]["z"], 2) if inst else None,
            "tstreak": inst["投信"]["streak"] if inst else None,
            "marginChg": int(mg["chg"]) if mg else None,
            "lendRate": round(ld["rate"], 2) if ld else None,
            "lendMed": round(ld["med"], 2) if ld else None,
            "prem": round(prem["cur"], 2) if prem else None,
            "premDelta": round(prem["delta"], 2) if prem else None,
            "rev": rec.get("_rev"),
            "events": sorted(rec.get("_events", [])),
            # 只留跟這檔有關的訊號（美股類的 sid 是 None）
            "signals": [{"kind": s["kind"], "head": s["head"], "body": s["body"],
                         "score": round(s["score"], 1)}
                        for s in sigs if s.get("sid") == sid],
        }

    # 美股脈絡只算一次，全站共用
    us = {}
    if stocks:
        d = brief.load({"2330": "台積電"}, asof)
        for t in brief.US_CONTEXT:
            mv = brief.us_move(d, t)
            if mv:
                us[t] = {"close": round(mv[0], 2), "chg": round(mv[1], 2)}

    payload = {"asof": asof, "generated": datetime.now().isoformat(timespec="seconds"),
               "us": us, "stocks": stocks}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

    n_sig = sum(len(s["signals"]) for s in stocks.values())
    print(f"\n完成：{len(stocks)}/{len(items)} 檔，共 {n_sig} 個訊號 -> {OUT} "
          f"({os.path.getsize(OUT)/1024:.0f} KB)", file=sys.stderr)
    if skipped:
        print(f"略過 {len(skipped)} 檔：", file=sys.stderr)
        for s in skipped[:10]:
            print(f"  {s[0]} {s[1]} — {s[2]}", file=sys.stderr)
    if brief.RATE_LIMITED:
        print("\n⚠ 撞到流量上限，資料不完整，不要拿這份去發布", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
