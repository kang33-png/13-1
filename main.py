"""
13F 持仓追踪后端 - 真实 SEC EDGAR 数据 + 季度历史 + 环比变动
运行：uvicorn main:app --host 0.0.0.0 --port $PORT
适配免费托管平台（内存缓存，无需写磁盘）
"""
import time
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel

import sec_client as sc

app = FastAPI(title="13F Tracker API")

# ============ 内存缓存（进程重启清空，免费平台可接受） ============
# cache[(cik, quarter)] = list[dict] 持仓
_cache: dict[tuple[str, str], list[dict]] = {}
_meta: dict[str, str] = {}


# ============ 模型 ============
class Fund(BaseModel):
    cik: str
    name: str
    totalValue: float
    quarter: str
    holderCount: int

class Holding(BaseModel):
    ticker: str
    companyName: str
    shares: int
    value: float
    weight: float
    changeType: str
    changePercent: float

class QuarterInfo(BaseModel):
    quarter: str
    filingDate: str

# 常用热门机构（CIK），供默认列表
POPULAR = [
    ("0001067983", "Berkshire Hathaway"),
    ("0001948323", "Bridgewater Associates"),
    ("0001653482", "ARK Investment Management"),
    ("0001037389", "Renaissance Technologies"),
    ("0001311335", "Two Sigma Investments"),
]

def quarter_label(filing: dict) -> str:
    rd = filing.get("report_quarter") or ""
    if rd and len(rd) >= 10:
        y, m = rd[:4], int(rd[5:7])
        q = (m - 1) // 3 + 1
        return f"{y}Q{q}"
    return filing["date"][:7]

def _cache_one(cik: str, f: dict) -> None:
    q = quarter_label(f)
    if (cik, q) in _cache:
        return
    try:
        xml_url = sc._find_info_table_xml(f["cik"], f["accession"], f["primary_doc"])
        if not xml_url:
            return
        xml_text = sc._get(xml_url).text
        holdings = sc.parse_info_table(xml_text)
        cusips = [h["cusip"] for h in holdings]
        ticker_map = sc.figi_cusip_to_ticker(cusips)
        rows = []
        for h in holdings:
            tk = ticker_map.get(h["cusip"], h["cusip"])
            rows.append({
                "ticker": tk, "company_name": h["companyName"],
                "shares": h["shares"], "value": h["value"]
            })
        _cache[(cik, q)] = rows
        _meta[cik] = f["name"]
    except Exception as e:
        print("缓存失败", cik, q, e)

def load_quarter(cik: str, quarter: str) -> list[dict]:
    return _cache.get((cik, quarter), [])

def diff_holdings(prev: dict[str, float], curr: dict[str, float]):
    result = {}
    for t in set(prev) | set(curr):
        old, new = prev.get(t, 0.0), curr.get(t, 0.0)
        if old == 0 and new > 0:
            result[t] = ("NEW", 100.0)
        elif new == 0 and old > 0:
            result[t] = ("CLOSED", -100.0)
        elif new > old:
            result[t] = ("ADDED", (new - old) / old * 100)
        elif new < old:
            result[t] = ("TRIMMED", (new - old) / old * 100)
        else:
            result[t] = ("HOLD", 0.0)
    return result

# ============ 接口 ============
@app.get("/api/funds", response_model=list[Fund])
def get_funds(q: Optional[str] = None):
    out = []
    for cik, name in POPULAR:
        out.append(Fund(cik=cik, name=name, totalValue=0.0,
                        quarter="", holderCount=0))
    if q:
        out = [f for f in out if q.lower() in f.name.lower() or q in f.cik]
    return out

@app.get("/api/funds/{cik}/quarters", response_model=list[QuarterInfo])
def get_quarters(cik: str):
    filings = sc.list_13f_filings(cik)
    out = []
    for f in filings:
        out.append(QuarterInfo(quarter=quarter_label(f), filingDate=f["date"]))
    return out

@app.get("/api/funds/{cik}/holdings", response_model=list[Holding])
def get_holdings(cik: str, quarter: Optional[str] = None):
    filings = sc.list_13f_filings(cik)
    qmap = {quarter_label(f): f for f in filings}
    qs = list(qmap.keys())
    target = quarter or qs[0]
    for q in [target] + ([qs[qs.index(target) + 1]] if target in qs and qs.index(target) + 1 < len(qs) else []):
        if q in qmap:
            _cache_one(cik, qmap[q])
    rows = load_quarter(cik, target)
    total = sum(r["value"] for r in rows) or 1

    idx = qs.index(target) if target in qs else 0
    prev_map = {}
    if idx + 1 < len(qs):
        pq = qs[idx + 1]
        _cache_one(cik, qmap[pq])
        prev_rows = load_quarter(cik, pq)
        prev_map = {r["ticker"]: r["shares"] for r in prev_rows}
    curr_map = {r["ticker"]: r["shares"] for r in rows}
    changes = diff_holdings(prev_map, curr_map)

    out = []
    for r in rows:
        ct, pct = changes.get(r["ticker"], ("HOLD", 0.0))
        out.append(Holding(
            ticker=r["ticker"], companyName=r["company_name"],
            shares=int(r["shares"]), value=r["value"],
            weight=round(r["value"] / total, 4),
            changeType=ct, changePercent=round(pct, 2)))
    out.sort(key=lambda x: x.value, reverse=True)
    return out

@app.get("/api/stocks/{ticker}/holders", response_model=list[dict])
def get_stock_holders(ticker: str):
    tk = ticker.upper()
    out = []
    for cik, _ in POPULAR:
        try:
            filings = sc.list_13f_filings(cik)
            if filings:
                _cache_one(cik, filings[0])
                lq = quarter_label(filings[0])
                for r in load_quarter(cik, lq):
                    if r["ticker"] == tk:
                        out.append({"cik": cik, "fundName": _meta.get(cik, cik),
                                    "shares": int(r["shares"]), "value": r["value"],
                                    "changeType": "HOLD"})
        except Exception:
            continue
    return out

@app.get("/api/health")
def health():
    return {"status": "ok", "time": time.time()}
