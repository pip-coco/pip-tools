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
        {"year": "2025", "period": "M13", "value": "315.0"},   # M13 は年平均。使ってはいけない
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


def make_shiller_xlsx(split_header=False):
    """新形式(.xlsx)版のダミー。
    split_header=True にすると、実物と同じく見出しを2行に割って作る
    （CAPE の列名が上下に分かれていて取り逃した件の再現）。"""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["Robert Shiller"] + [None] * 6)
    if split_header:
        # 上の行に前半、下の行に後半を置く。"Date" は下の行にある。
        ws.append([None, "S&P", None, None, None, None, None, None, None,
                   "Cyclically Adjusted", "TR", "Excess"])
        ws.append(["Date", "Comp. P", "Dividend D", "Earnings E", "Consumer Price Index",
                   "Date Fraction", "Long Interest Rate GS10", "Real Price", "Real Earnings",
                   "Price Earnings Ratio P/E10 or CAPE", "CAPE", "CAPE Yield"])
    else:
        ws.append([None] * 7)
        ws.append(SHILLER_HEADER)
    for r in SHILLER_ROWS:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# 旧形式(.xls / Excel 97-2003)版のダミー。実物の Shiller のファイルはこの形式。
# 作るのに xlwt が要るので、作った結果を zlib圧縮＋base64 で埋め込んである。
# こうしておけば、このテストは追加のライブラリなしで動く。
# 中身は SHILLER_HEADER / SHILLER_ROWS と同じ。
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


def make_shiller_xls():
    import base64
    import zlib
    return zlib.decompress(base64.b64decode(_SHILLER_XLS_B64))


def shiller_long_series():
    """30年ぶんの月次ダミー（1996-09〜2026-08）。株価・利益・物価が緩やかに伸びる。"""
    price, earn, cpi = {}, {}, {}
    for k in range(360):
        y, m = 1996 + (8 + k) // 12, (8 + k) % 12 + 1
        d = "%04d-%02d-01" % (y, m)
        price[d] = 600.0 * (1.008 ** k)        # 株価は月0.8%成長
        earn[d] = 40.0 * (1.006 ** k)          # 利益は月0.6%成長
        cpi[d] = 100.0 * (1.002 ** k)          # 物価は月0.2%上昇
    return price, earn, cpi


def make_shiller_long(cape_filled):
    """30年ぶんの Excel。cape_filled=False なら CAPE 列を空にして、
    「見出しにつられて隣の空列を掴んでしまった」状況を再現する。"""
    import openpyxl
    price, earn, cpi = shiller_long_series()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["Robert Shiller"] + [None] * 6)
    ws.append([None] * 7)
    ws.append(["Date", "S&P Comp. P", "Dividend D", "Earnings E", "Consumer Price Index",
               "Date Fraction", "Long Interest Rate GS10", "CAPE"])
    for k, d in enumerate(sorted(price)):
        y, m = int(d[:4]), int(d[5:7])
        ws.append([y + m / 100.0, price[d], 10.0, earn[d], cpi[d], 0, 4.0,
                   (25.0 + (k % 20)) if cape_filled else None])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# 実物のページは、配布ファイルへのリンクを "/downloads/..." のように
# 途中から書いていることがある。そのまま requests に渡すと MissingSchema で落ちる。
SHILLER_PAGE = ('<html><body><a href="/downloads/yyy/ie_data.xls?ver=123">'
                'US Stock Markets 1871-Present</a></body></html>')
SHILLER_XLSX = make_shiller_xlsx()
SHILLER_XLSX_SPLIT = make_shiller_xlsx(split_header=True)
SHILLER_XLS = make_shiller_xls()
SHILLER_BLOB = SHILLER_XLS          # 既定は実物と同じ旧形式で試す


def fake_get(url, timeout=25, tries=2, **kw):
    C.get.last_error = ""
    # 本物の requests と同じく、スキームのないURLは受け付けない。
    # これで「相対リンクをそのまま渡していないか」を検査できる。
    if not url.startswith("http"):
        C.get.last_error = "MissingSchema"
        return None
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
    for fmt, blob in (("旧形式 .xls", SHILLER_XLS),
                      ("新形式 .xlsx", SHILLER_XLSX),
                      ("見出しが2行に割れている", SHILLER_XLSX_SPLIT)):
        SHILLER_BLOB = blob
        s = C.fetch_shiller()
        check("[%s] 実績PER を P÷E で作る (7500/254=29.53)" % fmt,
              s.get("spx_per", {}).get("2026-06-01") == 29.53,
              str(s.get("spx_per", {}).get("2026-06-01")))
        check("[%s] Eが空の月はPERを作らない" % fmt,
              "2026-08-01" not in s.get("spx_per", {}))
        # 3行しかないので CAPE は作れないのが正しい。
        # 少ない点数から無理に判定用の数字を作らないことを確かめる。
        check("[%s] 履歴が足りなければ CAPE を作らない" % fmt,
              s.get("cape", {}) == {}, str(s.get("cape", {})))
    SHILLER_BLOB = SHILLER_XLS

    print("\n[5d] CAPE らしさを名前ではなく中身で見分ける")
    real_cape = {"%04d-%02d-01" % (2006 + i // 12, i % 12 + 1): 20.0 + (i % 25)
                 for i in range(240)}
    excess_yield = {"%04d-%02d-01" % (2006 + i // 12, i % 12 + 1): 0.5 + (i % 5) * 0.4
                    for i in range(240)}
    check("本物の CAPE らしい並びは受け入れる", C._looks_like_cape(real_cape))
    check("Excess CAPE Yield のような小さい値は弾く", not C._looks_like_cape(excess_yield))
    check("点数が少なすぎるものは弾く",
          not C._looks_like_cape({"2026-01-01": 41.9, "2026-02-01": 42.0}))

    print("\n[5c] CAPE を定義どおり計算できる（見出しが当てにならない場合）")
    price, earn, cpi_s = shiller_long_series()
    SHILLER_BLOB = make_shiller_long(cape_filled=False)
    s = C.fetch_shiller()
    cape = s.get("cape", {})
    check("空の CAPE 列を使わない（水増ししない）", len(cape) > 100, "%d点" % len(cape))
    last = max(cape)
    dates_e = sorted(earn)
    i = dates_e.index(last)
    window = [earn[d] / cpi_s[d] for d in dates_e[max(0, i - 119):i + 1]]
    expect = round((price[last] / cpi_s[last]) / (sum(window) / len(window)), 2)
    check("計算値が定義と一致する", abs(cape[last] - expect) < 0.01,
          "計算 %s / 期待 %s" % (cape[last], expect))
    check("実績PER も同時に取れる", len(s.get("spx_per", {})) > 100,
          "%d点" % len(s.get("spx_per", {})))
    SHILLER_BLOB = make_shiller_long(cape_filled=True)
    got = C.fetch_shiller().get("cape", {}).get("2026-08-01")
    check("埋まっている CAPE 列はそのまま使う", got == 25.0 + (359 % 20), str(got))
    SHILLER_BLOB = SHILLER_XLS

    print("\n[5e] 相対リンクでも Shiller を落とせる（MissingSchema の件）")
    check("ページ内の相対リンクを絶対URLに直す", C._shiller_download() is not None)

    print("\n[5b] Excel でないものを掴まない")
    SHILLER_BLOB = BOTWALL
    check("ボット判定ページなら空を返す", C.fetch_shiller() == {})
    SHILLER_BLOB = SHILLER_XLS

    print("\n[6] BLS 米CPI")
    cpi = C.fetch_bls_cpi()
    yoy = C.to_yoy(cpi)
    check("前年比を計算できる", abs(yoy.get("2026-07-01", 0) - 2.86) < 0.02,
          str(yoy.get("2026-07-01")))
    check("M13（年平均）を月次に混ぜない",
          not any(k.split("-")[1] == "13" for k in cpi), str(sorted(cpi)))

    print("\n[6b] おかしな日付を弾く")
    today = C.TODAY.isoformat()
    future = (C.TODAY + dt.timedelta(days=100)).isoformat()
    old = (C.TODAY - dt.timedelta(days=365 * 30)).isoformat()
    check("今日は通す", C.valid_date(today))
    check("未来の日付を弾く（2026-12-01 の件）", not C.valid_date(future), future)
    check("20年より古い日付を弾く", not C.valid_date(old), old)
    check("存在しない日付を弾く", not C.valid_date("2026-13-01"))
    check("形式が違うものを弾く", not C.valid_date("R8.8.21"))
    merged = C.merge({}, {"nikkei": {today: 1.0, future: 2.0, "2026-13-01": 3.0}})
    check("merge がまとめて落とす", list(merged["nikkei"]) == [today], str(merged["nikkei"]))

    print("\n[7] ボット判定・タイムアウトの検知")
    check("ボット判定ページを見抜く", C.looks_like_botwall(BOTWALL))
    check("multpl は空を返す", C.fetch_multpl("shiller-pe") == {})
    check("FRED は空を返す", C.fetch_fred("DGS10") == {})

    print("\n[7b] 前回ファイルの読み戻しで点数を水増ししない")
    import tempfile
    prev = {
        "dates": ["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-05"],
        "series": {
            # 1点しか実測がなく、あとは前方補完で同じ値が並んでいる状態
            "cape": [None, 41.96, 41.96, 41.96, 41.96],
            # こちらは毎日ちゃんと動いている実測
            "nikkei": [100.0, 101.0, 102.0, 103.0, 104.0],
        },
    }
    tmpf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(prev, tmpf, ensure_ascii=False)
    tmpf.close()
    saved = C.OUT_PATH
    C.OUT_PATH = tmpf.name
    back = C.load_previous()
    C.OUT_PATH = saved
    os.unlink(tmpf.name)
    check("補完でできた重複を1点に畳む", len(back["cape"]) == 1, str(back["cape"]))
    check("畳んだあとの日付が最初の実測日", list(back["cape"]) == ["2026-08-02"], str(back["cape"]))
    check("動いている系列はそのまま残す", len(back["nikkei"]) == 5, str(len(back["nikkei"])))

    print("\n[8] 通しで動かす")
    SHILLER_BLOB = make_shiller_long(cape_filled=False)   # 実物に近い30年ぶんで通す
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

    print("\n[9] ドル換算（その日のドル円で割る）")
    check("ドル建て日経が計算されている", len(table["nikkei_usd"]) > 0)
    check("ドル建てTOPIXが計算されている", len(table["topix_usd"]) > 0,
          "%d点" % len(table["topix_usd"]))
    # 同じ日の 指数 ÷ ドル円 になっているかを手計算で突き合わせる
    d0 = max(table["topix_usd"])
    tp = C.latest_on_or_before(table["topix"], d0)
    fx = C.latest_on_or_before(table["usdjpy"], d0)
    check("TOPIX ÷ ドル円 と一致する",
          abs(table["topix_usd"][d0] - tp / fx) < 0.01,
          "%s ÷ %.3f = %.3f / 値 %s" % (round(tp, 2), fx, tp / fx, table["topix_usd"][d0]))
    nk = C.latest_on_or_before(table["nikkei"], d0)
    check("日経 ÷ ドル円 と一致する",
          abs(table["nikkei_usd"][d0] - nk / fx) < 0.01)
    # ドル円が取れない日は作らない（円のまま出してしまわないこと）
    t2 = C.add_derived({"topix": {"2026-08-21": 4000.0}, "usdjpy": {}}, ["2026-08-21"])
    check("ドル円がなければドル建ては作らない", t2["topix_usd"] == {}, str(t2["topix_usd"]))

    print("\n" + "=" * 52)
    if FAIL:
        print("失敗 %d 件: %s" % (len(FAIL), " / ".join(FAIL)))
        return 1
    print("すべて通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
