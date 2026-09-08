# -*- coding: utf-8 -*-
"""把 tradelab.json 烤進樣板，並產出前端輪詢用的 meta.json。

    python export_tradelab.py && python build_tradelab.py
"""
import json, os, shutil, sys

TEMPLATE = "tradelab.template.html"
DATA = "tradelab.json"
OUT = "tradelab.html"
DOCS = "docs"


def main():
    for f in (TEMPLATE, DATA):
        if not os.path.exists(f):
            sys.exit(f"缺少 {f}")

    with open(DATA, encoding="utf-8") as f:
        d = json.load(f)
    if not d.get("stocks"):
        sys.exit("tradelab.json 沒有任何股票，不要發布")

    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()
    html = html.replace("__DATA__", json.dumps(d, ensure_ascii=False, separators=(",", ":")))
    if "__DATA__" in html:
        sys.exit("樣板替換失敗")

    os.makedirs(DOCS, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    shutil.copy(OUT, os.path.join(DOCS, OUT))
    shutil.copy(DATA, os.path.join(DOCS, DATA))

    # 前端每分鐘輪詢這個小檔判斷有沒有更新，有變才去抓 1MB 的主檔
    meta = {"asof": d["asof"], "generated": d["generated"], "universe": d["universe"]}
    for path in ("meta.json", os.path.join(DOCS, "meta.json")):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)

    print(f"{OUT}  {os.path.getsize(OUT)/1024/1024:.2f} MB  "
          f"{len(d['stocks'])} 檔 / 資料日 {d['asof']}")


if __name__ == "__main__":
    main()
