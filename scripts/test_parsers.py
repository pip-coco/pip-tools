#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
取得先ごとの読み取り処理（パーサ）が正しいかを、
実際の応答と同じ形のダミーデータで確かめる。ネットワークにはつながない。

    python3 scripts/test_parsers.py
"""

import datetime as dt
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect_toushi as C          # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    print(("  OK   " if cond else "  NG   ") + name + (("  " + detail) if detail else ""))
    if not cond:
        FAIL.append(name)


class FakeResponse(object):
    def __init__(self, content, encoding="utf-8"):
        if isinstance(content, str):
            self.content = content.encode(encoding)
        else:
            self.content = content
        self._enc = encoding
        self.status_code = 200
        self.apparent_encoding = encoding

    @property
    def text(self):
        return self.content.decode(self._enc, errors="replace")

    @text.setter
    def text(self, v):
        pass

    @property
    def encoding(self):
        return self._enc

    @encoding.setter
    def encoding(self, v):
        self._enc = v

    def json(self):
        return json.loads(self.text)


# ---------------------------------------------------------------------------
# ダミーの応答
# ---------------------------------------------------------------------------

def yahoo_payload(days, start_price):
    base = dt.datetime(2026, 8, 21, 0, 0)
    stamps, closes = [], []
    for i in range(days):
        stamps.append(int((base - dt.timedelta(days=7 * (days - 1 - i))).timestamp()))
        closes.append(start_price * (1 + 0.001 * i))
    return json.dumps({"chart": {"result": [{
        "timestamp": stamps,
        "indicators": {"quote": [{"close": closes}]},
    }], "error": None}})


TREASURY_NOMINAL = (
    'Date,"1 Mo","3 Mo","6 Mo","1 Yr","2 Yr","3 Yr","5 Yr","7 Yr","10 Yr","20 Yr","30 Yr"\n'
    '08/21/2026,4.31,4.35,4.40,4.45,4.50,4.55,4.60,4.65,4.69,4.90,5.05\n'
    '08/20/2026,4.30,4.34,4.39,4.44,4.49,4.54,4.59,4.64,4.68,4.89,5.04\n'
)
TREASURY_REAL = (
    'Date,"5 YR","7 YR","10 YR","20 YR","30 YR"\n'
    '08/21/2026,2.10,2.25,2.35,2.50,2.60\n'
    '08/20/2026,2.09,2.24,2.34,2.49,2.59\n'
)

# 財務省: 和暦・Shift-JIS・列は 0=日付, 1..=年限（10=10年）
MOF_CSV = (
    "基準日,1年,2年,3年,4年,5年,6年,7年,8年,9年,10年,15年,20年,25年,30年,40年\n"
    "S49.9.24,,,,,,,,,,8.244,,,,,\n"
    "R8.8.20,0.85,1.05,1.20,1.35,1.50,1.70,1.90,2.10,2.40,2.665,,3.20,,3.55,3.90\n"
    "R8.8.21,0.86,1.06,1.21,1.36,1.51,1.71,1.91,2.11,2.41,2.670,,3.21,,3.56,3.91\n"
)

NIKKEI_HTML = """
<table><tbody>
<tr><td>2026.08.20</td><td>17.30</td><td>22.21</td></tr>
<tr><td>2026.08.21</td><td>17.11</td><td>22.05</td></tr>
</tbody></table>
"""

BLS_JSON = json.dumps({
    "status": "REQUEST_SUCCEEDED",
    "Results": {"series": [{"seriesID": "CUUR0000SA0", "data": [
        {"year": "2026", "period": "M07", "value": "320.5"},
        {"year": "2025", "period": "M07", "value": "311.6"},
    ]}]},
})

BOTWALL = ('<!doctype html><html><head><meta charset="utf-8">'
           '<meta name="robots" content="noindex,nofollow"></head><body>'
           '<noscript>this site requires javascript to verify your browser.</noscript>')


SHILLER_HEADER = ["Date", "S&P Comp.\nP", "Dividend\nD", "Earnings\nE",
                  "Consumer\nPrice Index", "Date\nFraction", "Long\nInterest Rate GS10",
                  "Real\nPrice", "Real\nEarnings", "Cyclically\nAdjusted\nCAPE",
                  "TR CAPE", "Excess CAPE Yield"]
# 2026.06 : P=7500 E=254 → 実績PER 29.53 / CAPE 41.9
SHILLER_ROWS = [
    [2026.06, 7500.0, 60.0, 254.0, 320.0, 2026.45, 4.69, 7500.0, 254.0, 41.9, 45.0, 1.2],
    [2026.07, 7600.0, 60.0, 256.0, 321.0, 2026.54, 4.70, 7600.0, 256.0, 42.1, 45.2, 1.1],
    [2026.08, 7674.0, 60.0, None, 322.0, 2026.62, 4.69, 7674.0, None, 42.4, 45.5, 1.0],
]


def make_shiller_xlsx():
    """新形式(.xlsx)版のダミー。"""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["Robert Shiller"] + [None] * 6)
    ws.append([None] * 7)
    ws.append(SHILLER_HEADER)
    for r in SHILLER_ROWS:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_shiller_xls():
    """旧形式(.xls / Excel 97-2003)版のダミー。実物はこちらの形式。"""
    import xlwt
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Data")
    ws.write(0, 0, "Robert Shiller")
    for c, v in enumerate(SHILLER_HEADER):
        ws.write(2, c, v)
    for i, row in enumerate(SHILLER_ROWS):
        for c, v in enumerate(row):
            if v is not None:
                ws.write(3 + i, c, v)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


SHILLER_PAGE = ('<html><body><a href="https://img1.wsimg.com/blobby/go/xxx/'
                'downloads/yyy/ie_data.xls?ver=123">US Stock Markets 1871-Present</a></body></html>')
SHILLER_XLSX = make_shiller_xlsx()
SHILLER_XLS = make_shiller_xls()
SHILLER_BLOB = SHILLER_XLS          # 既定は実物と同じ旧形式で試す


def fake_get(url, timeout=25, tries=2, **kw):
    C.get.last_error = ""
    if "finance.yahoo.com" in url:
        if "%5EN225" in url or "N225" in url:
            return FakeResponse(yahoo_payload(60, 60000))
        if "TPX" in url or "998405" in url:
            return FakeResponse(yahoo_payload(60, 3800))
        if "GSPC" in url:
            return FakeResponse(yahoo_payload(60, 7000))
        if "GC%3DF" in url or "GC=F" in url:
            return FakeResponse(yahoo_payload(60, 4200))
        if "SI%3DF" in url or "SI=F" in url:
            return FakeResponse(yahoo_payload(60, 62))
        if "JPY" in url:
            return FakeResponse(yahoo_payload(60, 150))
        return None
    if "daily_treasury_real_yield_curve" in url:
        return FakeResponse(TREASURY_REAL)
    if "daily_treasury_yield_curve" in url:
        return FakeResponse(TREASURY_NOMINAL)
    if "jgbcm_all.csv" in url:
        return FakeResponse(MOF_CSV.encode("cp932"), encoding="cp932")
    if "indexes.nikkei.co.jp" in url:
        return FakeResponse(NIKKEI_HTML)
    if "api.bls.gov" in url:
        return FakeResponse(BLS_JSON)
    if url.rstrip("/").endswith("shillerdata.com"):
        return FakeResponse(SHILLER_PAGE)
    if "ie_data.xls" in url:
        return FakeResponse(SHILLER_BLOB)
    if "multpl.com" in url:
        return FakeResponse(BOTWALL)          # 実際にボット判定された想定
    if "fred.stlouisfed.org" in url:
        C.get.last_error = "タイムアウト"
        return None                            # 実際にタイムアウトした想定
    return None


# ---------------------------------------------------------------------------

def main():
    C.get = fake_get
    C.time.sleep = lambda *_a, **_k: None      # 待ち時間は飛ばす

    print("\n[1] Yahoo Finance の読み取り")
    y = C.fetch_yahoo(["^N225"])
    check("日経の点が取れる", len(y) > 40, "%d点" % len(y))
    check("日付が YYYY-MM-DD", all(len(d) == 10 and d[4] == "-" for d in y))

    print("\n[2] 米財務省 イールドカーブ")
    n = C.treasury_curve("daily_treasury_yield_curve", ["10 Yr", "10 YR"])
    r = C.treasury_curve("daily_treasury_real_yield_curve", ["10 YR", "10 Yr"])
    check("10年名目 = 4.69", n.get("2026-08-21") == 4.69, str(n.get("2026-08-21")))
    check("10年実質 = 2.35", r.get("2026-08-21") == 2.35, str(r.get("2026-08-21")))
    check("BEIが 2.34 になる", round(n["2026-08-21"] - r["2026-08-21"], 2) == 2.34)

    print("\n[3] 財務省 国債金利情報（和暦・Shift-JIS）")
    j = C.fetch_jgb10y()
    check("令和8年8月21日 → 2026-08-21", "2026-08-21" in j, str(sorted(j)[-1:]))
    check("10年金利 = 2.67", j.get("2026-08-21") == 2.670, str(j.get("2026-08-21")))
    check("20年より古い昭和分は捨てる", "1974-09-24" not in j)

    print("\n[4] 日経平均プロフィル")
    p = C.fetch_nikkei_ratio("per")
    check("加重平均のほうを取る (17.11)", p.get("2026-08-21") == 17.11, str(p.get("2026-08-21")))

    print("\n[5] Shiller (Excel)")
    global SHILLER_BLOB
    for fmt, blob in (("旧形式 .xls", SHILLER_XLS), ("新形式 .xlsx", SHILLER_XLSX)):
        SHILLER_BLOB = blob
        s = C.fetch_shiller()
        check("[%s] CAPE が取れる (42.4)" % fmt,
              s.get("cape", {}).get("2026-08-01") == 42.4,
              str(s.get("cape", {}).get("2026-08-01")))
        check("[%s] 実績PER を P÷E で作る (7500/254=29.53)" % fmt,
              s.get("spx_per", {}).get("2026-06-01") == 29.53,
              str(s.get("spx_per", {}).get("2026-06-01")))
        check("[%s] Eが空の月はPERを作らない" % fmt,
              "2026-08-01" not in s.get("spx_per", {}))
        check("[%s] Excess CAPE Yield を CAPE と取り違えない" % fmt,
              s.get("cape", {}).get("2026-06-01") == 41.9,
              str(s.get("cape", {}).get("2026-06-01")))
    SHILLER_BLOB = SHILLER_XLS

    print("\n[5b] Excel でないものを掴まない")
    SHILLER_BLOB = BOTWALL
    check("ボット判定ページなら空を返す", C.fetch_shiller() == {})
    SHILLER_BLOB = SHILLER_XLS

    print("\n[6] BLS 米CPI")
    cpi = C.fetch_bls_cpi()
    yoy = C.to_yoy(cpi)
    check("前年比を計算できる", abs(yoy.get("2026-07-01", 0) - 2.86) < 0.02,
          str(yoy.get("2026-07-01")))

    print("\n[7] ボット判定・タイムアウトの検知")
    check("ボット判定ページを見抜く", C.looks_like_botwall(BOTWALL))
    check("multpl は空を返す", C.fetch_multpl("shiller-pe") == {})
    check("FRED は空を返す", C.fetch_fred("DGS10") == {})

    print("\n[8] 通しで動かす")
    C.DIAG[:] = []
    C.NOTES[:] = []
    raw = C.collect()
    got = {k: len(v) for k, v in raw.items() if v}
    check("価格6本すべて取れている",
          all(got.get(k) for k in ("nikkei", "topix", "spx", "gold", "silver", "usdjpy")),
          str({k: got.get(k) for k in ("nikkei", "topix", "spx", "gold", "silver", "usdjpy")}))
    check("金利3本取れている", all(got.get(k) for k in ("dgs10", "real10", "jp10y")))
    check("バリュエーション4本取れている",
          all(got.get(k) for k in ("nikkei_per", "nikkei_pbr", "spx_per", "cape")))

    table = C.merge({}, raw)
    axis = []
    for k, col in table.items():
        if k not in C.DERIVED and len(col) >= 30:
            axis += list(col.keys())
    dates = C.build_date_axis(axis)
    table = C.add_derived(table, dates)
    check("日付軸が1点に潰れない", len(dates) > 30, "%d点" % len(dates))
    check("BEI が計算されている", len(table["bei10"]) > 0)
    check("NT倍率が計算されている", len(table["nt"]) > 0)
    check("金銀比価が計算されている", len(table["gsr"]) > 0)
    check("日本イールドスプレッドが計算されている", len(table["ys_jp"]) > 0)
    check("米国イールドスプレッドが計算されている", len(table["ys_us"]) > 0)

    print("\n" + "=" * 52)
    if FAIL:
        print("失敗 %d 件: %s" % (len(FAIL), " / ".join(FAIL)))
        return 1
    print("すべて通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
