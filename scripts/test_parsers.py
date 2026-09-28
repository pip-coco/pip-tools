#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
収集スクリプトの自己テスト（第2版）。ネットワークにはつながない。

取得先ごとに「実際の応答と同じ形」のダミーを作り、読み取り・接ぎ木・間引き・
派生指標・保存形式までを通しで確かめる。

    python3 scripts/test_parsers.py
"""

import base64
import datetime as dt
import io
import json
import math
import os
import re
import sys
import tempfile
import zlib
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect_toushi as C          # noqa: E402

FAIL = []
TODAY = C.TODAY


def check(name, cond, detail=""):
    print(("  OK   " if cond else "  NG   ") + name + (("  " + str(detail)) if detail != "" else ""))
    if not cond:
        FAIL.append(name)


class FakeResponse(object):
    def __init__(self, content, encoding="utf-8"):
        self.content = content.encode(encoding) if isinstance(content, str) else content
        self._enc = encoding
        self.status_code = 200
        self.apparent_encoding = encoding

    @property
    def text(self):
        return self.content.decode(self._enc, errors="replace")

    @property
    def encoding(self):
        return self._enc

    @encoding.setter
    def encoding(self, v):
        self._enc = v

    def json(self):
        return json.loads(self.text)


# ---------------------------------------------------------------------------
# 「本当の値」を決めておき、各取得先のダミーはそこから作る。
# こうすると、読み取った値が正しいかを本当の値と突き合わせて確かめられる。
# ---------------------------------------------------------------------------

def years_since(d, y0):
    return (d - dt.date(y0, 1, 1)).days / 365.25


def true_nikkei(d):
    t = years_since(d, 1965)
    return round(1200.0 * math.exp(0.06 * t) * (1 + 0.08 * math.sin(t * 1.3)), 2)


def true_topix(d):
    return round(true_nikkei(d) / 14.0, 2)            # NT倍率 ≒ 14


def true_etf(d):
    # TOPIX連動ETFは TOPIX のおよそ 1/10。配当の積み上がりで年に±1%ほど揺れる
    season = 1.0 + 0.01 * math.sin(2 * math.pi * d.timetuple().tm_yday / 365.0)
    return round(true_topix(d) / 10.0 * season, 2)


def true_spx(d):
    t = years_since(d, 1871)
    return round(4.0 * math.exp(0.045 * t), 2)


def true_ndx(d):
    t = years_since(d, 1985)
    return round(250.0 * math.exp(0.12 * t), 2)


def true_usdjpy(d):
    t = years_since(d, 1996)
    return round(120.0 + 20.0 * math.sin(t / 3.0), 3)


# 取得先ごとの「いつから持っているか」
YAHOO_START = {"^N225": dt.date(1965, 1, 5), "^TPX": dt.date(2021, 4, 1),
               "1306.T": dt.date(2001, 7, 13), "^GSPC": dt.date(1927, 12, 30), "^NDX": dt.date(1985, 10, 1),
               "JPY=X": dt.date(1996, 10, 30)}
YAHOO_TRUE = {"^N225": true_nikkei, "^TPX": true_topix, "1306.T": true_etf,
              "^GSPC": true_spx, "^NDX": true_ndx, "JPY=X": true_usdjpy}
YAHOO_OFFSET = {"^N225": 32400, "^TPX": 32400, "1306.T": 32400,   # 東京 UTC+9
                "^GSPC": -14400, "^NDX": -14400, "JPY=X": 3600}


def month_end(d):
    nxt = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return nxt - dt.timedelta(days=1)


def yahoo_payload(symbol, rng, interval):
    """Yahoo のチャートAPIと同じ形を作る。
    月足・週足のタイムスタンプは「期間の初日の現地0時」で、終値は期間最後の値。"""
    start, fn, off = YAHOO_START[symbol], YAHOO_TRUE[symbol], YAHOO_OFFSET[symbol]
    stamps, closes = [], []

    def stamp(day, hour=0):
        local = dt.datetime(day.year, day.month, day.day, hour)
        return int((local - dt.datetime(1970, 1, 1)).total_seconds()) - off

    if interval == "1mo":
        d = start.replace(day=1)
        while d <= TODAY:
            stamps.append(stamp(d))
            closes.append(fn(min(month_end(d), TODAY)))
            d = month_end(d) + dt.timedelta(days=1)
    elif interval == "1wk":
        d = max(start, TODAY - dt.timedelta(days=5 * 365))
        d = d - dt.timedelta(days=d.weekday())        # 月曜
        while d <= TODAY:
            stamps.append(stamp(d))
            closes.append(fn(min(d + dt.timedelta(days=4), TODAY)))
            d += dt.timedelta(days=7)
    else:
        d = max(start, TODAY - dt.timedelta(days=365))
        while d <= TODAY:
            if d.weekday() < 5:
                stamps.append(stamp(d, 9))
                closes.append(fn(d))
            d += dt.timedelta(days=1)
    return json.dumps({"chart": {"result": [{
        "meta": {"gmtoffset": off},
        "timestamp": stamps,
        "indicators": {"quote": [{"close": closes}]},
    }], "error": None}})


def nikkei_official_csv():
    """日経公式の日次CSV。列の並びは 日付,終値,始値,高値,安値。Shift-JIS。"""
    lines = ['"データ日付","終値","始値","高値","安値"']
    d = dt.date(2023, 1, 4)
    while d <= TODAY:
        if d.weekday() < 5:
            c = true_nikkei(d)
            lines.append('"%s","%.2f","%.2f","%.2f","%.2f"'
                         % (d.strftime("%Y/%m/%d"), c, c * 0.995, c * 1.01, c * 0.99))
        d += dt.timedelta(days=1)
    return "\n".join(lines).encode("cp932")


def lbma_json(start, base, drift):
    out, d = [], start
    while d <= TODAY:
        t = (d - start).days / 365.25
        v = round(base * math.exp(drift * t), 3)
        out.append({"is_cms_locked": 0, "d": d.isoformat(), "v": [v, v * 0.8, None]})
        d += dt.timedelta(days=3)
    out.append({"is_cms_locked": 0, "d": "1968-04-15", "v": [None, None, None]})   # 欠測
    return json.dumps(out)


def treasury_csv(first_year, col_name, level):
    lines = ['Date,"1 Mo","%s","30 Yr"' % col_name]
    d = dt.date(first_year, 1, 2)
    while d <= TODAY:
        if d.weekday() < 5:
            t = years_since(d, first_year)
            lines.append("%s,1.0,%.2f,3.0" % (d.strftime("%m/%d/%Y"), level + math.sin(t)))
        d += dt.timedelta(days=1)
    return "\n".join(lines)


def treasury_year_csv(year, col_name, level):
    body = [l for l in treasury_csv(1990, col_name, level).splitlines()[1:]
            if l.split(",")[0].endswith("/%d" % year)]
    return "\n".join(['Date,"1 Mo","%s","30 Yr"' % col_name] + body)


def mof_csv():
    """財務省の国債金利。和暦・Shift-JIS。0=日付, 10=10年。"""
    lines = ["国債金利情報", "基準日,1年,2年,3年,4年,5年,6年,7年,8年,9年,10年,15年,20年"]
    d = dt.date(1974, 9, 24)
    while d <= TODAY:
        if d.year >= 2019 and (d.year > 2019 or d.month >= 5):
            era, y = "R", d.year - 2018
        elif d.year >= 1989:
            era, y = "H", d.year - 1988
        else:
            era, y = "S", d.year - 1925
        ten = round(2.0 + math.sin(years_since(d, 1974)), 3)
        lines.append("%s%d.%d.%d,,,,,,,,,,%s,," % (era, y, d.month, d.day, ten))
        d += dt.timedelta(days=15)
    return "\n".join(lines).encode("cp932")


NIKKEI_HTML = """
<select name="year"><option value="2004">2004年</option><option value="2026">2026年</option></select>
<a href="/nkave/archives/data?list=per&amp;year=2025&amp;month=8">8月</a>
<table><tbody>
<tr><td>2026.09.24</td><td>17.30</td><td>22.21</td></tr>
<tr><td>2026.09.25</td><td>17.11</td><td>22.05</td></tr>
</tbody></table>
"""


def bls_json(y0, y1):
    data = []
    for y in range(y0, y1 + 1):
        for m in range(1, 13):
            if dt.date(y, m, 1) > TODAY:
                continue
            data.append({"year": str(y), "period": "M%02d" % m,
                         "value": "%.3f" % (100 * 1.025 ** (y - 1990 + m / 12.0))})
        data.append({"year": str(y), "period": "M13", "value": "999.0"})  # 年平均。使ってはいけない
    return json.dumps({"status": "REQUEST_SUCCEEDED",
                       "Results": {"series": [{"seriesID": "CUUR0000SA0", "data": data}]}})


def shiller_long(cape_filled=False, split_header=False):
    """Shiller の ie_data と同じ並びの Excel（1871-01 〜 今月）。"""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["Robert Shiller"] + [None] * 6)
    if split_header:
        ws.append([None, "S&P", None, None, None, None, "Long", None, "Cyclically Adjusted"])
        ws.append(["Date", "Comp. P", "Dividend D", "Earnings E", "Consumer Price Index",
                   "Date Fraction", "Interest Rate GS10", "Real Price",
                   "Price Earnings Ratio P/E10 or CAPE", "Excess CAPE Yield"])
    else:
        ws.append([None] * 7)
        ws.append(["Date", "S&P Comp. P", "Dividend D", "Earnings E", "Consumer Price Index",
                   "Date Fraction", "Long Interest Rate GS10", "Real Price", "CAPE",
                   "Excess CAPE Yield"])
    k, y, m = 0, 1871, 1
    while dt.date(y, m, 1) <= TODAY:
        d = dt.date(y, m, 1)
        p = true_spx(d)
        e = p / (15.0 + 3.0 * math.sin(k / 50.0))
        c = 10.0 * 1.0015 ** k
        g = 4.0 + 1.5 * math.sin(k / 80.0)
        last = (y, m) == (TODAY.year, TODAY.month)
        # 実物と同じく、CAPE は10年ぶんの利益がそろう1881年から埋まっている
        ws.append([y + m / 100.0, p, 1.0, None if last else e, c, 0, g, p,
                   (20.0 + (k % 20)) if (cape_filled and k >= 120) else None, 2.0])
        k += 1
        m += 1
        if m > 12:
            y, m = y + 1, 1
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# 旧形式(.xls)の小さなダミー（3ヶ月ぶん）。xlwt なしで動くよう圧縮して埋め込んである
_SHILLER_XLS_B64 = (
    "eNrtWEtoFHcY/83sbHZ3djfuxqiJQhiEpq2KGL140W0emyDUsN0UUvue7A46Os7K7EZNPahJcywU"
    "evJxEb30ou1FLbZo60WokNIeCoXSpLm1pxYFkZj1+3/zSKIesqCiMt8y33y/7/l/zn92fpnKTp/7"
    "du0MHqGdiGC+nkDTIp1EV8IHGZC9Xheif4/TVQ/ppaJEnCayKYpr6dsxMYdivmcg4xvlR+LA33R9"
    "iEMYrNiG9hyph9ugS6INO4hLOEuaZrRzq1qYl5ivZH6JPb9n/hZrvmC+g3ynpfcxlRvcsN1bxe/J"
    "69nWDJH3Csf8wZourMItsYqPfym5vlF0O6ZuvZiGDiWFC6B5GzBsw9GtabTSBF7A3boG3PF36g0t"
    "1D9fvQTS31uqjz1B/5WsACdQ/5QX+CRSGFeERUGfXtPn8BfShNK8VFGsjBhOTRvaZ1qW4ZAf+RhJ"
    "YKizoPVWDh7arBZUUpqHzbJhl9U+AnndsU17b1XNtwK9Fbs6etBw1IJjlgxtl102jqbdLGq/o5dq"
    "ZsVeA7xdsfequ+ya4RjVmlYkqzYw1LWFshUN3XKD0x7w87dR9rGSZZZ0yxpTu8v7R6s1o6z2dhfy"
    "tDjfLWpCylJ7jpaMapWRtsc0rLIqThR+AmWWPIHSvDNTxMtYwXKW92eGzpi5r//7dfdIIfcJa07w"
    "qeOeTa+J4UQdJ0UEBTfDzwfKBWzgiI3MxznrOpbXMm+lXUX3zsIqT+ifYJ/P2dpJdbYx/ZZ7fZH8"
    "BsmT/75ztWNyNvcmyRcHZj5rvfh77hzW01lZpnjxm8AmaZN0+pSg73L+XfKeY38yb3/smRaXM17f"
    "6t4BvAIPoLKYZS48ZO7dUg+ZPcSYuMgfPxfJhCIBihBSAqQQigYoSqgpQE2EYgGKEYoHKE4oEaAE"
    "ITVAKqFkgJKEUl7bI4+0/QcMsi6Lax+liG8dFfL/rLkfEVyOCj78sbAOxxD4uNZj3YJfZv9Z6n6S"
    "qyhPqKKwz3Wu8tPhhSpiCWXFgSiycZUPuIrr41p/5irn+7mdQZXoY1XaWJfFTa7yzxG/iky2DrKJ"
    "sVa50plF/RF+MS9ajGNvj9CuG4A3F0nslFtwWUwnnXQLpCKkkEIKKaSQQgppOSTx+wj4rUu8E0W9"
    "N+aY913nAV3z4WeSV5aKqNCvRn9M87Dp7mCsofWzGlHJzyUtM8b/XihomKo7OIARbseBhtcv/YOS"
    "Fvdn2YGZp7eFGq0/30g7n3H9hyr/J7A="
)
SHILLER_XLS_SMALL = zlib.decompress(base64.b64decode(_SHILLER_XLS_B64))

SHILLER_PAGE = ('<html><body><a href="/downloads/yyy/ie_data.xls?ver=123">'
                'US Stock Markets 1871-Present</a></body></html>')
BOTWALL = ('<!doctype html><html><head><meta charset="utf-8">'
           '<meta name="robots" content="noindex,nofollow"></head><body>'
           '<noscript>this site requires javascript to verify your browser.</noscript>')

# 取得先ごとのふるまいを切り替えるスイッチ
STATE = {
    "shiller_blob": None, "treasury_all_ok": True, "yahoo_tpx_ok": True,
    "lbma_ok": True, "nikkei_official_bad": False,
}


def fake_get(url, timeout=25, tries=2, **kw):
    C.get.last_error = ""
    if not url.startswith("http"):                   # 本物の requests と同じ
        C.get.last_error = "MissingSchema"
        return None
    u = urlparse(url)
    q = parse_qs(u.query)

    if "finance.yahoo.com" in url:
        sym = unquote(u.path.rsplit("/", 1)[-1])
        if sym == "^TPX" and not STATE["yahoo_tpx_ok"]:
            return FakeResponse(json.dumps({"chart": {"result": None,
                                                      "error": {"description": "No data found"}}}))
        if sym not in YAHOO_START:
            return FakeResponse(json.dumps({"chart": {"result": None,
                                                      "error": {"description": "Not found"}}}))
        return FakeResponse(yahoo_payload(sym, q["range"][0], q["interval"][0]))

    if "nikkei_stock_average_daily_jp.csv" in url:
        if STATE["nikkei_official_bad"]:             # 列を取り違えた想定（全部2倍）
            body = nikkei_official_csv().decode("cp932")
            body = re.sub(r'"(\d+\.\d\d)"', lambda m: '"%.2f"' % (float(m.group(1)) * 2), body)
            return FakeResponse(body.encode("cp932"), encoding="cp932")
        return FakeResponse(nikkei_official_csv(), encoding="cp932")

    if "prices.lbma.org.uk" in url:
        if not STATE["lbma_ok"]:
            C.get.last_error = "HTTP 503"
            return None
        if "gold" in url:
            return FakeResponse(lbma_json(dt.date(1968, 4, 1), 37.7, 0.07))
        return FakeResponse(lbma_json(dt.date(1968, 1, 2), 2.17, 0.06))

    if "home.treasury.gov" in url:
        real = "real_yield" in url
        col, lvl, first = ("10 YR", 1.5, 2003) if real else ("10 Yr", 4.0, 1990)
        year = u.path.split("/")[-2]
        if year == "all":
            if not STATE["treasury_all_ok"]:
                return FakeResponse(BOTWALL)
            return FakeResponse(treasury_csv(first, col, lvl))
        if int(year) < first:
            return FakeResponse("")
        return FakeResponse(treasury_year_csv(int(year), col, lvl))

    if "jgbcm_all.csv" in url:
        return FakeResponse(mof_csv(), encoding="cp932")
    if "indexes.nikkei.co.jp/nkave/archives" in url:
        return FakeResponse(NIKKEI_HTML)
    if "api.bls.gov" in url:
        return FakeResponse(bls_json(int(q["startyear"][0]), int(q["endyear"][0])))
    if u.netloc.endswith("shillerdata.com") and u.path in ("", "/"):
        return FakeResponse(SHILLER_PAGE)
    if "ie_data.xls" in url:
        return FakeResponse(STATE["shiller_blob"] or shiller_long())
    if "jpx.co.jp" in url:
        if url.endswith(".csv"):
            return FakeResponse("指数名,月末値\nTOPIX,4000.00\n".encode("cp932"), encoding="cp932")
        C.get.last_error = "HTTP 403"
        return None
    if "multpl.com" in url:
        return FakeResponse(BOTWALL)
    if "fred.stlouisfed.org" in url:
        C.get.last_error = "タイムアウト"
        return None
    return None


# ---------------------------------------------------------------------------

def main():
    C.get = fake_get
    # 本物の基準点（2026年8月の実測値）はダミーの世界の値と合わないので、ダミーに合わせたものに差し替える
    C.TOPIX_ANCHORS = {d: true_topix(dt.date(2026, 8, int(d[-2:]))) for d in C.TOPIX_ANCHORS}
    C.time.sleep = lambda *_a, **_k: None

    print("\n[1] Yahoo Finance（月足・週足・日足の日付の付け方）")
    m = C.yahoo_chart("^N225", "max", "1mo")
    check("月足は1965年から取れる", min(m) <= "1965-01-31", min(m))
    d0 = "1990-06-30"
    check("月足は月末の日付になる（東京の月初0時がUTCで前月にずれない）", d0 in m,
          sorted(k for k in m if k.startswith("1990-0"))[:3])
    check("月足の値は月末の終値", abs(m[d0] - true_nikkei(dt.date(1990, 6, 30))) < 0.01)
    w = C.yahoo_chart("^N225", "5y", "1wk")
    fri = [k for k in w if dt.date.fromisoformat(k).weekday() == 4]
    check("週足は金曜の日付になる", len(fri) >= len(w) - 1, "%d/%d" % (len(fri), len(w)))
    y = C.fetch_yahoo(["^N225"])
    check("余計な桁を丸める（64611.1484375 → 64611.15）", C.tidy(64611.1484375) == 64611.15)
    check("ドル円の小数3桁は削らない（158.985 のまま）", C.tidy(158.985) == 158.985
          and C.tidy(158.98500001) == 158.985)
    check("小さい値も有効数字で残す", C.tidy(0.0901234567) == 0.09012346)
    check("月足・週足・日足を重ねる", len(y) > len(m), "%d点" % len(y))

    print("\n[2] 日経公式CSV（列の並びが 日付,終値,始値,… で Shift-JIS）")
    off = C.fetch_nikkei_official()
    dd = max(off)
    check("終値の列を見出しで選ぶ", abs(off[dd] - true_nikkei(dt.date.fromisoformat(dd))) < 0.01,
          "%s = %s" % (dd, off[dd]))
    ok, n = C.agrees(off, y)
    check("Yahoo と値が一致する", ok, "%d点で比較" % n)
    STATE["nikkei_official_bad"] = True
    bad = C.fetch_nikkei_official()
    ok2, _ = C.agrees(bad, y)
    check("列を取り違えたら食い違いを検知する", not ok2)
    STATE["nikkei_official_bad"] = False

    print("\n[3] LBMA（金・銀）")
    g = C.fetch_lbma("gold_pm")
    check("金は1968年から", min(g) == "1968-04-01", min(g))
    check("ドル建て(v[0])を使う", g["1968-04-01"] == 37.7, g["1968-04-01"])
    check("欠測(null)は飛ばす", "1968-04-15" not in g or g.get("1968-04-15") is not None)
    s = C.fetch_lbma("silver")
    check("銀は1968年から", min(s) == "1968-01-02", min(s))

    print("\n[4] 米財務省（まとめ取り → だめなら年ごと）")
    t_all = C.treasury_curve("daily_treasury_yield_curve", ["10 Yr"], 1990)
    check("全年まとめて1990年から", min(t_all) <= "1990-01-05", min(t_all))
    STATE["treasury_all_ok"] = False
    t_yr = C.treasury_curve("daily_treasury_yield_curve", ["10 Yr"], 1990)
    check("まとめ取りがだめでも年ごとに1990年まで取る", min(t_yr) <= "1990-01-05", min(t_yr))
    check("どちらでも同じ値", t_all.get("2005-06-01") == t_yr.get("2005-06-01"))
    r_yr = C.treasury_curve("daily_treasury_real_yield_curve", ["10 YR"], 2003)
    check("実質金利は2003年から（それより前は打ち切り）", min(r_yr) >= "2003-01-01", min(r_yr))
    STATE["treasury_all_ok"] = True

    print("\n[5] 財務省（日本10年・和暦）")
    j = C.fetch_jgb10y()
    check("昭和49年(1974年)から取れる（20年で切らない）", min(j) == "1974-09-24", min(j))
    check("平成・令和の変換", any(k.startswith("1995-") for k in j) and
          any(k.startswith("2025-") for k in j))

    print("\n[6] 日経平均プロフィル（PER）")
    p = C.fetch_nikkei_ratio("per")
    check("加重平均のほうを取る", p.get("2026-09-25") == 17.11, p.get("2026-09-25"))

    print("\n[7] Shiller（1871年からの月次）")
    for label, blob, want_cape_col in (
            ("CAPE列が空（名前だけ一致）", shiller_long(False), False),
            ("CAPE列が埋まっている", shiller_long(True), True),
            ("見出しが2行に割れている", shiller_long(True, split_header=True), True)):
        STATE["shiller_blob"] = blob
        sh = C.fetch_shiller()
        check("[%s] 株価は1871年から" % label, min(sh["price"]) == "1871-01-01", min(sh["price"]))
        check("[%s] CAPE は1881年ごろから（10年ぶんの利益が要る）" % label,
              "1880-01-01" <= min(sh["cape"]) <= "1881-12-01", min(sh["cape"]))
        check("[%s] 長期金利が取れる" % label, len(sh["gs10"]) > 1800, len(sh["gs10"]))
        check("[%s] CPI が取れる" % label, len(sh["cpi"]) > 1800, len(sh["cpi"]))
        if not want_cape_col:
            # 定義どおりの計算と突き合わせる
            last = max(sh["cape"])
            dates_e = sorted(sh["earn"])
            i = dates_e.index(last) if last in dates_e else len(dates_e) - 1
            win = [sh["earn"][d] / sh["cpi"][d] for d in dates_e[max(0, i - 119):i + 1]]
            want = round((sh["price"][last] / sh["cpi"][last]) / (sum(win) / len(win)), 2)
            check("[%s] 計算した CAPE が定義と一致" % label,
                  abs(sh["cape"][last] - want) < 0.02, "%s / %s" % (sh["cape"][last], want))
    STATE["shiller_blob"] = SHILLER_XLS_SMALL
    sh_small = C.fetch_shiller()
    check("3ヶ月しかなければ CAPE を作らない", sh_small.get("cape") == {}, sh_small.get("cape"))
    STATE["shiller_blob"] = BOTWALL.encode()
    check("Excelでないものは掴まない", C.fetch_shiller() == {})
    STATE["shiller_blob"] = None
    check("CAPE らしさを中身で判定（小さい値は弾く）",
          not C._looks_like_cape({"%04d-01-01" % (1900 + i): 1.5 for i in range(240)}))

    print("\n[8] BLS")
    b = C.fetch_bls_cpi()
    check("M13（年平均）を混ぜない", not any(k[5:7] == "13" for k in b))
    check("20年ぶん", min(b) <= "%d-01-01" % (TODAY.year - 19), min(b))

    print("\n[9] 接ぎ木（古い区間を別の取得先から補う）")
    main_ = {"2000-01-01": 10.0, "2001-01-01": 11.0}
    old = {"1990-01-01": 5.0, "2000-01-01": 99.0, "2002-01-01": 99.0}
    gr = C.graft_older(main_, old)
    check("main より前だけ足す", gr.get("1990-01-01") == 5.0)
    check("重なりは main を優先", gr["2000-01-01"] == 10.0)
    check("main より後は足さない", "2002-01-01" not in gr)

    print("\n[9b] 前回値との合わせ方")
    prev = {"x": {"2019-01-01": 1.0, "2020-06-15": 2.0, "2026-08-17": 3.0}}
    fresh = {"x": {"2020-01-01": 10.0, "2026-09-01": 20.0}}
    mg = C.merge(prev, fresh)["x"]
    check("今回の期間より外側の前回値は残す", mg.get("2019-01-01") == 1.0)
    check("今回の期間の内側にある前回値は捨てる（紛れ込み防止）",
          "2020-06-15" not in mg and "2026-08-17" not in mg, sorted(mg))
    check("今回の値はそのまま", mg.get("2026-09-01") == 20.0)
    mg2 = C.merge(prev, {"x": {}})["x"]
    check("今回まったく取れなければ前回値をそのまま保つ", mg2 == prev["x"])
    acc = C.merge({"per": {"2026-08-03": 17.0, "2026-08-20": 17.5}},
                  {"per": {"2026-09-24": 17.3, "2026-09-25": 17.1}})["per"]
    check("当月ぶんしか取れない系列は、過去の積み上げが残る", len(acc) == 4, sorted(acc))

    print("\n[10] TOPIX の穴埋め（ETF×倍率）")
    real = C.fetch_yahoo(["^TPX"])
    etf = C.fetch_yahoo(["1306.T"])
    check("本物は2021年からしかない（再現）", min(real) >= "2021-04-01", min(real))
    tp, filled = C.splice_topix(real, etf, {})
    check("ETFで2001年まで遡れる", min(tp) <= "2001-07-31", min(tp))
    check("本物がある日は本物のまま", all(tp[d] == real[d] for d in real))
    errs = sorted(abs(tp[d] / true_topix(dt.date.fromisoformat(d)) - 1)
                  for d in tp if d < min(real))
    check("補った区間の誤差は中央値1%未満", errs[len(errs) // 2] < 0.01,
          "中央値 %.2f%% / 最大 %.2f%%" % (errs[len(errs) // 2] * 100, errs[-1] * 100))
    STATE["yahoo_tpx_ok"] = False
    real_none = C.fetch_yahoo(["^TPX", "998405.T", "^TOPX"])
    saved_anchors = C.TOPIX_ANCHORS
    C.TOPIX_ANCHORS = {}
    tp2, _ = C.splice_topix(real_none, etf, {})
    check("本物も基準点も前回分もなければ補わない（倍率を決められない）", tp2 == {})
    tp3, _ = C.splice_topix(real_none, etf, real)
    check("前回保存した本物で較正して補える", len(tp3) > 100, "%d点" % len(tp3))

    # 2026-09-28 に実際に起きたこと：Yahoo の ^TPX が404、前回の保存もほぼ空。
    # 固定の基準点（実測5日分）だけで較正して、2001年まで補えること
    days5 = sorted(d for d in etf if d <= TODAY.isoformat())[-30:-25]
    C.TOPIX_ANCHORS = {d: true_topix(dt.date.fromisoformat(d)) for d in days5}
    tp4, _ = C.splice_topix(real_none, etf, {})
    check("基準点5日分だけで2001年まで補える", tp4 and min(tp4) <= "2001-07-31",
          min(tp4) if tp4 else "なし")
    errs4 = sorted(abs(tp4[d] / true_topix(dt.date.fromisoformat(d)) - 1) for d in tp4)
    check("基準点だけで較正しても誤差は中央値2%未満", errs4[len(errs4) // 2] < 0.02,
          "中央値 %.2f%% / 最大 %.2f%%" % (errs4[len(errs4) // 2] * 100, errs4[-1] * 100))
    check("基準点そのものは本物の値のまま残す", all(tp4[d] == C.TOPIX_ANCHORS[d] for d in days5))
    C.TOPIX_ANCHORS = saved_anchors
    STATE["yahoo_tpx_ok"] = True

    print("\n[11] 間引き")
    col = {}
    d = TODAY - dt.timedelta(days=365 * 12)
    while d <= TODAY:
        col[d.isoformat()] = float(d.toordinal())
        d += dt.timedelta(days=1)
    th = C.thin(col)
    recent = [k for k in th if k >= (TODAY - dt.timedelta(days=C.DAILY_DAYS)).isoformat()]
    check("直近400日は毎日残す", len(recent) == C.DAILY_DAYS + 1, len(recent))
    old_part = [k for k in th if k < (TODAY - dt.timedelta(days=C.WEEKLY_DAYS)).isoformat()]
    months = set(k[:7] for k in old_part)
    check("5年より前は月1点", len(old_part) == len(months), "%d点/%dヶ月" % (len(old_part), len(months)))
    check("月の最後の点を残す", all(dt.date.fromisoformat(k) == month_end(dt.date.fromisoformat(k))
                                   or k == min(old_part) or k[:7] == max(old_part)[:7]
                                   for k in old_part))
    check("何度間引いても同じ（冪等）", C.thin(th) == th)
    check("12年の日次が軽くなる", len(th) < 1100, "%d点 ← %d点" % (len(th), len(col)))

    print("\n[12] 古すぎる値を使わない")
    at = C.lookup({"2020-01-01": 1.0}, 7)
    check("7日以内なら使う", at("2020-01-05") == 1.0)
    check("7日を超えたら使わない", at("2020-02-01") is None)
    check("それより前は使わない", at("2019-12-31") is None)

    print("\n[13] 派生指標")
    tbl = {"topix": {"2026-09-25": 4000.0, "2026-09-26": 4100.0, "2010-01-04": 900.0},
           "nikkei": {"2026-09-25": 56000.0},
           "usdjpy": {"2026-09-25": 160.0, "2026-09-26": 150.0},
           "spx_per": {"2026-09-01": 25.0}, "dgs10": {"2026-08-28": 4.0},
           "real10": {"2026-09-25": 2.0}}
    der = C.add_derived(tbl)
    check("ドル建てTOPIX = その日のTOPIX ÷ その日のドル円",
          der["topix_usd"].get("2026-09-26") == round(4100 / 150.0, 3), der["topix_usd"])
    check("ドル円が古すぎる日は作らない（2010年に2026年のドル円を使わない）",
          "2010-01-04" not in der["topix_usd"])
    check("NT倍率", der["nt"].get("2026-09-25") == 14.0, der["nt"])
    check("米国イールドスプレッドは月次PERと数日前の金利で作る",
          der["ys_us"].get("2026-09-01") == round(100 / 25.0 - 4.0, 2), der["ys_us"])
    check("BEI = 名目 − 実質（名目がなければ作らない）", der["bei10"] == {}, der["bei10"])
    tbl2 = {"spx": {"2026-09-25": 7600.0, "1990-01-31": 330.0},
            "ndx": {"2026-09-25": 25000.0},
            "usdjpy": {"2026-09-25": 150.0}}
    der2 = C.add_derived(tbl2)
    check("円建てS&P500 = その日のS&P500 × その日のドル円",
          der2["spx_jpy"].get("2026-09-25") == 7600.0 * 150.0, der2["spx_jpy"])
    check("円建てナスダック100 = その日の指数 × その日のドル円",
          der2["ndx_jpy"].get("2026-09-25") == 25000.0 * 150.0, der2["ndx_jpy"])
    check("ドル円がない時代(1990年)の円建ては作らない", "1990-01-31" not in der2["spx_jpy"])

    print("\n[14] 通しで動かす（初回：旧形式のファイルから移行）")
    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, "data"))
    C.OUT_PATH = os.path.join(tmp, "data", "toushi.json")
    C.STATUS_PATH = os.path.join(tmp, "data", "toushi-status.json")
    v1 = {"updated": "x", "dates": ["2026-08-17", "2026-08-18", "2026-08-19"],
          "series": {"nikkei_per": [17.66, 17.66, 17.5], "cape": [41.96, 41.96, 41.96]}}
    json.dump(v1, open(C.OUT_PATH, "w", encoding="utf-8"))
    C.DIAG[:] = []
    C.NOTES[:] = []
    rc = C.main()
    out = json.load(open(C.OUT_PATH, encoding="utf-8"))
    st = json.load(open(C.STATUS_PATH, encoding="utf-8"))
    check("正常終了", rc == 0)
    check("新形式(第2版)で書き出す", out.get("version") == 2)
    check("旧形式から移行したと記録", any("旧形式" in l for l in st["diag"]))
    per = out["series"]["nikkei_per"]
    check("旧形式の日経PERを持ち越す（重複は畳む）", 20260817 in per["t"] and 20260818 not in per["t"],
          per["t"][:4])
    first = {k: (out["series"][k]["t"][0] if out["series"][k]["t"] else None) for k in C.META}
    check("S&P500 は1871年から（Shillerで接ぎ木）", first["spx"] == 18710101, first["spx"])
    check("CAPE は1881年ごろから", 18800101 <= (first["cape"] or 0) <= 18811201, first["cape"])
    check("米10年金利は1871年から（Shillerで接ぎ木）", first["dgs10"] == 18710101, first["dgs10"])
    check("米CPI前年比は1872年から", first["us_cpi_yoy"] == 18720101, first["us_cpi_yoy"])
    check("金・銀は1968年から", first["gold"] <= 19680430 and first["silver"] <= 19680131,
          (first["gold"], first["silver"]))
    check("日本10年金利は1974年から", first["jp10y"] <= 19740930, first["jp10y"])
    check("日経平均は1965年から", first["nikkei"] <= 19650131, first["nikkei"])
    check("TOPIX は2001年から（ETFで補った）", first["topix"] <= 20010731, first["topix"])
    check("TOPIX の出どころに換算区間を書く", "1306" in out["meta"]["topix"]["source"],
          out["meta"]["topix"]["source"])
    check("ドル建てTOPIX", len(out["series"]["topix_usd"]["t"]) > 100)
    check("ナスダック100は1985年から", first["ndx"] <= 19851031, first["ndx"])
    check("円建てS&P500・ナスダック100はドル円がある1996年から",
          first["spx_jpy"] == first["usdjpy"] and first["ndx_jpy"] == first["usdjpy"],
          (first["spx_jpy"], first["ndx_jpy"], first["usdjpy"]))
    # 最新日で、円建て ＝ 指数 × ドル円 になっているかを本当の値と突き合わせる
    sj = out["series"]["spx_jpy"]
    last_t = sj["t"][-1]
    ld = dt.date(last_t // 10000, last_t // 100 % 100, last_t % 100)
    want = round(true_spx(ld) * true_usdjpy(ld))
    check("円建てS&P500の最新値が 指数×ドル円 と一致", abs(sj["v"][-1] - want) < 1.0,
          "%s / 期待 %s" % (sj["v"][-1], want))
    check("米国イールドスプレッドは1871年から（実績PERとShillerの長期金利）",
          first["ys_us"] == 18710101, first["ys_us"])
    cape_t = out["series"]["cape"]["t"]
    check("月次の CAPE に旧ファイルの日付（8月17日）が紛れ込まない",
          20260817 not in cape_t, [t for t in cape_t if t >= 20260801])
    futures = [t for s in out["series"].values() for t in s["t"] if t > int(TODAY.strftime("%Y%m%d"))]
    check("未来の日付がない", futures == [], futures[:3])
    for k, s in out["series"].items():
        if s["t"] != sorted(s["t"]) or len(s["t"]) != len(set(s["t"])):
            check("%s の日付が昇順で重複なし" % k, False)
    kb = os.path.getsize(C.OUT_PATH) / 1024.0
    check("ファイルが重すぎない（400KB未満）", kb < 400, "%.0f KB / 合計 %d点" % (kb, st["total_points"]))
    check("偵察の結果が記録されている", any("[偵察] 日経PERアーカイブ" in l for l in st["diag"]) and
          any("[偵察] JPX" in l for l in st["diag"]))

    print("\n[15] 2回目（新形式から読み戻しても崩れない）")
    before = json.load(open(C.OUT_PATH, encoding="utf-8"))
    C.DIAG[:] = []
    C.NOTES[:] = []
    C.main()
    after = json.load(open(C.OUT_PATH, encoding="utf-8"))
    same = all(before["series"][k] == after["series"][k] for k in C.META)
    check("同じ入力なら同じ出力（読み書きで点が増えたり減ったりしない）", same)

    print("\n[16] 取得先が落ちた日")
    STATE["lbma_ok"] = False
    STATE["yahoo_tpx_ok"] = False
    C.DIAG[:] = []
    C.NOTES[:] = []
    C.main()
    down = json.load(open(C.OUT_PATH, encoding="utf-8"))
    check("金は Yahoo に切り替えるか前回値を保つ", len(down["series"]["gold"]["t"]) >=
          len(after["series"]["gold"]["t"]) * 0.9)
    check("TOPIX は前回の本物で較正して補い続ける",
          down["series"]["topix"]["t"][0] == after["series"]["topix"]["t"][0])
    STATE["lbma_ok"] = True
    STATE["yahoo_tpx_ok"] = True

    print("\n" + "=" * 56)
    total = sum(1 for _ in FAIL)
    if FAIL:
        print("失敗 %d 件:" % total)
        for f in FAIL:
            print("  - " + f)
        return 1
    print("すべて通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
