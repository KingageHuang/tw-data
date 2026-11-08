# -*- coding: utf-8 -*-
"""月營收／EPS 事件資料抓取（給 GitHub Actions 用；雲端 Claude 環境連不到 FinMind，所以這支沒在真實 API 上跑過）。
用法：
  python fetch_events.py --probe            # 只抓 3 檔、印出原始回應格式，不寫檔（第一次先跑這個）
  python fetch_events.py --run              # 正式抓取，可續跑（已存在的檔案略過），到時間預算就停
環境變數 FINMIND_TOKEN（選用；免費註冊可提高每小時額度）。
輸出：data/events/rev_<code>.csv、data/events/eps_<code>.csv、data/events/progress.txt
"""
import os, sys, time, json, argparse, io
import urllib.request, urllib.parse
import pandas as pd
API="https://api.finmindtrade.com/api/v4/data"
DS={'rev':('TaiwanStockMonthRevenue','2022-01-01'),'eps':('TaiwanStockFinancialStatements','2022-01-01')}
def call(dataset,code,start,token=None):
    q={'dataset':dataset,'data_id':code,'start_date':start}
    if token: q['token']=token
    url=API+'?'+urllib.parse.urlencode(q)
    for k in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'tw-data-events'}),timeout=30) as r:
                j=json.loads(r.read().decode('utf-8'))
            if j.get('status')==200: return j.get('data',[]),None
            err=f"status={j.get('status')} msg={j.get('msg')}"
        except Exception as e: err=repr(e)
        if any(s in err for s in ('402','429','limit','Requests reach')):
            return None,'RATE:'+err
        time.sleep(5*(k+1))
    return None,'FAIL:'+err
def universe(daily='data/daily.csv',n_min=0.5,vol=2000):
    d=pd.read_csv(daily,dtype={'code':str}); d=d[d.code.str.fullmatch(r'\d{4}')]
    last=sorted(d.date.unique())[-250:]; d=d[d.date.isin(last)]
    share=d.groupby('code').apply(lambda g:(g.vol_lots>=vol).mean()); return sorted(share[share>=n_min].index)
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--probe',action='store_true'); ap.add_argument('--run',action='store_true')
    ap.add_argument('--budget_min',type=float,default=300); ap.add_argument('--per_hour',type=int,default=280); a=ap.parse_args()
    tok=os.environ.get('FINMIND_TOKEN') or None
    if tok and a.per_hour==280: a.per_hour=550
    os.makedirs('data/events',exist_ok=True)
    if a.probe:
        for code in ['2330','2412','3017']:
            for key,(ds,st) in DS.items():
                rows,err=call(ds,code,'2026-01-01',tok); print('==',code,key,'err=',err,'n=',None if rows is None else len(rows))
                if rows: print(json.dumps(rows[:2],ensure_ascii=False)[:700])
                time.sleep(2)
        return
    t0=time.time(); gap=3600.0/a.per_hour; calls=0; stop=False
    codes=universe(); print('universe',len(codes))
    for key in ('rev','eps'):
        ds,st=DS[key]
        for code in codes:
            fn=f'data/events/{key}_{code}.csv'
            if os.path.exists(fn): continue
            if (time.time()-t0)/60>a.budget_min: stop=True;break
            rows,err=call(ds,code,st,tok); calls+=1
            if err and err.startswith('RATE'):
                print('rate limited, sleeping 10 min',err); time.sleep(600); rows,err=call(ds,code,st,tok)
                if err: print('stop:',err); stop=True; break
            if err: print('skip',code,key,err); continue
            pd.DataFrame(rows).to_csv(fn,index=False)
            time.sleep(gap)
        if stop: break
    n={k:len([f for f in os.listdir('data/events') if f.startswith(k+'_')]) for k in DS}
    open('data/events/progress.txt','w').write(f"{time.strftime('%Y-%m-%d %H:%M')} calls={calls} files={n} universe={len(codes)} stopped_early={stop}\n"); print(open('data/events/progress.txt').read())
if __name__=='__main__': main()
