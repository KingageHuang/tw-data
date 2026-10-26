#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest_tw.py — 台股短線選股規則回測（TWSE 上市股票，Windows / macOS / Linux 皆可）

用途：用多年歷史資料驗證「法人占量比」等規則與「日型閘門」是否真的有超額報酬，
      取代目前只看 10 天的做法。只用標準庫 + pandas + numpy。

三個步驟（都在同一個檔案）：
  1) 下載原始資料（可中斷續傳，約每交易日 10 秒；3 年約 2 小時，請放著跑）
       python backtest_tw.py download --start 2023-01-01 --end 2026-10-22 --data data
  2) 解析成 CSV（改解析規則不必重新下載）
       python backtest_tw.py build --data data
  3) 回測並輸出 output/summary.html
       python backtest_tw.py run --data data --out output [--macro macro.csv]
  另有離線自我檢查（用合成資料驗證程式本身，不是市場結果）：
       python backtest_tw.py selftest --edge 0.0      # 無任何訊號：應該全部不通過
       python backtest_tw.py selftest --edge 0.15     # 注入訊號：應該能偵測到

資料來源（與專案記錄的端點一致）：
  MI_INDEX（每日收盤行情＋加權指數）、T86（三大法人個股買賣超）、BFI82U（三大法人買賣金額）。
  這三個端點需從你本地的 Python 執行（雲端環境連不到）。T86 欄位 2017 年後版本才穩定，建議 start ≥ 2018-01-01。
  上櫃（TPEx）股票本版不含。

macro.csv（可選，用於夜盤／費半閘門；日期＝交易日，欄位名稱如下，缺的欄位自動略過）：
  date,night_pct,sox_prev_pct,fut_short_chg
  2026-10-22,1.00,1.97,-3676

重要假設（請在報告中對照）：
  * 次日報酬採 TWSE 的「漲跌價差／參考價」，已處理除權息，不會把除息當成下跌。
  * 進場模擬：限價買單＝前收×1.005；開盤低於前收×0.995×0.99 或高於前收×1.005×1.01 則跳過；
    當日停損＝前收×0.97（與目標同日觸及時，保守假設先停損）、目標＝前收×1.05，否則收盤出場。
  * 成本：手續費（折扣可調）＋證交稅（預設 0.3%）＋單邊滑價 0.1%。
  * 驗收標準（預先登記）：樣本外 ≥60 個交易日、扣成本後平均超額報酬 > 0 且 t 值 > 2。
"""
import argparse
import html as _html
import datetime as dt
import glob
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd

UA = {"User-Agent": "Mozilla/5.0"}
BASE = "https://www.twse.com.tw/rwd/zh"
URLS = {
    "mi": BASE + "/afterTrading/MI_INDEX?date={d}&type=ALLBUT0999&response=json",
    "t86": BASE + "/fund/T86?date={d}&selectType=ALLBUT0999&response=json",
    "bfi": BASE + "/fund/BFI82U?type=day&dayDate={d}&response=json",
}


# ------------------------------------------------------------------ 下載 ----
def http_json(url, retries=4, pause=6):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa
            last = e
            time.sleep(pause * (i + 1))
    print("  ! 下載失敗：", url, last)
    return None


def daterange(a, b):
    d = a
    while d <= b:
        if d.weekday() < 5:
            yield d
        d += dt.timedelta(days=1)


def cmd_download(a):
    raw = os.path.join(a.data, "raw")
    os.makedirs(raw, exist_ok=True)
    seen_p = os.path.join(a.data, "seen_dates.txt")  # 已處理完的日期（含休市日），可提交進 repo
    seen = set(open(seen_p).read().split()) if os.path.exists(seen_p) else set()
    s = dt.date.fromisoformat(a.start)
    e = dt.date.fromisoformat(a.end)
    kinds = ["mi", "t86"] + ([] if a.no_bfi else ["bfi"])
    t0 = time.time()
    for d in daterange(s, e):
        if d.isoformat() in seen:
            continue
        if a.max_minutes and time.time() - t0 > a.max_minutes * 60:
            print("達到時間上限，停止；下次續傳")
            break
        ds = d.strftime("%Y%m%d")
        ok = True
        for k in kinds:
            p = os.path.join(raw, f"{k}_{ds}.json")
            if os.path.exists(p):
                continue
            js = http_json(URLS[k].format(d=ds))
            time.sleep(a.sleep)  # 太快會被 TWSE 擋；不要調太小
            if js is None:
                ok = False
                continue  # 不寫入快取，下次續傳會重試
            with open(p, "w", encoding="utf-8") as f:
                json.dump(js, f, ensure_ascii=False)
        if ok and d < dt.date.today():  # 今天的資料可能還沒公布，不標記完成
            seen.add(d.isoformat())
            with open(seen_p, "a") as f:
                f.write(d.isoformat() + "\n")
        print("done", d)


# ------------------------------------------------------------------ 解析 ----
def num(x):
    if x is None:
        return np.nan
    s = re.sub(r"<[^>]+>", "", str(x)).replace(",", "").strip()
    if s in ("", "--", "---", "X0.00", "除權息"):
        return np.nan
    try:
        return float(s)
    except ValueError:
        return np.nan


def sign_of(x):
    s = re.sub(r"<[^>]+>", "", str(x))
    return 1 if "+" in s else (-1 if "-" in s else 0)


def pick_table(js, need, min_rows=0):
    """從 JSON 找出含必要欄位的表（rwd 版 MI_INDEX 為 tables，T86 為頂層 fields/data）。"""
    cands = []
    if "tables" in js:
        cands = js["tables"]
    elif "fields" in js and "data" in js:
        cands = [js]
    best = None
    for t in cands:
        f = t.get("fields") or []
        if all(any(n in c for c in f) for n in need) and len(t.get("data") or []) >= min_rows:
            if best is None or len(t["data"]) > len(best["data"]):
                best = t
    return best


def col(fields, *names, exclude=()):
    for n in names:
        for i, c in enumerate(fields):
            if n in c and not any(x in c for x in exclude):
                return i
    return None


def parse_quotes(js, date):
    t = pick_table(js, ["證券代號", "收盤價"], 300)
    if t is None:
        return None
    f = t["fields"]
    ix = dict(code=col(f, "證券代號"), name=col(f, "證券名稱"), sh=col(f, "成交股數"), val=col(f, "成交金額"),
              o=col(f, "開盤價"), h=col(f, "最高價"), l=col(f, "最低價"), c=col(f, "收盤價"),
              sg=col(f, "漲跌(+/-)"), df=col(f, "漲跌價差"))
    if any(v is None for k, v in ix.items() if k in ("code", "sh", "o", "h", "l", "c", "df")):
        raise RuntimeError("MI_INDEX 欄位不符，請檢查欄位：" + ",".join(f))
    rows = []
    for r in t["data"]:
        code = str(r[ix["code"]]).strip()
        c = num(r[ix["c"]])
        if not re.fullmatch(r"[1-9]\d{3}", code) or np.isnan(c):
            continue
        sg = sign_of(r[ix["sg"]]) if ix["sg"] is not None else 0
        chg = sg * (0 if np.isnan(num(r[ix["df"]])) else num(r[ix["df"]]))
        ref = c - chg
        rows.append(dict(date=date, code=code, name=str(r[ix["name"]]).strip(), open=num(r[ix["o"]]),
                         high=num(r[ix["h"]]), low=num(r[ix["l"]]), close=c, ref=ref,
                         ret=(chg / ref * 100 if ref and ref > 0 else np.nan),
                         vol_lots=num(r[ix["sh"]]) / 1000.0))
    return pd.DataFrame(rows)


def parse_taiex(js):
    if "tables" not in js:
        return np.nan
    for t in js["tables"]:
        f = t.get("fields") or []
        if any("收盤指數" in c for c in f):
            for r in t["data"]:
                if "發行量加權股價指數" in str(r[0]):
                    return num(r[col(f, "收盤指數")])
    return np.nan


def parse_t86(js, date):
    t = pick_table(js, ["證券代號", "投信買賣超股數"], 100)
    if t is None:
        return None
    f = t["fields"]
    i_code = col(f, "證券代號")
    i_f1 = col(f, "外陸資買賣超股數(不含外資自營商)", "外資買賣超股數", "外陸資買賣超股數")
    i_f2 = col(f, "外資自營商買賣超股數")
    i_t = col(f, "投信買賣超股數")
    i_d = col(f, "自營商買賣超股數", exclude=("自行買賣", "避險"))
    if None in (i_code, i_f1, i_t, i_d):
        raise RuntimeError("T86 欄位不符，請檢查欄位：" + ",".join(f))
    rows = []
    for r in t["data"]:
        code = str(r[i_code]).strip()
        if not re.fullmatch(r"[1-9]\d{3}", code):
            continue
        fo = num(r[i_f1]) + (0 if i_f2 is None or np.isnan(num(r[i_f2])) else num(r[i_f2]))
        rows.append(dict(date=date, code=code, foreign=fo / 1000.0, trust=num(r[i_t]) / 1000.0,
                         dealer=num(r[i_d]) / 1000.0))
    return pd.DataFrame(rows)


def parse_bfi(js, date):
    if "data" not in js or "fields" not in js:
        return None
    f = js["fields"]
    i_diff = col(f, "買賣差額")
    if i_diff is None:
        return None
    fo = tr = de = 0.0
    for r in js["data"]:
        name = str(r[0])
        v = num(r[i_diff])
        if np.isnan(v):
            continue
        if "外資" in name:
            fo += v
        elif "投信" in name:
            tr += v
        elif "自營商" in name:
            de += v
    return dict(date=date, foreign_net=fo / 1e8, trust_net=tr / 1e8, dealer_net=de / 1e8)


def cmd_build(a):
    raw = os.path.join(a.data, "raw")
    dates = sorted({re.search(r"_(\d{8})\.json", p).group(1) for p in glob.glob(os.path.join(raw, "mi_*.json"))})
    old = {}
    for nm in ("daily", "index", "bfi"):  # 增量：已建好的日期不重解析（raw 可以不保留）
        fp = os.path.join(a.data, nm + ".csv")
        old[nm] = pd.read_csv(fp, dtype={"code": str}) if os.path.exists(fp) else None
    have = set(old["daily"]["date"]) if old["daily"] is not None else set()
    dates = [x for x in dates if f"{x[:4]}-{x[4:6]}-{x[6:]}" not in have]
    q, fl, idx, bf = [], [], [], []
    for ds in dates:
        d = f"{ds[:4]}-{ds[4:6]}-{ds[6:]}"
        js = json.load(open(os.path.join(raw, f"mi_{ds}.json"), encoding="utf-8"))
        qd = parse_quotes(js, d)
        if qd is None or qd.empty:
            continue  # 休市日
        q.append(qd)
        idx.append(dict(date=d, taiex=parse_taiex(js)))
        p = os.path.join(raw, f"t86_{ds}.json")
        if os.path.exists(p):
            fd = parse_t86(json.load(open(p, encoding="utf-8")), d)
            if fd is not None:
                fl.append(fd)
        p = os.path.join(raw, f"bfi_{ds}.json")
        if os.path.exists(p):
            b = parse_bfi(json.load(open(p, encoding="utf-8")), d)
            if b:
                bf.append(b)
    if not q and old["daily"] is None:
        sys.exit("沒有可解析的交易日資料，請先 download")
    if not q:
        print("沒有新的交易日")
        return
    df = pd.concat(q)
    if fl:
        df = df.merge(pd.concat(fl), on=["date", "code"], how="left")
    else:
        df[["foreign", "trust", "dealer"]] = np.nan
    def merge(nm, new):
        new = pd.DataFrame(new) if not isinstance(new, pd.DataFrame) else new
        if old[nm] is not None:
            new = pd.concat([old[nm], new])
        return new.sort_values("date").reset_index(drop=True)
    df = merge("daily", df)
    df.to_csv(os.path.join(a.data, "daily.csv"), index=False, encoding="utf-8-sig")
    merge("index", idx).to_csv(os.path.join(a.data, "index.csv"), index=False)
    if bf:
        merge("bfi", bf).to_csv(os.path.join(a.data, "bfi.csv"), index=False)
    print("交易日", len(q), "列數", len(df), "法人資料筆數", int(df["foreign"].notna().sum()))


# ------------------------------------------------------------------ 統計 ----
def wilson(w, n, z=1.96):
    if n == 0:
        return (np.nan, np.nan)
    p = w / n
    c = (p + z * z / 2 / n) / (1 + z * z / n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / 4 / n / n) / (1 + z * z / n)
    return (max(0, c - h), min(1, c + h))


def tstat(x):
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    if len(x) < 3 or x.std(ddof=1) == 0:
        return np.nan
    return x.mean() / (x.std(ddof=1) / math.sqrt(len(x)))


def maxdd(r):
    eq = np.cumsum(np.nan_to_num(np.asarray(r, dtype=float)))
    peak = np.maximum.accumulate(eq)
    return float((eq - peak).min()) if len(eq) else 0.0


def summarize(day, exposure=0.10):
    """day: 每個有成交日一列：net, gross, excess, n, wn, wg"""
    if day is None or len(day) == 0:
        return dict(days=0)
    trades = int(day["n"].sum())
    wn, wg = int(day["wn"].sum()), int(day["wg"].sum())
    lo, hi = wilson(wn, trades)
    return dict(days=len(day), trades=trades, win_net=wn / trades if trades else np.nan, win_net_lo=lo, win_net_hi=hi,
                win_gross=wg / trades if trades else np.nan, mean_net=day["net"].mean() * 100,
                mean_excess=day["excess"].mean() * 100, t_excess=tstat(day["excess"]), t_net=tstat(day["net"]),
                maxdd=maxdd(day["net"].values * exposure) * 100)


# ------------------------------------------------------------------ 資料 ----
def load_data(data):
    df = pd.read_csv(os.path.join(data, "daily.csv"), dtype={"code": str})
    df = df.sort_values(["code", "date"]).reset_index(drop=True)
    idx = pd.read_csv(os.path.join(data, "index.csv"))
    bfp = os.path.join(data, "bfi.csv")
    bfi = pd.read_csv(bfp) if os.path.exists(bfp) else None
    return df, idx, bfi


def prepare(df, min_vol=1000, min_price=10):
    dates = sorted(df["date"].unique())
    nxt = dict(zip(dates[:-1], dates[1:]))
    g = df.groupby("code")
    for c in ["date", "open", "high", "low", "close", "ref", "ret"]:
        df[c + "_n"] = g[c].shift(-1)
    df["valid"] = df["date_n"] == df["date"].map(nxt)
    vol = df["vol_lots"].replace(0, np.nan)
    df["inst"] = (df["foreign"] + df["trust"] + df["dealer"]) / vol
    df["fr"] = df["foreign"] / vol
    df["tr"] = df["trust"] / vol
    cl = lambda x, a, b: np.minimum(b, np.maximum(a, x))  # noqa
    p = df["ret"]
    mom = np.where(p >= 0, np.where(p > 8, np.maximum(15 - (p - 8) * 2, 4), p / 8 * 15), np.maximum(p, -8) / 8 * 15)
    df["v35"] = cl(50 + mom + cl(df["fr"] * 100 / 30 * 25, -25, 25) + cl(df["tr"] * 100 / 15 * 10, -10, 10)
                   + cl(df["dealer"] / vol * 100 / 10 * 5, -5, 5), 0, 100)
    tr = np.maximum(df["high"] - df["low"], np.maximum((df["high"] - df["ref"]).abs(), (df["low"] - df["ref"]).abs())) / df["ref"] * 100
    df["tr_pct"] = tr
    df["atr5"] = df.groupby("code")["tr_pct"].transform(lambda s: s.rolling(5).mean())
    df["mom"] = df["ret"]
    u = df[(df["vol_lots"] >= min_vol) & (df["close"] >= min_price) & df["valid"] & df["ret"].notna()
           & df["ret_n"].notna() & df["inst"].notna()].copy()
    return u, dates, nxt


def simulate(u, fee=0.001425, disc=0.6, tax=0.003, slip=0.001):
    """對每列（決策日 t 的股票）模擬次日進出場。回傳 net/gross（小數）、filled 旗標。"""
    ref = u["ref_n"]
    lo, hi = ref * 0.995, ref * 1.005
    o, h, l, c = u["open_n"], u["high_n"], u["low_n"], u["close_n"]
    skip = (o > hi * 1.01) | (o < lo * 0.99) | o.isna() | h.isna() | l.isna()
    fill = np.where(o <= hi, o, np.where(l <= hi, hi, np.nan))
    fill = np.where(skip, np.nan, fill)
    stop, tgt = ref * 0.97, ref * 1.05
    stopped = l <= stop
    hit = h >= tgt
    exit_p = np.where(stopped, stop, np.where(hit, tgt, c))
    gross = exit_p / fill - 1
    cost = 2 * slip + 2 * fee * disc + tax
    u["filled"] = ~np.isnan(fill)
    u["gross"] = gross
    u["net"] = gross - cost
    return u


def day_series(u, score, K, filt, seed=0, draws=1, gate_days=None):
    """每日取分數前 K 名，回傳每日彙總（有成交者）。excess＝日平均淨報酬 − 當日全池平均 c2c 報酬。"""
    mkt = u.groupby("date")["ret_n"].mean() / 100
    out = []
    for d_i in range(draws):
        s = score.copy()
        if filt is not None:
            s = s.where(filt)
        if seed is not None and draws > 1:
            s = pd.Series(np.random.default_rng(seed + d_i).random(len(u)), index=u.index)
            if filt is not None:
                s = s.where(filt)
        rk = s.groupby(u["date"]).rank(ascending=False, method="first")
        pk = u[(rk <= K) & u["filled"]]
        if gate_days is not None:
            pk = pk[pk["date"].isin(gate_days)]
        agg = pk.groupby("date").agg(net=("net", "mean"), gross=("gross", "mean"), n=("net", "size"),
                                     wn=("net", lambda x: (x > 0).sum()), wg=("gross", lambda x: (x > 0).sum()))
        agg["excess"] = agg["net"] - mkt.reindex(agg.index)
        out.append(agg)
    if draws == 1:
        return out[0]
    cat = pd.concat(out)
    m = cat.groupby(level=0).mean()
    m["n"] = cat.groupby(level=0)["n"].mean().round().astype(int)
    return m


def strategies(u, K, seed):
    f6 = u["mom"] <= 6
    S = {}
    for nm, sc in [("inst_ratio", u["inst"]), ("foreign_ratio", u["fr"]), ("trust_ratio", u["tr"]), ("v35", u["v35"]),
                   ("mom_high", u["mom"]), ("mom_low", -u["mom"]), ("atr_high", u["atr5"]), ("atr_low", -u["atr5"])]:
        for fn, flt in [("all", None), ("chg≤6", f6)]:
            S[f"{nm}|{fn}|K{K}"] = day_series(u, sc, K, flt)
    S[f"random|all|K{K}"] = day_series(u, u["inst"], K, None, seed=seed, draws=10)
    base = S[f"random|all|K{K}"]["net"]
    for n, s in S.items():
        s["excess_mkt"] = s["excess"]                         # 對全池 c2c 平均（含成本落差）
        s["excess"] = s["net"] - base.reindex(s.index)        # 對「同成本、同進場規則」的隨機選股
    return S


# ------------------------------------------------------------------ 閘門 ----
def day_features(u, idx, bfi, macro, dates, nxt):
    d = pd.DataFrame(index=sorted(u["date"].unique()))
    d["breadth_t"] = u.groupby("date")["ret"].apply(lambda x: (x > 0).mean())
    d["up_next"] = u.groupby("date")["ret_n"].apply(lambda x: (x > 0).mean())
    ix = idx.set_index("date")["taiex"].astype(float)
    ix = ix[~ix.index.duplicated()]
    d["taiex"] = ix.reindex(d.index)
    d["ma60"] = ix.rolling(60).mean().reindex(d.index)
    d["taiex_ret_t"] = ix.pct_change().reindex(d.index) * 100
    d["taiex_ret_next"] = (ix.shift(-1) / ix - 1).reindex(d.index) * 100
    if bfi is not None:
        b = bfi.drop_duplicates("date").set_index("date")
        d["foreign_net_t"] = b["foreign_net"].reindex(d.index)
    if macro is not None:
        prev = {v: k for k, v in nxt.items()}
        m = macro.copy()
        m["asof"] = m["date"].map(prev)
        m = m.dropna(subset=["asof"]).drop_duplicates("asof").set_index("asof")
        for c in ("night_pct", "sox_prev_pct", "fut_short_chg"):
            if c in m.columns:
                d[c] = m[c].reindex(d.index)
    return d


def gate_rules(d):
    R = {}
    if "foreign_net_t" in d:
        R["外資前日賣超 <300 億"] = d["foreign_net_t"] > -300
    R["前日加權 ≥ 60MA"] = d["taiex"] >= d["ma60"]
    if {"night_pct", "sox_prev_pct"} <= set(d.columns):
        R["夜盤≥+0.3% 且 前晚費半≥+0.5%"] = (d["night_pct"] >= 0.3) & (d["sox_prev_pct"] >= 0.5)
    if "fut_short_chg" in d:
        R["外資期貨空單不增"] = d["fut_short_chg"] <= 0
    keys = list(R)
    if len(keys) >= 2:
        allm = R[keys[0]]
        for k in keys[1:]:
            allm = allm & R[k]
        R["以上全部通過"] = allm
    for k in list(R):
        R[k] = R[k].fillna(False)
    return R


# ------------------------------------------------------------------ 走動式 ----
def walk_forward(S, train=250, test=60, min_train_days=60):
    names = list(S)
    dates = sorted(set().union(*[set(S[n].index) for n in names]))
    X = pd.DataFrame({n: S[n]["excess"] for n in names}).reindex(dates)
    N = pd.DataFrame({n: S[n]["net"] for n in names}).reindex(dates)
    Wn = pd.DataFrame({n: S[n]["wn"] for n in names}).reindex(dates)
    Tn = pd.DataFrame({n: S[n]["n"] for n in names}).reindex(dates)
    oos, picks = [], []
    i = train
    while i < len(dates):
        tr_d, te_d = dates[i - train:i], dates[i:i + test]
        best, bt = None, -1e9
        for n in names:
            if n.startswith("random"):
                continue
            x = X.loc[tr_d, n].dropna()
            if len(x) < min_train_days:
                continue
            t = tstat(x)
            if not np.isnan(t) and t > bt:
                best, bt = n, t
        if best is not None:
            seg = pd.DataFrame({"net": N.loc[te_d, best], "excess": X.loc[te_d, best], "wn": Wn.loc[te_d, best],
                                "n": Tn.loc[te_d, best]}).dropna(subset=["net"])
            seg["gross"] = seg["net"]
            seg["wg"] = seg["wn"]
            oos.append(seg)
            picks.append((te_d[0], te_d[-1], best, round(bt, 2)))
        i += test
    return (pd.concat(oos) if oos else None), picks


# ------------------------------------------------------------------ 報告 ----
def fmt(v, p=2, pct=False):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"{v*100:.1f}%" if pct else f"{v:.{p}f}"


def table(headers, rows):
    esc = lambda c: c if (isinstance(c, str) and c.startswith("<span")) else _html.escape(str(c))  # noqa
    h = "".join(f"<th>{_html.escape(str(x))}</th>" for x in headers)
    b = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f"<div class='tw'><table><tr>{h}</tr>{b}</table></div>"


def verdict(st, min_days=60):
    if st.get("days", 0) < min_days:
        return "<span class='no'>樣本不足</span>"
    ok = st["mean_excess"] > 0 and st["t_excess"] > 2
    if ok and st["mean_net"] <= 0:
        return "<span class='ok'>有選股能力</span> <span class='no'>但淨報酬仍 ≤0</span>"
    return "<span class='ok'>通過</span>" if ok else "<span class='no'>未通過</span>"


def srow(name, st):
    if st.get("days", 0) == 0:
        return [name, 0, "—", "—", "—", "—", "—", "—", "—"]
    return [name, st["days"], st["trades"], f"{st['win_net']*100:.1f}% ({st['win_net_lo']*100:.0f}–{st['win_net_hi']*100:.0f})",
            f"{st['win_gross']*100:.1f}%", f"{st['mean_net']:+.2f}%", f"{st['mean_excess']:+.2f}%", fmt(st["t_excess"]),
            f"{st['maxdd']:.1f}%"]


HEAD = ["策略", "交易日數", "筆數", "淨勝率(95%CI)", "毛勝率", "日均淨報酬", "日均超額(對隨機同成本)", "t(超額)", "最大回檔(10%曝險)"]


def cmd_run(a):
    df, idx, bfi = load_data(a.data)
    if a.start:
        df = df[df["date"] >= a.start]
    if a.end:
        df = df[df["date"] <= a.end]
    macro = pd.read_csv(a.macro) if a.macro and os.path.exists(a.macro) else None
    u, dates, nxt = prepare(df, a.min_vol, a.min_price)
    u = simulate(u, a.fee, a.discount, a.tax, a.slip)
    S = strategies(u, a.k, a.seed)
    D = day_features(u, idx, bfi, macro, dates, nxt)
    G = gate_rules(D)
    os.makedirs(a.out, exist_ok=True)
    html = [f"<h1>回測摘要</h1><div class='sub'>資料 {dates[0]} ～ {dates[-1]}（{len(dates)} 個交易日）｜樣本股票 {u['code'].nunique()} 檔｜"
            f"每日平均可選 {len(u)/max(1, u['date'].nunique()):.0f} 檔｜K={a.k}｜成本：稅 {a.tax*100:.2f}%、滑價單邊 {a.slip*100:.2f}%</div>"]
    # 日型分佈
    ups = D["up_next"].dropna()
    bins = [("<30%（崩盤型）", (ups < 0.30).sum()), ("30–60%", ((ups >= 0.30) & (ups <= 0.60)).sum()), (">60%（上漲型）", (ups > 0.60).sum())]
    html.append("<h2>一、日型分佈（全池次日上漲比例）</h2>" + table(["區間", "天數", "占比"], [[n, int(c), f"{c/len(ups)*100:.0f}%"] for n, c in bins])
                + f"<div class='sub'>平均 {ups.mean()*100:.1f}%；標準差 {ups.std()*100:.1f}pp。這個分佈決定了單日勝率的天花板。</div>")
    # 全樣本（樣本內）
    rows = [srow(n, summarize(s)) + [verdict(summarize(s))] for n, s in S.items()]
    rows.sort(key=lambda r: -(float(r[6].replace("%", "").replace("+", "")) if r[6] != "—" else -99))
    html.append("<h2>二、全樣本（樣本內，僅供參考，含選擇偏誤）</h2>" + table(HEAD + ["驗收"], rows))
    # 走動式
    oos, picks = walk_forward(S, a.train, a.test)
    st = summarize(oos) if oos is not None else dict(days=0)
    html.append("<h2>三、走動式樣本外（每 %d 日重選，訓練窗 %d 日）</h2>" % (a.test, a.train)
                + table(HEAD + ["驗收"], [srow("走動式 OOS", st) + [verdict(st)]])
                + "<div class='sub'>只有這一段能當成「規則有沒有用」的證據。驗收：樣本外 ≥60 個交易日、日均超額 > 0 且 t > 2。</div>"
                + table(["測試期起", "測試期迄", "選到的策略", "訓練 t"], [[p[0], p[1], p[2], p[3]] for p in picks]))
    # 日型分層（以全樣本最佳 inst_ratio 為例）
    ref_name = f"inst_ratio|chg≤6|K{a.k}"
    rs = S[ref_name]
    rg = pd.DataFrame({"x": rs["excess"], "net": rs["net"], "wn": rs["wn"], "n": rs["n"], "up": D["up_next"].reindex(rs.index)})
    rg["bin"] = pd.cut(rg["up"], [-0.01, 0.30, 0.60, 1.01], labels=["<30%", "30–60%", ">60%"])
    reg = []
    for b, g in rg.groupby("bin", observed=True):
        reg.append([b, len(g), f"{g['wn'].sum()/g['n'].sum()*100:.1f}%" if g['n'].sum() else "—", f"{g['net'].mean()*100:+.2f}%", f"{g['x'].mean()*100:+.2f}%"])
    html.append("<h2>四、日型分層（%s，事後分層）</h2>" % _html.escape(ref_name) + table(["日型", "天數", "淨勝率", "日均淨報酬", "日均超額"], reg))
    # 閘門
    grows = []
    for name, m in G.items():
        m = m.reindex(D.index).fillna(False)
        for lab, mm in (("通過", m), ("未通過", ~m)):
            ds = D.index[mm]
            sub = rs[rs.index.isin(ds)]
            st2 = summarize(sub)
            grows.append([name, lab, int(mm.sum()), fmt(D.loc[mm, "up_next"].mean(), pct=True),
                          fmt(D.loc[mm, "taiex_ret_next"].mean(), 2) + "%", f"{st2['win_net']*100:.1f}%" if st2.get("days") else "—",
                          f"{st2['mean_net']:+.2f}%" if st2.get("days") else "—"])
    html.append("<h2>五、日型閘門檢驗（決策日 t 已知條件 → 次日結果）</h2>"
                + table(["規則", "組別", "天數", "次日全池上漲比例", "次日加權平均報酬", "參考策略淨勝率", "參考策略日均淨報酬"], grows)
                + "<div class='sub'>好的閘門：「通過」組的次日全池上漲比例與加權報酬明顯高於「未通過」，且差異在不同年份都存在。"
                  "沒有給 macro.csv 時，夜盤／費半／期貨空單三條件不會出現。</div>")
    orc = D.index[D["up_next"] > 0.5]
    st3 = summarize(rs[rs.index.isin(orc)])
    html.append("<h2>六、上限參考：若能事前知道次日是上漲型（事後 oracle）</h2>"
                + table(HEAD, [srow(ref_name + "｜只在上漲型日交易（事後）", st3)])
                + "<div class='sub'>這是不可實現的上限，用來衡量「日型預測」值多少。與第二節同一策略比較。</div>")
    html.append("<h2>七、限制</h2><ul><li>僅 TWSE 上市股票（不含上櫃）；使用當日收盤後資料作為隔日決策依據，與實際報告一致。</li>"
                "<li>進場以限價與跳空規則模擬，未模擬盤中成交順序、漲跌停鎖死與零股；停損與目標同時觸及時假設先停損。</li>"
                "<li>每日超額報酬彼此相關（同一波行情），t 值可能偏高；請以走動式樣本外與多年度一致性為準。</li>"
                "<li>策略集合有數十個，樣本內最佳者必然含運氣成分，請只信任第三節。</li></ul>")
    css = ("<style>:root{--bg:#0b1220;--p:#121a2b;--b:#26324a;--t:#e6edf7;--d:#8fa0bd;--ok:#38c98a;--no:#ff6b6b}"
           "@media(prefers-color-scheme:light){:root{--bg:#f4f6fb;--p:#fff;--b:#d6dcea;--t:#16213a;--d:#5d6b85;--ok:#0d8a56;--no:#c62f2f}}"
           "body{margin:0;background:var(--bg);color:var(--t);font:14px/1.6 system-ui,'Microsoft JhengHei',sans-serif;padding:16px}"
           ".w{max-width:1150px;margin:auto}h1{font-size:20px}h2{font-size:15px;color:var(--d);margin:22px 0 8px}.sub{color:var(--d);font-size:12px;margin:6px 0}"
           ".tw{overflow-x:auto;background:var(--p);border:1px solid var(--b);border-radius:10px}table{border-collapse:collapse;width:100%;font-size:13px}"
           "th,td{padding:6px 9px;border-bottom:1px solid var(--b);text-align:left;white-space:nowrap}th{color:var(--d)}.ok{color:var(--ok);font-weight:700}.no{color:var(--no);font-weight:700}</style>")
    page = f"<!DOCTYPE html><html lang='zh-Hant'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>回測摘要</title>{css}</head><body><div class='w'>{''.join(html)}</div></body></html>"
    open(os.path.join(a.out, "summary.html"), "w", encoding="utf-8").write(page)
    pd.DataFrame({n: s["net"] for n, s in S.items()}).to_csv(os.path.join(a.out, "daily_net_by_strategy.csv"))
    D.to_csv(os.path.join(a.out, "day_features.csv"))
    print("完成：", os.path.join(a.out, "summary.html"))


# ------------------------------------------------------------------ 自我檢查 ----
def cmd_selftest(a):
    """合成資料：隨機日型＋個股 β＋（可選）法人訊號。只驗證程式與統計是否運作，不是市場結果。"""
    rng = np.random.default_rng(a.seed)
    ND, NS = a.days, 300
    start = dt.date(2021, 1, 4)
    dts = []
    d = start
    while len(dts) < ND:
        if d.weekday() < 5:
            dts.append(d)
        d += dt.timedelta(days=1)
    crash = rng.random(ND) < 0.25
    mkt = np.where(crash, rng.normal(-1.6, 0.8, ND), rng.normal(0.5, 0.8, ND))
    beta = rng.uniform(0.3, 1.7, NS)
    px = rng.uniform(15, 300, NS)
    rows = []
    flow_prev = rng.normal(0, 1, NS)
    ixv = 10000.0
    idxr, bfr = [], []
    for t, day in enumerate(dts):
        ret = beta * mkt[t] + rng.normal(0, 1.4, NS) + a.edge * flow_prev
        ret = np.clip(ret, -9.5, 9.5)
        ref = px.copy()
        close = np.round(ref * (1 + ret / 100), 2)
        opn = np.round(ref * (1 + 0.35 * ret / 100 + rng.normal(0, 0.25, NS) / 100), 2)
        hi = np.maximum.reduce([close, opn]) * (1 + np.abs(rng.normal(0, 0.5, NS)) / 100)
        lo = np.minimum.reduce([close, opn]) * (1 - np.abs(rng.normal(0, 0.5, NS)) / 100)
        vol = np.exp(rng.normal(7.8, 1.0, NS))
        z = rng.normal(0, 1, NS)
        fr = z * vol * 0.12
        tr_ = rng.normal(0, 1, NS) * vol * 0.03
        de = rng.normal(0, 1, NS) * vol * 0.03
        for i in range(NS):
            rows.append((day.isoformat(), f"{1101+i}", f"S{i}", opn[i], hi[i], lo[i], close[i], ref[i], ret[i], vol[i], fr[i], tr_[i], de[i]))
        flow_prev = z
        px = close
        ixv *= 1 + mkt[t] / 100
        idxr.append((day.isoformat(), ixv))
        bfr.append((day.isoformat(), float(fr.sum() / 10), 0.0, 0.0))
    os.makedirs(a.data, exist_ok=True)
    pd.DataFrame(rows, columns=["date", "code", "name", "open", "high", "low", "close", "ref", "ret", "vol_lots", "foreign", "trust", "dealer"]).to_csv(os.path.join(a.data, "daily.csv"), index=False)
    pd.DataFrame(idxr, columns=["date", "taiex"]).to_csv(os.path.join(a.data, "index.csv"), index=False)
    pd.DataFrame(bfr, columns=["date", "foreign_net", "trust_net", "dealer_net"]).to_csv(os.path.join(a.data, "bfi.csv"), index=False)
    print("已產生合成資料 ->", a.data, "（edge=%s）" % a.edge)
    a.macro = None
    a.start = a.end = None
    cmd_run(a)


# ------------------------------------------------------------------ CLI ----
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    d = sp.add_parser("download")
    d.add_argument("--start", required=True)
    d.add_argument("--end", required=True)
    d.add_argument("--data", default="data")
    d.add_argument("--sleep", type=float, default=3.5)
    d.add_argument("--no-bfi", action="store_true")
    d.add_argument("--max-minutes", type=float, default=0, help="超過就停止，下次續傳（給 GitHub Actions 用）")
    b = sp.add_parser("build")
    b.add_argument("--data", default="data")
    for nm in ("run", "selftest"):
        r = sp.add_parser(nm)
        r.add_argument("--data", default="data" if nm == "run" else "selftest_data")
        r.add_argument("--out", default="output" if nm == "run" else "selftest_output")
        r.add_argument("--macro", default=None)
        r.add_argument("--start", default=None)
        r.add_argument("--end", default=None)
        r.add_argument("--k", type=int, default=3)
        r.add_argument("--min-vol", dest="min_vol", type=float, default=1000)
        r.add_argument("--min-price", dest="min_price", type=float, default=10)
        r.add_argument("--fee", type=float, default=0.001425)
        r.add_argument("--discount", type=float, default=0.6)
        r.add_argument("--tax", type=float, default=0.003)
        r.add_argument("--slip", type=float, default=0.001)
        r.add_argument("--train", type=int, default=250)
        r.add_argument("--test", type=int, default=60)
        r.add_argument("--seed", type=int, default=0)
        if nm == "selftest":
            r.add_argument("--edge", type=float, default=0.0)
            r.add_argument("--days", type=int, default=600)
    a = ap.parse_args()
    {"download": cmd_download, "build": cmd_build, "run": cmd_run, "selftest": cmd_selftest}[a.cmd](a)


if __name__ == "__main__":
    main()
