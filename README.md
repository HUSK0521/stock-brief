# 台股訊號早報

每個交易日自動掃描全台股 2,300+ 檔,找出法人異常買賣、外資與投信對做、融資接刀、量能異常,
產生一頁靜態網站。**完全不需要 API 金鑰**——資料全部來自證交所與櫃買中心的公開端點。

## 為什麼是這個架構

FinMind 免費層一次只能查一檔,全市場要 18,882 個 request(約 31.5 小時),做不到。
證交所自己的端點是「一天一個檔案、涵蓋全市場」:

```
每個交易日 6 個 request  →  全市場 2,300+ 檔
```

差三個數量級,而且不用金鑰、不用付費。

## 檔案

| | |
|---|---|
| `market.py` | 全市場資料層(證交所 + 櫃買),歷史抓過就永久快取在 `cache_mkt/` |
| `build_site.py` | 向量化算訊號 → 產生 `docs/index.html` |
| `site.template.html` | 網站樣板(`__DATA__` 會被換成當日資料) |
| `.github/workflows/daily.yml` | 每個交易日 17:00(台北)自動跑並提交 |
| `brief.py` | 舊的單一持股版(用 FinMind,有 ADR 溢價與借券費率訊號) |

本機跑一次:

```bash
pip install -r requirements.txt
python build_site.py
```

---

## 你要做的設定(一次就好)

### 1. 建 GitHub repo 並推上去

先在 GitHub 網頁上建一個新的 repo(**不要**勾 Add README),然後:

```bash
git remote add origin https://github.com/你的帳號/stock-brief.git
git branch -M main
git push -u origin main
```

### 2. 開啟 GitHub Pages

Repo → **Settings** → **Pages** →
Source 選 **Deploy from a branch**,Branch 選 **main**,資料夾選 **/docs** → Save。

一兩分鐘後網址會是:

```
https://你的帳號.github.io/stock-brief/
```

### 3. 允許 Actions 寫回 repo

Repo → **Settings** → **Actions** → **General** → 最下面
**Workflow permissions** 選 **Read and write permissions** → Save。

沒設這個的話,每日自動更新會在推送那一步失敗。

### 4.(選用)自訂網域

買好網域後,Settings → Pages → Custom domain 填進去,
再到網域商那邊把 DNS 指向 GitHub Pages。**要放廣告的話這步是必要的**——
AdSense 需要你自己擁有的網域。

---

## 之後怎麼運作

設定完就不用管了:

- 每個交易日台北時間 **17:00**(收盤與三大法人資料都已公布)自動執行
- 抓當天資料 → 重算全市場訊號 → 更新 `docs/index.html` → 自動提交
- GitHub Pages 偵測到變更會自動重新部署
- **不需要你的電腦開著,不需要 Claude**

想手動跑一次:Repo → Actions → 「每日產生早報」 → Run workflow。

## 訊號規則

| 類型 | 條件 |
|---|---|
| `retail` 散戶接刀 | 外資賣超(z≤−1)且融資餘額同步增加(z≥1) |
| `diverge` 法人對做 | 外資與投信方向相反,雙方 \|z\|≥0.8 |
| `z` 法人異常量 | 外資買賣超 2 ≤ \|z\| ≤ 8 |
| `streak` 連續買賣 | 投信連續同向 ≥5 天(逆價走勢加權) |
| `vol` 量能異常 | 成交量 ≥2.5× 或 ≤0.4× 20 日均量 |

### 三道雜訊防線(拿掉會出事)

1. **流動性**:20 日成交金額中位數 < 1,000 萬的不給訊號
2. **參與度**:20 天內外資實際有進出的天數 < 8 天的不給訊號
3. **z 上限 8**:超過幾乎都是分母趨近 0 造成的假訊號

沒有這三道時實測會出現「外資賣超 z=−48.46」這種不可能的數字,
而且冷門股的假訊號會佔滿整個排行榜(訊號數從 1,137 降到 440)。

另外**全市場排行不含 ETF**:ETF 的法人買賣超多來自造市商的申購贖回避險,
反映的是套利機制而不是對個股的看法。使用者自己的持股裡還是照常顯示。

## 資料正確性

所有欄位對照都拿 FinMind 交叉驗證過,不是照文件猜的:

- 台積電五個交易日的收盤價與融資餘額 —— 逐日完全吻合
- 櫃買的三大法人欄位名稱全是重複的「買進股數/賣出股數/買賣超股數」,沒有分組標籤,
  只能用位置取。用 6488 環球晶、8069 元太、3105 穩懋 三檔逐筆核對,
  確認 `idx4=外資、idx13=投信、idx22=自營商`

## 免責

本站為公開資料的整理與統計描述,**非投資建議**,不構成任何買賣要約。
資料來源:臺灣證券交易所、證券櫃檯買賣中心。
