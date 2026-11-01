# -*- coding: utf-8 -*-
"""回補夜盤／費半歷史，產生 data/macro.csv（給 backtest_tw.py run --macro 使用）
來源：費半＝Yahoo chart API(^SOX)；夜盤＝期交所 台指期(TX) 盤後行情下載。
GitHub Actions 的機器可上網；Claude 雲端不行，所以這支只在 Actions 或你的電腦跑。
用法：python macro_fetch.py --start 2023-01-01 --out data/macro.csv
      python macro_fetch.py --selftest
輸出欄位：date,night_pct,sox_prev_pct,fut_short_chg（fut_short_chg 本版留空＝閘門(2)不會被回測）
"""
import argparse, io, os, sys, time, datetime as dt
import urllib.request, urllib.parse
import pandas as pd

UA = {"User-Agent": "Mozilla/5.0"}
# 已驗證錨點（2026-11-01 查證，兩家以上來源）：夜盤收盤 → 對應的台股交易日
ANCHORS = [(45926, "2026-10-30"), (44433, "2026-11-02")]

def http(url, data=None, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=UA)
            return urllib.request.urlopen(req, timeout=60).read()
        except Exception as e:
            if i == tries - 1: raise
            time.sleep(2 * (i + 1))

def fetch_sox(start, end):
    p1 = int(dt.datetime.fromisoformat(start).timestamp()) - 86400 * 7
    p2 = int(dt.datetime.fromisoformat(end).timestamp()) + 86400 * 2
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/%5ESOX?period1={p1}&period2={p2}&interval=1d"
    import json
    j = json.loads(http(url))["chart"]["result"][0]
    ts = j["timestamp"]; cl = j["indicators"]["quote"][0]["close"]
    s = pd.DataFrame({"us_date": [dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d") for t in ts], "close": cl}).dropna()
    s["pct"] = s.close.pct_change() * 100
    return s.reset_index(drop=True)

def fetch_tx_month(a, b):
    body = urllib.parse.urlencode({"down_type": "1", "commodity_id": "TX", "commodity_id2": "",
        "queryStartDate": a.strftime("%Y/%m/%d"), "queryEndDate": b.strftime("%Y/%m/%d")}).encode()
    raw = http("https://www.taifex.com.tw/cht/3/dlFutDataDown", data=body)
    for enc in ("big5", "utf-8-sig", "cp950"):
        try: return pd.read_csv(io.StringIO(raw.decode(enc)), dtype=str)
        except Exception: continue
    raise RuntimeError("無法解析期交所 CSV")

def parse_night(df):
    """從 TX 下載表取每日盤後(夜盤)主力月份：收盤、漲跌%。回傳 label_date, close, pct"""
    df = df.rename(columns=lambda c: c.strip())
    df = df[df["交易時段"].str.strip() == "盤後"].copy()
    df = df[~df["到期月份(週別)"].str.contains("/|W", na=False)]      # 排除價差單與週選
    df["vol"] = pd.to_numeric(df["成交量"].str.replace(",", ""), errors="coerce")
    df["close"] = pd.to_numeric(df["收盤價"].str.replace(",", ""), errors="coerce")
    df["pct"] = pd.to_numeric(df["漲跌%"].str.replace("%", "").str.replace(",", ""), errors="coerce")
    df = df.dropna(subset=["close", "pct"])
    df = df.sort_values(["交易日期", "vol"], ascending=[True, False]).groupby("交易日期").head(1)
    df["label"] = pd.to_datetime(df["交易日期"].str.replace("/", "-")).dt.strftime("%Y-%m-%d")
    return df[["label", "close", "pct"]].reset_index(drop=True)

def calibrate(night, tdates):
    """用錨點判斷「期交所盤後的日期標籤」是 D 當天還是 D 的前一個交易日。"""
    hit = {}
    for close, D in ANCHORS:
        r = night[(night.close - close).abs() < 0.5]
        if len(r): hit[D] = r.label.iloc[0]
    if not hit: raise SystemExit("錨點對不上：無法判斷夜盤日期標籤，請把輸出貼給 Claude，不要硬用。")
    D, L = next(iter(hit.items()))
    mode = "same" if L == D else "prev"
    print("日期標籤判斷：", mode, "（錨點", hit, "）")
    return mode

def build(start, end, out, index_csv="data/index.csv"):
    idx = pd.read_csv(index_csv); tdates = sorted(idx.date.astype(str))
    sox = fetch_sox(start, end)
    frames = []; cur = dt.date.fromisoformat(start); last = dt.date.fromisoformat(end)
    while cur <= last:
        nxt = min((cur.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1), last)
        try: frames.append(parse_night(fetch_tx_month(cur, nxt)))
        except Exception as e: print("跳過", cur, e)
        cur = nxt + dt.timedelta(days=1); time.sleep(1.5)
    night = pd.concat(frames).drop_duplicates("label").reset_index(drop=True)
    mode = calibrate(night, tdates)
    rows = []
    for D in tdates:
        if D < start: continue
        if mode == "same": lab = D
        else:
            prev = [x for x in tdates if x < D]; lab = prev[-1] if prev else None
        n = night[night.label == lab]
        s = sox[sox.us_date < D].tail(1)
        rows.append({"date": D, "night_pct": round(float(n.pct.iloc[0]), 2) if len(n) else "",
                     "sox_prev_pct": round(float(s.pct.iloc[0]), 2) if len(s) and pd.notna(s.pct.iloc[0]) else "",
                     "fut_short_chg": ""})
    m = pd.DataFrame(rows); os.makedirs(os.path.dirname(out) or ".", exist_ok=True); m.to_csv(out, index=False)
    print("寫出", out, len(m), "列；夜盤缺", int((m.night_pct == "").sum()), "；費半缺", int((m.sox_prev_pct == "").sum()))

def selftest():
    csv = "交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,交易時段\n" \
          "2026/10/29,TX,202611,46000,46100,45900,45926,-643,-1.38%,50000,盤後\n" \
          "2026/10/29,TX,202611/202612,0,0,0,10,1,1%,100,盤後\n" \
          "2026/10/29,TX,202611,46500,46600,46400,46569,10,0.02%,90000,一般\n"
    n = parse_night(pd.read_csv(io.StringIO(csv), dtype=str))
    assert len(n) == 1 and n.close.iloc[0] == 45926 and abs(n.pct.iloc[0] + 1.38) < 1e-9
    print("selftest OK（只驗證解析；網路抓取未在此測）")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default=dt.date.today().isoformat()); ap.add_argument("--out", default="data/macro.csv")
    ap.add_argument("--selftest", action="store_true"); a = ap.parse_args()
    selftest() if a.selftest else build(a.start, a.end, a.out)
