"""
SEC 真实数据抓取 + OpenFIGI CUSIP->ticker 反查 + 季度缓存
"""
import time
import requests
import xml.etree.ElementTree as ET
from typing import Optional

HEADERS = {"User-Agent": "13FTracker research@example.com", "Accept-Encoding": "gzip, deflate"}
FIGI_URL = "https://api.openfigi.com/v3/mapping"
SEC_SUB = "https://data.sec.gov/submissions/CIK{:010d}.json"
SEC_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{fname}"


def _get(url: str) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=25)
    time.sleep(0.15)  # SEC 限流友好
    r.raise_for_status()
    return r


def list_13f_filings(cik: str) -> list[dict]:
    """返回该机构全部 13F-HR 申报（新的在前）。"""
    r = _get(SEC_SUB.format(int(cik)))
    d = r.json()
    recent = d["filings"]["recent"]
    out = []
    for i, form in enumerate(recent["form"]):
        if form == "13F-HR":
            acc = recent["accessionNumber"][i]
            out.append({
                "cik": cik,
                "name": d["name"],
                "accession": acc,
                "date": recent["filingDate"][i],
                "report_quarter": recent.get("reportDate", [""] * len(recent["form"]))[i],
                "primary_doc": recent["primaryDocument"][i],
            })
    return out


def _find_info_table_xml(cik: str, accession: str, primary_doc: str) -> Optional[str]:
    """在申报目录里找 informationTable 的 .xml 文件。"""
    acc_nodash = accession.replace("-", "")
    idx_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/"
    try:
        html = _get(idx_url).text
    except Exception:
        return None
    import re
    for m in re.finditer(r'href="([^"]+\.xml)"', html):
        href = m.group(1)
        if href.endswith(primary_doc):
            continue
        fname = href.split("/")[-1]
        # 信息表 xml 通常是纯数字命名
        if fname.rstrip(".xml").isdigit():
            if href.startswith("http"):
                return href
            return f"https://www.sec.gov{href}"
    return None


def parse_info_table(xml_text: str) -> list[dict]:
    """解析 13F 信息表 XML，按 CUSIP 聚合（同 CUSIP 多行合并股数/市值）。"""
    ns = {"f": "http://www.sec.gov/edgar/document/thirteenf/informationtable"}
    root = ET.fromstring(xml_text)
    agg: dict[str, dict] = {}
    for table in root.iter("{http://www.sec.gov/edgar/document/thirteenf/informationtable}infoTable"):
        issuer = table.findtext("f:nameOfIssuer", default="", namespaces=ns)
        cusip = table.findtext("f:cusip", default="", namespaces=ns)
        value = float(table.findtext("f:value", default="0", namespaces=ns))
        ssh = table.find("f:shrsOrPrnAmt", ns)
        shares = float(ssh.findtext("f:sshPrnamt", default="0", namespaces=ns)) if ssh is not None else 0.0
        if not cusip:
            continue
        cur = agg.setdefault(cusip, {"cusip": cusip, "companyName": issuer, "shares": 0.0, "value": 0.0})
        cur["shares"] += shares
        cur["value"] += value
    return list(agg.values())


def figi_cusip_to_ticker(cusips: list[str]) -> dict[str, str]:
    """批量 CUSIP -> ticker，OpenFIGI 每次最多 10 条。失败返回空。"""
    result: dict[str, str] = {}
    for i in range(0, len(cusips), 10):
        chunk = cusips[i:i + 10]
        payload = [{"idType": "ID_CUSIP", "idValue": c} for c in chunk]
        try:
            r = requests.post(FIGI_URL, json=payload, headers={"Content-Type": "application/json", "User-Agent": "13FTracker"}, timeout=25)
            if r.status_code != 200:
                continue
            for item, resp in zip(chunk, r.json()):
                data = resp.get("data")
                if data and data[0].get("ticker"):
                    result[item] = data[0]["ticker"]
        except Exception:
            continue
        time.sleep(0.2)
    return result
