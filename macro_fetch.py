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
    s = pd.DataFrame({"us_date": [dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d") for t in ts], "close": cl}).dropna()
    s["pct"] = s.close.pct_change() * 100
    return s.reset_index(drop=True)

def fetch_tx_month(a, b):
    body = urllib.parse.urlencode({"down_type": "1", "commodity_id": "TX", "commodity_id2": "",
        "queryStartDate": a.strftime("%Y/%m/%d"), "queryEndDate": b.strftime("%Y/%m/%d")}).encode()
    raw = http("https://www.taifex.com.tw/cht/3/dlFutDataDown", data=body)
    for enc in ("big5", "utf-8-sig", "cp950"):
        try: return pd.read_csv(io.StringIO(raw.decode(enc)), dtype=str, index_col=False)
        except Exception: continue
    raise RuntimeError("無法解析期交所 CSV")

def parse_night(df):
    """從 TX 下載表取每日盤後(夜盤)主力月份：收盤、漲跌%。回傳 label_date, close, pct"""
    df = df.rename(columns=lambda c: c.strip())
    if not df["交易日期"].astype(str).str.match(r"\d{4}/\d{1,2}/\d{1,2}").all():   # 資料列尾多一個逗號會讓欄位整體右移
        df = df.reset_index(); df.columns = list(df.columns[:1]) + list(df.columns[1:])
    sc = [c for c in df.columns if "時段" in c][0]
    mc = [c for c in df.columns if "到期月份" in c][0]
    if not getattr(parse_night, "_shown", False):
        parse_night._shown = True
        print("DEBUG 欄位:", list(df.columns)); print("DEBUG 交易時段值:", df[sc].astype(str).str.strip().unique().tolist())
        print("DEBUG 前3列:\n", df.head(3).to_string())
    df = df[~df[sc].astype(str).str.contains("一般")].copy()                 # 非日盤＝盤後
    df = df[~df[mc].astype(str).str.contains("/|W", na=False)]              # 排除價差單與週選
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
    if not hit:
        print("DEBUG 夜盤資料筆數:", len(night)); print("DEBUG 最後8筆:\n", night.tail(8).to_string())
        print("DEBUG 離錨點最近的5筆:\n", night.iloc[(night.close - ANCHORS[0][0]).abs().argsort()[:5]].to_string())
        raise SystemExit("錨點對不上：無法判斷夜盤日期標籤，請把 DEBUG 輸出貼給 Claude，不要硬用。")
    D, L = next(iter(hit.items()))
    mode = "same" if L == D else "prev"
    print("日期標籤判斷：", mode, "（錨點", hit, "）")
    return mode

def build(start, end, out, index_csv="data/index.csv", extra_dates=(), write=True):
    idx = pd.read_csv(index_csv); tdates = sorted(set(idx.date.astype(str)) | set(extra_dates))
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
    m = pd.DataFrame(rows)
    if not write: return m
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True); m.to_csv(out, index=False)
    print("寫出", out, len(m), "列；夜盤缺", int((m.night_pct == "").sum()), "；費半缺", int((m.sox_prev_pct == "").sum()))

def merge_macro(old, new, valid_dates):
    """把 new 併進 old：同日期只覆蓋 new 有值的欄位；old 中不在 valid_dates 的列（例如先前補的「今天」遇到休市）會被移除。"""
    cols = ["date", "night_pct", "sox_prev_pct", "fut_short_chg"]
    o = old.copy() if old is not None else pd.DataFrame(columns=cols)
    for c in cols:
        if c not in o.columns: o[c] = ""
    o = o[cols].astype(object); o["date"] = o["date"].astype(str); o = o.set_index("date")
    for r in new.itertuples(index=False):
        d = str(r.date)
        if d not in o.index: o.loc[d] = ["", "", ""]
        for c in ("night_pct", "sox_prev_pct"):
            v = getattr(r, c)
            if v != "" and pd.notna(v): o.at[d, c] = v
    o = o[o.index.isin(set(valid_dates))].sort_index().reset_index()
    return o[cols]

def update(out, index_csv="data/index.csv", start="2026-10-25"):
    """每天跑：只抓近期（含錨點 10/30、11/2 以便判斷日期標籤），併進現有 macro.csv。
    盤前（台北）若今天是平日且尚未在 index.csv，會多補一列「今天」（夜盤＝昨晚盤後、費半＝美股昨收）。"""
    today = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)).date()
    idx = pd.read_csv(index_csv); tdates = sorted(idx.date.astype(str))
    extra = [today.isoformat()] if today.weekday() < 5 and today.isoformat() > tdates[-1] else []
    new = build(start, today.isoformat(), out, index_csv, extra_dates=extra, write=False)
    old = pd.read_csv(out, dtype=str, keep_default_na=False) if os.path.exists(out) else None
    m = merge_macro(old, new, set(tdates) | set(extra))
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True); m.to_csv(out, index=False)
    print("更新", out, "共", len(m), "列；最後 3 列：\n", m.tail(3).to_string())

def selftest():
    csv = "交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,交易時段\n" \
          "2026/10/29,TX,202611,46000,46100,45900,45926,-643,-1.38%,50000,盤後\n" \
          "2026/10/29,TX,202611/202612,0,0,0,10,1,1%,100,盤後\n" \
          "2026/10/29,TX,202611,46500,46600,46400,46569,10,0.02%,90000,一般\n"
    csv2 = "\n".join([csv.split("\n")[0]] + [l + "," for l in csv.split("\n")[1:] if l])   # 模擬資料列尾多逗號
    n = parse_night(pd.read_csv(io.StringIO(csv2), dtype=str, index_col=False))
    assert len(n) == 1 and n.close.iloc[0] == 45926 and abs(n.pct.iloc[0] + 1.38) < 1e-9
    old = pd.DataFrame({"date": ["2026-10-30", "2026-11-03"], "night_pct": ["-1.38", "-0.5"], "sox_prev_pct": ["-1.42", "2.63"], "fut_short_chg": ["", ""]})
    new = pd.DataFrame({"date": ["2026-11-03", "2026-11-04", "2026-11-07"], "night_pct": [0.98, 0.5, ""], "sox_prev_pct": ["", -3.13, ""]})
    m = merge_macro(old, new, {"2026-10-30", "2026-11-03", "2026-11-04"})
    assert list(m.date) == ["2026-10-30", "2026-11-03", "2026-11-04"], m
    r3 = m[m.date == "2026-11-03"].iloc[0]; assert float(r3.night_pct) == 0.98 and float(r3.sox_prev_pct) == 2.63   # 新值覆蓋、舊值不被空白蓋掉
    assert float(m[m.date == "2026-11-04"].iloc[0].sox_prev_pct) == -3.13
    print("selftest OK（解析＋合併邏輯；網路抓取未在此測）")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default=dt.date.today().isoformat()); ap.add_argument("--out", default="data/macro.csv")
    ap.add_argument("--selftest", action="store_true"); ap.add_argument("--update", action="store_true"); a = ap.parse_args()
    if a.selftest: selftest()
    elif a.update: update(a.out)
    else: build(a.start, a.end, a.out)
