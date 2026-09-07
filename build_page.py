# -*- coding: utf-8 -*-
"""把 universe.json 烤進 page.template.html，產出可發布的 brief-sample.html。

用法:
    python universe.py && python build_page.py
"""
import json, os, sys

TEMPLATE = "page.template.html"
DATA = "universe.json"
OUT = "brief-sample.html"

# 選股區的分類與排序（頁面上就照這個順序顯示）
CATS = [
    {"name": "半導體", "ids": ["2330", "2454", "2303", "3711", "2379", "3034",
                               "3661", "5269", "3443"]},
    {"name": "電子 · AI 伺服器", "ids": ["2317", "2382", "2357", "2356", "3231",
                                        "2376", "2377", "6669", "2301", "2308"]},
    {"name": "金融", "ids": ["2881", "2882", "2891", "2886", "2884", "2892",
                             "5880", "2887", "2890"]},
    {"name": "傳產 · 電信 · 航運", "ids": ["1301", "1303", "2002", "1216", "1101",
                                          "2412", "3045", "4904", "2207",
                                          "2603", "2609", "2615", "2610", "2618"]},
    {"name": "生技", "ids": ["6446", "1707"]},
    {"name": "ETF", "ids": ["0050", "0056", "00878", "00919", "00929", "00940",
                            "006208", "00713"]},
]

# 沒選過的人第一次看到的預設組合：一檔權值 + 一檔市值型 ETF + 一檔高股息
DEFAULT = ["2330", "0050", "00878"]


def main():
    for f in (TEMPLATE, DATA):
        if not os.path.exists(f):
            sys.exit(f"缺少 {f}")

    with open(DATA, encoding="utf-8") as f:
        data = json.load(f)
    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()

    stocks = data.get("stocks", {})
    if not stocks:
        sys.exit("universe.json 沒有任何股票，不要發布")

    # 分類裡沒列到的股票不會出現在選單，先檢查一遍
    listed = {i for c in CATS for i in c["ids"]}
    orphan = [i for i in stocks if i not in listed]
    if orphan:
        print(f"⚠ 這些股票有資料但不在分類裡，選單看不到：{orphan}", file=sys.stderr)

    html = (html
            .replace("__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
            .replace("__CATS__", json.dumps(CATS, ensure_ascii=False))
            .replace("__DEFAULT__", json.dumps(DEFAULT)))

    for ph in ("__DATA__", "__CATS__", "__DEFAULT__"):
        if ph in html:
            sys.exit(f"樣板還有沒替換的 {ph}")

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)

    n_sig = sum(len(s.get("signals", [])) for s in stocks.values())
    print(f"{OUT}  {os.path.getsize(OUT)/1024:.0f} KB  "
          f"{len(stocks)} 檔 / {n_sig} 個訊號 / 資料日 {data['asof']}")


if __name__ == "__main__":
    main()
