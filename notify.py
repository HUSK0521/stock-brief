# -*- coding: utf-8 -*-
"""把當日訊號推播到 Telegram / Discord / LINE。

    python notify.py --dry-run     # 只印出訊息，不送
    python notify.py --test        # 送一則測試訊息，確認管道通了
    python notify.py               # 正式送

設定分兩層：
  notify.config.json  ← 要推哪些股票、哪些規則、開哪些管道（可進版控）
  環境變數 / GitHub Secrets ← token 與 webhook（絕對不要進版控）

環境變數：
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
  DISCORD_WEBHOOK_URL
  LINE_CHANNEL_TOKEN / LINE_TO        （LINE Notify 已於 2025-03-31 停止服務，
                                        這裡用的是 Messaging API 的 push）
"""
import argparse, json, os, sys
from datetime import datetime

import requests

CONFIG = "notify.config.json"
DATA = "tradelab.json"

DEFAULT_CONFIG = {
    "watch": ["2330", "2317", "2454"],
    "rules": ["inst_in", "retail_catch", "diverge", "fz_buy", "fz_sell"],
    "market_top": 3,
    "channels": {"telegram": False, "discord": False, "line": False},
    "only_when_signals": True,
}


def load_config():
    if not os.path.exists(CONFIG):
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        print(f"已建立 {CONFIG}，請編輯後再執行", file=sys.stderr)
        return DEFAULT_CONFIG
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg


# ---------------------------------------------------------------- 訊號

def lots(v):
    return f"{abs(v)/1000:,.0f} 張"


def rank_txt(s):
    r = s.get("frank")
    if r is None or s.get("fnet") is None:
        return ""
    d = "買" if s["fnet"] > 0 else "賣"
    if r == 1:
        return f"20 天內最大的一次{d}超"
    if r <= 3:
        return f"20 天內第 {r} 大的{d}超"
    return f"近 20 天第 {r} 大"


def signals_for(code, s, rules):
    """跟前端同一套規則。門檻用預設值——要調就改這裡，或改前端後同步。"""
    out = []
    fz, tz, mz = s.get("fz"), s.get("tz"), s.get("mz")
    fnet, mchg, chg = s.get("fnet"), s.get("mchg"), s.get("chg") or 0
    nm = s["name"]

    if "inst_in" in rules and None not in (fnet, mchg, mz, fz) and fnet > 0 and mchg < 0 and mz <= -1 and fz >= 1:
        out.append(("inst_in", f"{nm}：法人在進，散戶在減碼",
                    f"外資買超 {lots(fnet)}（{rank_txt(s)}），融資餘額反而減少 {abs(mchg):,.0f} 張。股價 {chg:+.2f}%。"))
    if "retail_catch" in rules and None not in (fnet, mchg, mz, fz) and fnet < 0 and mchg > 0 and mz >= 1 and fz <= -1:
        out.append(("retail_catch", f"{nm}：法人在出，散戶用融資在接",
                    f"外資賣超 {lots(fnet)}（{rank_txt(s)}），融資餘額增加 {mchg:,.0f} 張。股價 {chg:+.2f}%。"))
    if "diverge" in rules and None not in (fnet, fz, tz) and abs(fz) >= 0.8 and abs(tz) >= 0.8 \
            and ((fnet > 0 and tz < 0) or (fnet < 0 and tz > 0)):
        out.append(("diverge", f"{nm} 外資與投信對做",
                    f"外資{'買' if fnet > 0 else '賣'}超 {lots(fnet)}，投信方向相反。股價 {chg:+.2f}%。"))
    if "fz_buy" in rules and fz is not None and 2 <= fz <= 8:
        out.append(("fz_buy", f"{nm} 外資買超 {lots(fnet)}，{rank_txt(s)}", f"股價 {chg:+.2f}%。"))
    if "fz_sell" in rules and fz is not None and -8 <= fz <= -2:
        out.append(("fz_sell", f"{nm} 外資賣超 {lots(fnet)}，{rank_txt(s)}", f"股價 {chg:+.2f}%。"))
    return out


def build_message(cfg):
    with open(DATA, encoding="utf-8") as f:
        d = json.load(f)
    S, rules = d["stocks"], set(cfg["rules"])
    edge = {r["key"]: r for r in (d.get("validation") or {}).get("rules", [])}

    mine = []
    for c in cfg["watch"]:
        if c in S:
            for k, head, body in signals_for(c, S[c], rules):
                mine.append((c, k, head, body))

    m = d["market"]
    L = [f"📊 台股訊號 · {d['asof']} 收盤",
         f"加權 {m['index']:,.2f} ({m['pct']:+.2f}%)　"
         f"▲{m['adv']} ▼{m['dec']}　{d['regime']['state']}",
         ""]

    if mine:
        L.append(f"【你的自選股 · {len(mine)} 筆】")
        for c, k, head, body in mine:
            e = edge.get(k)
            tail = f"（此訊號歷史 5 日超額 {e['excess']:+.2f}%，N={e['n']}）" if e else ""
            L += [f"▸ {head}", f"  {body}{tail}"]
        L.append("")
    elif cfg.get("only_when_signals"):
        return None, 0   # 自選股沒事就不吵人

    n = cfg.get("market_top", 0)
    if n:
        allsig = []
        for c, s in S.items():
            for k, head, body in signals_for(c, s, rules):
                e = edge.get(k)
                allsig.append((abs(e["excess"]) if e else 0, head, body))
        allsig.sort(reverse=True, key=lambda x: x[0])
        if allsig:
            L.append(f"【全市場最強 {min(n, len(allsig))} 筆】")
            for _, head, body in allsig[:n]:
                L += [f"▸ {head}"]
            L.append("")

    L.append("公開資料整理，非投資建議。")
    return "\n".join(L), len(mine)


# ---------------------------------------------------------------- 管道

def send_telegram(text):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return False, "缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID"
    r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=30,
                      json={"chat_id": chat, "text": text[:4096],
                            "disable_web_page_preview": True})
    return r.status_code == 200, f"HTTP {r.status_code} {r.text[:120]}"


def send_discord(text):
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return False, "缺少 DISCORD_WEBHOOK_URL"
    r = requests.post(url, timeout=30, json={"content": text[:1990]})
    return r.status_code in (200, 204), f"HTTP {r.status_code} {r.text[:120]}"


def send_line(text):
    """LINE Messaging API push。

    LINE Notify 已於 2025-03-31 終止，改用官方帳號的 Messaging API：
    需要 Channel access token，且收訊者必須先加該官方帳號為好友。
    免費方案有每月訊息則數上限，超過要付費。
    """
    tok, to = os.environ.get("LINE_CHANNEL_TOKEN"), os.environ.get("LINE_TO")
    if not tok:
        return False, "缺少 LINE_CHANNEL_TOKEN"
    # 只做點對點 push，不做 broadcast。
    # broadcast 會一次送給官方帳號的所有好友，不應該因為少設一個環境變數就發生。
    if not to:
        return False, "缺少 LINE_TO（收訊者 userId）。本工具不使用 broadcast。"
    body = {"to": to, "messages": [{"type": "text", "text": text[:4900]}]}
    r = requests.post("https://api.line.me/v2/bot/message/push", timeout=30, json=body,
                      headers={"Authorization": f"Bearer {tok}",
                               "Content-Type": "application/json"})
    return r.status_code == 200, f"HTTP {r.status_code} {r.text[:160]}"


SENDERS = {"telegram": send_telegram, "discord": send_discord, "line": send_line}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只印訊息，不送出")
    ap.add_argument("--test", action="store_true", help="送一則測試訊息")
    args = ap.parse_args()
    cfg = load_config()

    if args.test:
        text = f"✅ TradeLab 推播測試 · {datetime.now():%Y-%m-%d %H:%M}\n管道設定正確。"
        n_sig = 1
    else:
        text, n_sig = build_message(cfg)
        if text is None:
            print("自選股今天沒有觸發訊號，依設定不推播。", file=sys.stderr)
            return

    if args.dry_run:
        print(text)
        return

    on = [k for k, v in cfg["channels"].items() if v]
    if not on:
        # 「還沒設定推播」是一種狀態，不是錯誤。回非零會讓 CI 介面出現紅字，
        # 看起來像壞掉了，但其實只是使用者還沒填 secrets。
        print("沒有啟用任何推播管道，跳過。", file=sys.stderr)
        print("要開啟的話，編輯 notify.config.json 的 channels，"
              "並在 GitHub Secrets 設定對應的 token。", file=sys.stderr)
        return

    fail = 0
    for k in on:
        ok, msg = SENDERS[k](text)
        print(f"  {k:<9} {'OK' if ok else '失敗'}  {msg if not ok else ''}", file=sys.stderr)
        fail += 0 if ok else 1
    print(f"推播完成：{len(on) - fail}/{len(on)} 成功，{n_sig} 筆自選訊號", file=sys.stderr)
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
