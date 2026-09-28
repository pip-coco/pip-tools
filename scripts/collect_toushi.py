#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
投資ダッシュボード用のデータ収集スクリプト（第2版）。

GitHub Actions から毎日実行され、data/toushi.json を更新する。

■ 設計方針
  1. 取得先はすべて無料・APIキー不要。
  2. 1つの系列に複数の取得先を用意し、上から順に試す（カスケード）。
     GitHub のサーバーからはボット判定で弾かれるサイトがあるため、
     公的機関・本家（財務省・米財務省・BLS・LBMA・Shiller）を優先する。
  3. 期間で切らない。取れるだけ古くまで取り、以後は積み上げていく。
     古い区間を持っている取得先は、新しい取得先の手前につなぐ（接ぎ木）。
  4. 保存は「系列ごとに実測点だけ」。隙間を前の値で埋めて保存しない。
     埋めて保存すると、月1回の指標が直近の毎日ぶん水増しされ、
     割安・割高の判定が最近の値に引っ張られてしまうため。
  5. どこかが壊れても全体は止めない。取れなかった系列は前回値を保持する。
  6. 何をどう試して何が起きたかを全部 data/toushi-status.json に残す。
"""

import bisect
import datetime as dt
import io
import json
import os
import re
import sys
import time
from urllib.parse import quote, urljoin

import requests

# ---------------------------------------------------------------------------
# 基本設定
# ---------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_PATH = os.path.join(ROOT, "data", "toushi.json")
STATUS_PATH = os.path.join(ROOT, "data", "toushi-status.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": UA,
    "Accept": "text/csv,application/json,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
})

TODAY = dt.date.today()
FLOOR = dt.date(1850, 1, 1)        # これより古い日付は誤読とみなす

# 保存するときの間引き方（表示の期間切替に合わせてある）
#   直近 400日 = 日次   … 「1年」表示を日足で描くため
#   直近 5年   = 週次   … 「5年」表示を週足で描くため
#   それより前 = 月次
DAILY_DAYS = 400
WEEKLY_DAYS = 5 * 365 + 2

DIAG = []          # 取得の記録（成功も失敗も全部）
NOTES = []         # 画面に出す短いメモ


def diag(line):
    print("    " + line, file=sys.stderr)
    DIAG.append(line)


def valid_date(s):
    """使ってよい日付か。暦として正しく、1850年以降で、未来でないこと。"""
    try:
        d = dt.date.fromisoformat(s)
    except (ValueError, TypeError):
        return False
    return FLOOR <= d <= TODAY


def get(url, timeout=25, tries=2, **kw):
    """GET。失敗しても例外を投げずに None を返す。理由は get.last_error に残す。"""
    last = ""
    for i in range(tries):
        try:
            r = SESSION.get(url, timeout=timeout, **kw)
            if r.status_code == 200:
                get.last_error = ""
                return r
            last = "HTTP %d" % r.status_code
        except requests.exceptions.Timeout:
            last = "タイムアウト"
        except Exception as e:                       # noqa: BLE001
            last = type(e).__name__
        if i + 1 < tries:
            time.sleep(2)
    get.last_error = last
    return None


get.last_error = ""


def looks_like_botwall(text):
    """ボット判定ページが返ってきていないか見る。"""
    head = text[:600].lower()
    for sign in ("requires javascript", "enable javascript", "cf-browser-verification",
                 "just a moment", "captcha", "<!doctype html><html><head><meta charset"):
        if sign in head:
            return True
    return False


def decode_jp(content):
    """日本のサイトの CSV は UTF-8 と Shift-JIS が混在するので両方試す。"""
    for enc in ("utf-8-sig", "cp932"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("cp932", errors="replace")


def tidy(v):
    """値の桁をそろえる。Yahoo は 64611.1484375 のような余計な桁を付けてくるので、
    有効数字7桁に丸める（ファイルを軽くするため）。
    小数点以下の桁数で丸めると、ドル円（158.985 のように小数3桁で建つ）まで削れてしまう。"""
    return float("%.7g" % v)


def span(col):
    return "%s〜%s" % (min(col), max(col)) if col else "なし"


# ---------------------------------------------------------------------------
# 系列の定義
#   expensive_high: True なら「値が高い＝割高」。False なら「高い＝割安」。
#                   None なら割安割高の判定をしない（ただの水準）。
# ---------------------------------------------------------------------------

META = {
    "nikkei":      dict(label="日経平均株価",      unit="円",   group="price", expensive_high=None, source="Yahoo Finance ^N225 / 日経公式"),
    "topix":       dict(label="TOPIX",             unit="pt",   group="price", expensive_high=None, source="Yahoo Finance ^TPX"),
    "spx":         dict(label="S&P500",            unit="pt",   group="price", expensive_high=None, source="Yahoo Finance ^GSPC / Shiller"),
    "ndx":         dict(label="ナスダック100",     unit="pt",   group="price", expensive_high=None, source="Yahoo Finance ^NDX"),
    "gold":        dict(label="金",                unit="$/oz", group="price", expensive_high=None, source="LBMA 金価格(PM)"),
    "silver":      dict(label="銀",                unit="$/oz", group="price", expensive_high=None, source="LBMA 銀価格"),
    "usdjpy":      dict(label="ドル円",            unit="円",   group="price", expensive_high=None, source="Yahoo Finance JPY=X"),

    "nikkei_per":  dict(label="日経平均 PER",      unit="倍",   group="value", expensive_high=True,  source="日経平均プロフィル(加重平均)"),
    "nikkei_pbr":  dict(label="日経平均 PBR",      unit="倍",   group="value", expensive_high=True,  source="日経平均プロフィル(加重平均)"),
    "spx_per":     dict(label="S&P500 PER",        unit="倍",   group="value", expensive_high=True,  source="Shiller (株価÷12ヶ月利益)"),
    "cape":        dict(label="S&P500 CAPE",       unit="倍",   group="value", expensive_high=True,  source="Shiller CAPE"),

    "dgs10":       dict(label="米10年金利",        unit="%",    group="rate",  expensive_high=None, source="米財務省 / Shiller"),
    "real10":      dict(label="米10年 実質金利",   unit="%",    group="rate",  expensive_high=None, source="米財務省 実質イールドカーブ"),
    "bei10":       dict(label="10年 BEI",          unit="%",    group="rate",  expensive_high=None, source="名目10年 − 実質10年"),
    "jp10y":       dict(label="日本10年金利",      unit="%",    group="rate",  expensive_high=None, source="財務省 国債金利情報"),
    "us_cpi_yoy":  dict(label="米CPI 前年比",      unit="%",    group="rate",  expensive_high=None, source="BLS / Shiller"),
    "jp_cpi_yoy":  dict(label="日本CPI 前年比",    unit="%",    group="rate",  expensive_high=None, source="FRED JPNCPIALLMINMEI"),

    "nt":          dict(label="NT倍率",            unit="倍",   group="ratio", expensive_high=None, source="日経平均 ÷ TOPIX"),
    "gsr":         dict(label="金銀比価",          unit="倍",   group="ratio", expensive_high=None, source="金 ÷ 銀"),
    "nikkei_usd":  dict(label="ドル建て日経",      unit="$",    group="fx", expensive_high=None, source="日経平均 ÷ その日のドル円"),
    "topix_usd":   dict(label="ドル建てTOPIX",     unit="$",    group="fx", expensive_high=None, source="TOPIX ÷ その日のドル円"),
    "spx_jpy":     dict(label="円建てS&P500",      unit="円",   group="fx", expensive_high=None, source="S&P500 × その日のドル円"),
    "ndx_jpy":     dict(label="円建てナスダック100", unit="円", group="fx", expensive_high=None, source="ナスダック100 × その日のドル円"),
    "nikkei_gold": dict(label="日経 ÷ 金",         unit="oz",   group="ratio", expensive_high=True,  source="ドル建て日経 ÷ 金価格"),
    "ys_jp":       dict(label="日本 イールドスプレッド", unit="%", group="spread", expensive_high=False, source="100÷日経PER − 日本10年金利"),
    "ys_us":       dict(label="米国 イールドスプレッド", unit="%", group="spread", expensive_high=False, source="100÷S&P500PER − 米10年金利"),
}

# 計算で作る系列。取得はしない。
DERIVED = ("bei10", "nt", "gsr", "nikkei_usd", "topix_usd", "spx_jpy", "ndx_jpy",
           "nikkei_gold", "ys_jp", "ys_us")


# ---------------------------------------------------------------------------
# 接ぎ木（古い区間を別の取得先から補う）
# ---------------------------------------------------------------------------

def graft_older(main, older):
    """main が始まる日より前の区間だけ、older から足す。
    重なっている期間は main（新しくて正確な方）を優先する。"""
    if not older:
        return dict(main)
    if not main:
        return dict(older)
    first = min(main)
    out = {d: v for d, v in older.items() if d < first}
    out.update(main)
    return out


# TOPIX の実測値（倍率の基準点）。
# 2026-08-23 に Yahoo!ファイナンス（998405.T の時系列ページ）から取得した終値。
# Yahoo の ^TPX が 2026-09-28 に 404（銘柄なし）になり、本物の TOPIX が
# 1点も取れない日でも ETF からの換算が成り立つように、ここに固定で持っておく。
TOPIX_ANCHORS = {
    "2026-08-17": 4184.11,
    "2026-08-18": 4140.22,
    "2026-08-19": 4012.31,
    "2026-08-20": 4059.73,
    "2026-08-21": 4067.29,
}


def calibrate(real, proxy, recent=260, minimum=3):
    """proxy（例: TOPIX連動ETFの価格）を real（例: TOPIX）の水準に合わせる倍率。
    同じ日に両方ある点の比の中央値。配当の払い出しで比が季節的に揺れるため、
    重なりが多ければ直近1年ぶん程度をならして使う。"""
    both = sorted(d for d in real if d in proxy and proxy[d])
    both = both[-recent:]
    if len(both) < minimum:
        return None, len(both)
    ratios = sorted(real[d] / proxy[d] for d in both)
    return ratios[len(ratios) // 2], len(both)


# ---------------------------------------------------------------------------
# 取得先 1: Yahoo Finance（株価指数・為替・ETF）
#   チャートAPIを直接叩く。月足(全期間)＋週足(5年)＋日足(1年)を重ねる。
#   週足・月足は「その期間の終値」なので、期間の最終日の日付を付ける。
#   日付は取引所の現地時間で決める（UTCのままだと東京の月初が前月末にずれる）。
# ---------------------------------------------------------------------------

def yahoo_chart(symbol, rng, interval):
    for host in ("query2", "query1"):
        url = ("https://%s.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=%s"
               % (host, quote(symbol), rng, interval))
        r = get(url, timeout=25, tries=1)
        if r is None:
            continue
        try:
            j = r.json()
        except Exception:                            # noqa: BLE001
            continue
        res = (j.get("chart") or {}).get("result") or []
        if not res:
            err = ((j.get("chart") or {}).get("error") or {}).get("description", "")
            if err:
                diag("      %s %s/%s: %s" % (symbol, rng, interval, err[:60]))
            continue
        res = res[0]
        offset = int((res.get("meta") or {}).get("gmtoffset") or 0)
        stamps = res.get("timestamp") or []
        quotes = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        closes = quotes.get("close") or []
        out = {}
        for i in range(min(len(stamps), len(closes))):
            if closes[i] is None:
                continue
            day = dt.datetime.utcfromtimestamp(stamps[i] + offset).date()
            if interval == "1mo":                    # 月足 → その月の末日
                nxt = (day.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
                day = nxt - dt.timedelta(days=1)
            elif interval == "1wk":                  # 週足 → その週の金曜
                day = day + dt.timedelta(days=4)
            day = min(day, TODAY)
            out[day.isoformat()] = tidy(float(closes[i]))
        if out:
            return out
    return {}


def fetch_yahoo(symbols):
    """候補シンボルを順に試し、月足(全期間)＋週足(5年)＋日足(1年)を重ねる。"""
    for sym in symbols:
        monthly = yahoo_chart(sym, "max", "1mo")
        weekly = yahoo_chart(sym, "5y", "1wk")
        daily = yahoo_chart(sym, "1y", "1d")
        if not (monthly or weekly or daily):
            diag("      %s: 取れず" % sym)
            continue
        merged = {}
        merged.update(monthly)
        merged.update(weekly)
        merged.update(daily)                         # 細かいものほど優先
        diag("      %s: 月足%d + 週足%d + 日足%d → %d点 (%s)"
             % (sym, len(monthly), len(weekly), len(daily), len(merged), span(merged)))
        return merged
    return {}


# ---------------------------------------------------------------------------
# 取得先 2: 日経公式（日経平均の日次・直近数年）
#   列の並びは「日付, 終値, 始値, 高値, 安値」。位置ではなく見出しで探す。
# ---------------------------------------------------------------------------

def fetch_nikkei_official():
    url = "https://indexes.nikkei.co.jp/nkave/historical/nikkei_stock_average_daily_jp.csv"
    r = get(url, timeout=30, tries=2)
    if r is None:
        return {}
    text = decode_jp(r.content)
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return {}
    header = [h.strip().strip('"') for h in lines[0].split(",")]
    idx = None
    for i, h in enumerate(header):
        if "終値" in h:
            idx = i
            break
    if idx is None:
        diag("      日経公式: 終値の列が見つからず 見出し=%s" % header)
        return {}
    out = {}
    for line in lines[1:]:
        cells = [c.strip().strip('"') for c in line.split(",")]
        if len(cells) <= idx:
            continue
        m = re.match(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$", cells[0])
        if not m:
            continue
        try:
            out["%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))] = \
                float(cells[idx].replace(",", ""))
        except ValueError:
            continue
    return out


def agrees(a, b, tol=0.005):
    """2つの取得先が同じ日に同じような値を出しているか（中央値で ±0.5% 以内）。"""
    both = [d for d in a if d in b and b[d]]
    if len(both) < 5:
        return True, len(both)
    devs = sorted(abs(a[d] / b[d] - 1.0) for d in both)
    return devs[len(devs) // 2] <= tol, len(both)


# ---------------------------------------------------------------------------
# 取得先 3: LBMA（金・銀の国際指標価格。1968年から日次）
# ---------------------------------------------------------------------------

def fetch_lbma(name):
    """name は "gold_pm" か "silver"。v[0] がドル建て。"""
    r = get("https://prices.lbma.org.uk/json/%s.json" % name, timeout=60, tries=2)
    if r is None:
        return {}
    try:
        rows = r.json()
    except Exception:                                # noqa: BLE001
        diag("      LBMA %s: JSONとして読めず" % name)
        return {}
    out = {}
    for row in rows if isinstance(rows, list) else []:
        try:
            d, v = row.get("d"), (row.get("v") or [None])[0]
        except AttributeError:
            continue
        if d and isinstance(v, (int, float)) and v > 0:
            out[d] = float(v)
    return out


# ---------------------------------------------------------------------------
# 取得先 4: 米財務省（10年金利・10年実質金利。1990年/2003年から日次）
#   まず全年をまとめて1回で取り、だめなら年ごとに取る。
# ---------------------------------------------------------------------------

TREASURY_BASE = ("https://home.treasury.gov/resource-center/data-chart-center/"
                 "interest-rates/daily-treasury-rates.csv/%s/all"
                 "?type=%s&field_tdr_date_value=%s&page&_format=csv")


def _parse_treasury(text, col_names):
    lines = text.strip().splitlines()
    if not lines:
        return {}
    header = [h.strip().strip('"') for h in lines[0].split(",")]
    idx = next((header.index(w) for w in col_names if w in header), None)
    if idx is None:
        return {}
    out = {}
    for line in lines[1:]:
        cells = [c.strip().strip('"') for c in line.split(",")]
        if len(cells) <= idx or not cells[0]:
            continue
        try:
            m, d, y = cells[0].split("/")
            out["%04d-%02d-%02d" % (int(y), int(m), int(d))] = float(cells[idx])
        except (ValueError, IndexError):
            continue
    return out


def treasury_curve(kind, col_names, first_year):
    r = get(TREASURY_BASE % ("all", kind, "all"), timeout=90, tries=1)
    if r is not None and not looks_like_botwall(r.text):
        out = _parse_treasury(r.text, col_names)
        if len(out) > 2000:
            diag("      %s: 全年まとめて取得 %d点 (%s)" % (kind, len(out), span(out)))
            return out

    out, ok_years, miss_streak = {}, 0, 0
    for year in range(TODAY.year, first_year - 1, -1):
        if miss_streak >= 3:
            diag("      %s: 3年続けて取れないので %d年で打ち切り" % (kind, year))
            break
        r = get(TREASURY_BASE % (year, kind, year), timeout=20, tries=1)
        if r is None or looks_like_botwall(r.text):
            miss_streak += 1
            continue
        got = _parse_treasury(r.text, col_names)
        if not got:
            miss_streak += 1
            continue
        miss_streak = 0
        out.update(got)
        ok_years += 1
        time.sleep(0.3)
    diag("      %s: 年ごとに %d年分 %d点 (%s)" % (kind, ok_years, len(out), span(out)))
    return out


# ---------------------------------------------------------------------------
# 取得先 5: 財務省（日本の10年国債金利。1974年から）
#   日付は和暦（S=昭和, H=平成, R=令和）。0=日付, 1=1年 … 10=10年。
# ---------------------------------------------------------------------------

def fetch_jgb10y():
    url = "https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv"
    r = get(url, timeout=60, tries=2)
    if r is None:
        return {}
    text = r.content.decode("cp932", errors="replace")
    era = {"S": 1925, "H": 1988, "R": 2018}
    out = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) < 11:
            continue
        m = re.match(r"^([SHR])(\d+)\.(\d+)\.(\d+)$", cells[0])
        if not m:
            continue
        try:
            year = era[m.group(1)] + int(m.group(2))
            out["%04d-%02d-%02d" % (year, int(m.group(3)), int(m.group(4)))] = float(cells[10])
        except (ValueError, KeyError):
            continue
    return out


# ---------------------------------------------------------------------------
# 取得先 6: 日経平均プロフィル（日経の PER / PBR。いまは当月ぶんのみ）
#   表の列は「日付 / 加重平均(倍) / 指数ベース(倍)」。加重平均を使う。
# ---------------------------------------------------------------------------

def fetch_nikkei_ratio(kind):
    url = "https://indexes.nikkei.co.jp/nkave/archives/data?list=%s" % kind
    r = get(url, timeout=25, tries=2)
    if r is None:
        return {}
    r.encoding = r.apparent_encoding or "utf-8"
    html = r.text
    rows = re.findall(
        r"(\d{4})\.(\d{2})\.(\d{2})\s*</td>\s*<td[^>]*>\s*([\d.]+)\s*</td>\s*<td[^>]*>\s*([\d.]+)",
        html)
    if not rows:
        rows = re.findall(
            r"(\d{4})\.(\d{2})\.(\d{2})[^\d]{1,80}?([\d]+\.[\d]+)[^\d]{1,80}?([\d]+\.[\d]+)", html)
    out = {}
    for y, mo, d, weighted, _idx in rows:
        try:
            out["%s-%s-%s" % (y, mo, d)] = float(weighted)
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------------------
# 取得先 7: Shiller (Yale)。1871年からの月次。
#   株価・利益・物価・長期金利・CAPE をまとめて取り出す。
# ---------------------------------------------------------------------------

def _shiller_download():
    """shillerdata.com のページから ie_data.xls の場所を見つけて落とす。
    配布URLには版番号が付いていて更新のたびに変わるので、毎回ページから拾う。"""
    page = get("https://shillerdata.com/", timeout=30, tries=2)
    urls = []
    if page is not None:
        for href in re.findall(r'href="([^"]*ie_data\.xls[^"]*)"', page.text):
            href = href.replace("&amp;", "&")
            # ページ内のリンクは "/downloads/..." のように途中から書かれていることがある。
            # そのまま渡すと requests が MissingSchema で落ちるので、必ず絶対URLに直す。
            urls.append(urljoin("https://shillerdata.com/", href))
    # ページから拾えなかったときの控え（2026-08 時点で有効だったURL）
    urls.append("https://img1.wsimg.com/blobby/go/e5e77e0b-59d1-44d9-ab25-4763ac982e53"
                "/downloads/e27e58c1-8ae0-488c-a976-a298708c7175/ie_data.xls")
    for u in urls:
        r = get(u, timeout=90, tries=1)
        if r is None:
            diag("      Shiller: 取れず [%s] (%s)" % (get.last_error, u.split("/")[2]))
            continue
        head = r.content[:4]
        # 中身が本当に Excel か、マジックナンバーで確かめる
        if head[:2] == b"PK" or head == b"\xd0\xcf\x11\xe0":
            diag("      Shiller: %d KB 取得 (%s)" % (len(r.content) // 1024, u.split("/")[2]))
            return r.content
        diag("      Shiller: Excelではない応答 (%s)" % u.split("/")[2])
    return None


def _excel_rows(blob):
    """Excel を読んで、Data シートの行を list で返す。旧形式(.xls)にも対応。"""
    if blob[:2] == b"PK":                            # .xlsx / .xlsm
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(blob), data_only=True, read_only=True)
        names = wb.sheetnames
        pick = next((n for n in names if n.strip().lower().startswith("data")), names[0])
        return list(wb[pick].iter_rows(values_only=True))
    import xlrd                                      # .xls（Excel 97-2003）
    book = xlrd.open_workbook(file_contents=blob)
    names = book.sheet_names()
    pick = next((n for n in names if n.strip().lower().startswith("data")), names[0])
    sheet = book.sheet_by_name(pick)
    return [tuple(sheet.row_values(i)) for i in range(sheet.nrows)]


def fetch_shiller():
    """戻り値: {"price","earn","cpi","gs10","cape","spx_per"} それぞれ {日付: 値}。"""
    blob = _shiller_download()
    if blob is None:
        return {}
    try:
        rows = _excel_rows(blob)
    except Exception as e:                           # noqa: BLE001
        diag("      Shiller: Excelを開けず (%s: %s)" % (type(e).__name__, str(e)[:60]))
        return {}

    def norm(v):
        return " ".join(str(v).split()).lower() if v is not None else ""

    # 見出しは上下の行に割れていることがあるので、Date の行と上2行を列ごとに繋いで探す
    col = dict(date=None, p=None, e=None, cpi=None, gs10=None, cape=None)
    for ri, row in enumerate(rows[:14]):
        cells = [norm(c) for c in row]
        if not any(c == "date" for c in cells):
            continue
        col["date"] = cells.index("date")
        block = rows[max(0, ri - 2):ri + 1]
        width = max(len(r) for r in block)
        merged = [" ".join(norm(r[i]) for r in block if i < len(r) and norm(r[i]))
                  for i in range(width)]
        for i, c in enumerate(merged):
            if not c:
                continue
            if (col["cape"] is None and ("cape" in c or "p/e10" in c or "pe10" in c)
                    and "excess" not in c and "yield" not in c and "tr cape" not in c):
                col["cape"] = i
            if col["p"] is None and "comp" in c:
                col["p"] = i
            if col["e"] is None and "earnings" in c and "real" not in c and "scaled" not in c:
                col["e"] = i
            if col["cpi"] is None and ("consumer" in c or c.strip() == "cpi"):
                col["cpi"] = i
            if col["gs10"] is None and ("gs10" in c or "long interest" in c):
                col["gs10"] = i
        diag("      Shiller: 見出し = " + " | ".join(m[:26] for m in merged[:16]))
        diag("      Shiller: 列 " + " ".join("%s=%s" % (k, v) for k, v in col.items()))
        break
    if col["date"] is None:
        diag("      Shiller: 見出し行が見つからず")
        return {}

    def cell(row, i):
        if i is None or i >= len(row):
            return None
        v = row[i]
        return float(v) if isinstance(v, (int, float)) else None

    price, earn, cpi, gs10, cape_col, per = {}, {}, {}, {}, {}, {}
    for row in rows:
        d = cell(row, col["date"])
        if d is None:
            continue
        year = int(d)                                # 1871.01 のような小数。小数第2位が月
        month = int(round((d - year) * 100))
        if year < 1871 or not 1 <= month <= 12:
            continue
        date = "%04d-%02d-01" % (year, month)
        p, e, ci = cell(row, col["p"]), cell(row, col["e"]), cell(row, col["cpi"])
        g, c = cell(row, col["gs10"]), cell(row, col["cape"])
        if p:
            price[date] = p
        if e:
            earn[date] = e
        if ci:
            cpi[date] = ci
        if g is not None and 0 < g < 25:             # 長期金利がありえる範囲か
            gs10[date] = g
        if c and 3 < c < 80:
            cape_col[date] = round(c, 2)
        if p and e and e > 0 and 3 < p / e < 120:
            per[date] = round(p / e, 2)

    # CAPE の列が本当に CAPE か、名前ではなく中身で確かめる（結合セルで隣の空列を掴むことがある）
    cape = cape_col if _looks_like_cape(cape_col) else {}
    if cape:
        diag("      Shiller: CAPE は見出しの列から取得")
    else:
        if cape_col:
            diag("      Shiller: 見出しの列は CAPE らしくないので使わない（%d点）" % len(cape_col))
        cape = _cape_from_pe(price, earn, cpi)
        if cape:
            diag("      Shiller: CAPE を定義どおり計算して作成")
    diag("      Shiller: 株価%d 利益%d CPI%d 長期金利%d CAPE%d PER%d"
         % (len(price), len(earn), len(cpi), len(gs10), len(cape), len(per)))
    if cape:
        last = max(cape)
        diag("      Shiller: CAPE 最新 %s = %s" % (last, cape[last]))
    return {"price": price, "earn": earn, "cpi": cpi, "gs10": gs10,
            "cape": cape, "spx_per": per}


def _looks_like_cape(col):
    """CAPE らしい数字の並びかどうかを、名前ではなく中身で判定する。"""
    if len(col) < 200:
        return False
    vals = sorted(v for _, v in sorted(col.items())[-240:])
    median = vals[len(vals) // 2]
    return 5.0 <= median <= 60.0


def _cape_from_pe(price, earn, cpi):
    """CAPE = 実質株価 ÷ 過去10年(120ヶ月)の実質利益の平均。
    実質化の基準時点は分子と分母で約分されて消えるので、CPI で割るだけでよい。"""
    real_e_dates, real_e_vals = [], []
    for d in sorted(earn):
        c = cpi.get(d)
        if c:
            real_e_dates.append(d)
            real_e_vals.append(earn[d] / c)
    if len(real_e_vals) < 120:
        return {}
    out = {}
    for d in sorted(price):
        c = cpi.get(d)
        if not c:
            continue
        hi = bisect.bisect_right(real_e_dates, d)
        window = real_e_vals[max(0, hi - 120):hi]
        if len(window) < 120:                         # 丸10年ぶん揃うまでは出さない（Shiller の定義）
            continue
        avg = sum(window) / len(window)
        if avg <= 0:
            continue
        v = (price[d] / c) / avg
        if 3 < v < 80:
            out[d] = round(v, 2)
    return out


# ---------------------------------------------------------------------------
# 取得先 8: BLS（米CPI。直近20年）と FRED / multpl（控え）
# ---------------------------------------------------------------------------

def fetch_bls_cpi():
    """米CPI（都市部・全品目）の月次指数。キーなしの公開APIは1回10年までなので2回に分ける。"""
    out = {}
    for y1 in (TODAY.year, TODAY.year - 10):
        y0 = y1 - 9
        url = ("https://api.bls.gov/publicAPI/v1/timeseries/data/CUUR0000SA0"
               "?startyear=%d&endyear=%d" % (y0, y1))
        r = get(url, timeout=30, tries=2)
        if r is None:
            continue
        try:
            j = r.json()
        except Exception:                            # noqa: BLE001
            continue
        if j.get("status") != "REQUEST_SUCCEEDED":
            diag("      BLS: %s" % str(j.get("message"))[:80])
            continue
        for s in (j.get("Results") or {}).get("series", []):
            for row in s.get("data", []):
                try:
                    year = int(row["year"])
                    month = int(row["period"].replace("M", ""))
                    if not 1 <= month <= 12:
                        continue          # M13（年平均）は月次ではないので使わない
                    out["%04d-%02d-01" % (year, month)] = float(row["value"])
                except (ValueError, KeyError):
                    continue
        time.sleep(1)
    return out


def fetch_fred(series_id):
    if not series_id:
        return {}
    r = get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s" % series_id,
            timeout=25, tries=1)
    if r is None or looks_like_botwall(r.text):
        return {}
    out = {}
    for line in r.text.strip().splitlines()[1:]:
        cells = line.split(",")
        if len(cells) < 2 or cells[1].strip() in (".", "", "NA"):
            continue
        try:
            out[cells[0].strip()] = float(cells[1])
        except ValueError:
            continue
    return out


def fetch_multpl(path):
    url = "https://www.multpl.com/%s/table/by-month" % path
    r = get(url, timeout=25, tries=1)
    if r is None or looks_like_botwall(r.text):
        return {}
    rows = re.findall(
        r"<td[^>]*>\s*([A-Z][a-z]{2})\s+(\d{1,2}),\s*(\d{4})\s*</td>\s*<td[^>]*>\s*([\d.]+)",
        r.text)
    months = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
              "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
    out = {}
    for mon, day, year, val in rows:
        if mon in months:
            try:
                out["%s-%02d-%02d" % (year, months[mon], int(day))] = float(val)
            except ValueError:
                continue
    return out


def to_yoy(monthly):
    """月次の指数から前年同月比(%)を作る。"""
    out = {}
    for date, val in monthly.items():
        y, m, d = date.split("-")
        prev = "%04d-%s-%s" % (int(y) - 1, m, d)
        if monthly.get(prev):
            out[date] = round((val / monthly[prev] - 1.0) * 100.0, 2)
    return out


# ---------------------------------------------------------------------------
# 下調べ（データには使わない。構造を記録するだけ）
#   次の改修で過去データを取り込むための偵察。結果は status の diag に残る。
# ---------------------------------------------------------------------------

def probe_nikkei_archive():
    """日経PERの過去分（2004年〜）を選ぶ仕組みが、どういうURLや部品でできているかを記録する。"""
    r = get("https://indexes.nikkei.co.jp/nkave/archives/data?list=per", timeout=25, tries=1)
    if r is None:
        diag("  [偵察] 日経PERアーカイブ: 取れず [%s]" % get.last_error)
        return
    r.encoding = r.apparent_encoding or "utf-8"
    html = r.text
    found = []
    for m in re.finditer(r"<(a|option|form|select|input|button)\b[^>]*>", html, re.I):
        tag = m.group(0)
        if re.search(r"year|month|list=per|2004|2025|archives", tag, re.I):
            found.append(re.sub(r"\s+", " ", tag)[:180])
    for m in re.finditer(r"(?:function|fetch|\$\.ajax|\.get\(|\.post\(|location\.href)[^;\n]{0,160}", html):
        s = m.group(0)
        if re.search(r"year|month|per|archive", s, re.I):
            found.append("[js] " + re.sub(r"\s+", " ", s)[:180])
    seen = []
    for f in found:
        if f not in seen:
            seen.append(f)
    diag("  [偵察] 日経PERアーカイブ: 部品 %d 個" % len(seen))
    for f in seen[:25]:
        diag("    " + f)


def probe_jpx_topix():
    """JPX の TOPIX 長期データ（年次表・月次レポート）の中身の形を記録する。"""
    url = "https://www.jpx.co.jp/markets/indices/topix/tvdivq00000030ne-att/topixyear_j.xls"
    r = get(url, timeout=30, tries=1)
    if r is None:
        diag("  [偵察] JPX topixyear: 取れず [%s]" % get.last_error)
    else:
        try:
            rows = _excel_rows(r.content)
            diag("  [偵察] JPX topixyear: %d行" % len(rows))
            for row in rows[:8] + rows[-3:]:
                diag("    " + " | ".join(str(c)[:14] for c in row[:10]))
        except Exception as e:                       # noqa: BLE001
            diag("  [偵察] JPX topixyear: 読めず %s (%d bytes, 先頭 %r)"
                 % (type(e).__name__, len(r.content), r.content[:8]))
    url2 = ("https://www.jpx.co.jp/automation/markets/indices/related/report/files/"
            "monthlyindexreport_j.csv")
    r2 = get(url2, timeout=30, tries=1)
    if r2 is None:
        diag("  [偵察] JPX 月次指数レポート: 取れず [%s]" % get.last_error)
    else:
        lines = [l for l in decode_jp(r2.content).splitlines() if l.strip()]
        diag("  [偵察] JPX 月次指数レポート: %d行" % len(lines))
        for l in lines[:6] + lines[-2:]:
            diag("    " + l[:200])


# ---------------------------------------------------------------------------
# カスケード実行
# ---------------------------------------------------------------------------

def try_sources(key, candidates):
    """候補を上から試し、最初に中身が返ったものを採用する。"""
    label = META[key]["label"]
    for rank, (name, fn) in enumerate(candidates):
        t0 = time.time()
        try:
            data = fn()
        except Exception as e:                       # noqa: BLE001
            diag("  %s ← %s : 例外 %s (%s)" % (label, name, type(e).__name__, str(e)[:60]))
            continue
        sec = time.time() - t0
        if data:
            diag("  %s ← %s : OK %d点 (%s) %.1f秒" % (label, name, len(data), span(data), sec))
            if rank > 0:
                NOTES.append("%s は控えの %s から取得しました" % (label, name))
            return data
        diag("  %s ← %s : 空 [%s] %.1f秒" % (label, name, get.last_error or "内容なし", sec))
    NOTES.append("%s を取得できませんでした（前回値を保持）" % label)
    return {}


def collect():
    raw = {}

    print("■ Shiller（S&P500・CAPE・米金利・米CPI の長期分）")
    try:
        shiller = fetch_shiller()
    except Exception as e:                           # noqa: BLE001
        diag("  Shiller: 例外 %s" % type(e).__name__)
        shiller = {}

    print("■ 日経平均")
    nk = try_sources("nikkei", [
        ("Yahoo Finance", lambda: fetch_yahoo(["^N225"])),
        ("FRED", lambda: fetch_fred("NIKKEI225")),
    ])
    official = fetch_nikkei_official()
    if official:
        ok, n = agrees(official, nk)
        if ok:
            nk = dict(nk)
            nk.update(official)                      # 公式の終値で上書き
            diag("  日経平均 ← 日経公式で %d点 上書き（Yahooと%d点で一致を確認）"
                 % (len(official), n))
        else:
            diag("  日経平均: 日経公式が Yahoo と食い違うので使わない（%d点で比較）" % n)
    raw["nikkei"] = nk

    print("■ TOPIX（本物＋TOPIX連動ETFで補う）")
    real = try_sources("topix", [
        ("Yahoo Finance", lambda: fetch_yahoo(["^TPX", "998405.T", "^TOPX"])),
    ])
    etf = fetch_yahoo(["1306.T", "1305.T"])
    raw["topix"] = real
    raw["_topix_etf"] = etf                          # 較正と穴埋めに使う（保存はしない）

    print("■ S&P500")
    spx = try_sources("spx", [("Yahoo Finance", lambda: fetch_yahoo(["^GSPC"]))])
    raw["spx"] = graft_older(spx, shiller.get("price", {}))
    if spx and shiller.get("price"):
        diag("  S&P500: %s より前を Shiller の月次で補った" % min(spx))

    print("■ ナスダック100")
    raw["ndx"] = try_sources("ndx", [("Yahoo Finance", lambda: fetch_yahoo(["^NDX"]))])

    print("■ 金・銀（LBMA）")
    raw["gold"] = try_sources("gold", [
        ("LBMA", lambda: fetch_lbma("gold_pm")),
        ("Yahoo Finance", lambda: fetch_yahoo(["GC=F"])),
    ])
    raw["silver"] = try_sources("silver", [
        ("LBMA", lambda: fetch_lbma("silver")),
        ("Yahoo Finance", lambda: fetch_yahoo(["SI=F"])),
    ])

    print("■ ドル円")
    raw["usdjpy"] = try_sources("usdjpy", [
        ("Yahoo Finance", lambda: fetch_yahoo(["JPY=X", "USDJPY=X"])),
        ("FRED", lambda: fetch_fred("DEXJPUS")),
    ])

    print("■ 米国の金利（米財務省＋Shiller）")
    dgs10 = try_sources("dgs10", [
        ("米財務省", lambda: treasury_curve("daily_treasury_yield_curve", ["10 Yr", "10 YR"], 1990)),
        ("FRED",     lambda: fetch_fred("DGS10")),
    ])
    raw["dgs10"] = graft_older(dgs10, shiller.get("gs10", {}))
    if dgs10 and shiller.get("gs10"):
        diag("  米10年金利: %s より前を Shiller の長期金利で補った" % min(dgs10))
    raw["real10"] = try_sources("real10", [
        ("米財務省", lambda: treasury_curve("daily_treasury_real_yield_curve", ["10 YR", "10 Yr"], 2003)),
        ("FRED",     lambda: fetch_fred("DFII10")),
    ])

    print("■ 日本の金利（財務省）")
    raw["jp10y"] = try_sources("jp10y", [
        ("財務省 国債金利情報", fetch_jgb10y),
        ("FRED",               lambda: fetch_fred("IRLTLT01JPM156N")),
    ])

    print("■ 日経のバリュエーション（日経平均プロフィル）")
    raw["nikkei_per"] = try_sources("nikkei_per", [("日経平均プロフィル", lambda: fetch_nikkei_ratio("per"))])
    raw["nikkei_pbr"] = try_sources("nikkei_pbr", [("日経平均プロフィル", lambda: fetch_nikkei_ratio("pbr"))])

    print("■ S&P500 のバリュエーション（Shiller）")
    raw["cape"] = try_sources("cape", [
        ("Shiller (Yale)", lambda: shiller.get("cape", {})),
        ("multpl",         lambda: fetch_multpl("shiller-pe")),
    ])
    raw["spx_per"] = try_sources("spx_per", [
        ("Shiller (Yale)", lambda: shiller.get("spx_per", {})),
        ("multpl",         lambda: fetch_multpl("s-p-500-pe-ratio")),
    ])

    print("■ インフレ")
    bls = try_sources("us_cpi_yoy", [
        ("BLS 公開API", fetch_bls_cpi),
        ("FRED",        lambda: fetch_fred("CPIAUCSL")),
    ])
    # Shiller の CPI も同じ BLS の CPI-U。BLS が始まる前の区間を補う
    raw["us_cpi_yoy"] = to_yoy(graft_older(bls, shiller.get("cpi", {})))
    raw["jp_cpi_yoy"] = to_yoy(try_sources("jp_cpi_yoy", [
        ("FRED", lambda: fetch_fred("JPNCPIALLMINMEI")),
    ]))

    print("■ 下調べ（次の改修用）")
    for probe in (probe_nikkei_archive, probe_jpx_topix):
        try:
            probe()
        except Exception as e:                       # noqa: BLE001
            diag("  [偵察] %s: 例外 %s" % (probe.__name__, type(e).__name__))

    return raw


# ---------------------------------------------------------------------------
# 保存形式の読み書き・統合・間引き・派生指標
# ---------------------------------------------------------------------------

def to_int_date(iso):
    return int(iso.replace("-", ""))


def from_int_date(n):
    n = int(n)
    return "%04d-%02d-%02d" % (n // 10000, n // 100 % 100, n % 100)


def load_previous():
    """前回のファイルを {系列: {日付: 値}} に戻す。旧形式（第1版）からの移行もここで行う。"""
    if not os.path.exists(OUT_PATH):
        return {}
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            prev = json.load(f)
    except Exception as e:                           # noqa: BLE001
        diag("前回の JSON を読めなかった: %s" % e)
        return {}

    out = {}
    if prev.get("version") == 2:
        for key, s in (prev.get("series") or {}).items():
            t, v = s.get("t") or [], s.get("v") or []
            out[key] = {from_int_date(t[i]): v[i] for i in range(min(len(t), len(v)))
                        if v[i] is not None}
        return out

    # 第1版：全系列が共通の日付軸に並び、隙間は前の値で埋められている。
    # 同じ値が続く区間は埋めた結果なので先頭だけ残して、実測点に戻す。
    dates = prev.get("dates", [])
    for key, values in (prev.get("series") or {}).items():
        col, prev_val = {}, object()
        for i, v in enumerate(values):
            if v is None or i >= len(dates):
                continue
            if v == prev_val:
                continue
            col[dates[i]] = v
            prev_val = v
        out[key] = col
    diag("前回ファイルは旧形式（第1版）だったので新形式に移行した")
    return out


def merge(previous, fresh):
    """前回値と今回ぶんを合わせ、おかしな日付をここで一括して落とす。

    今回取れた期間の「内側」は今回の値だけを使い、前回の値は捨てる。
    前回の値を残すのは、今回取れた期間の「外側」だけ。
      ・全期間を取り直せた系列 … 取り直した値で丸ごと入れ替わる
        （取得先を替えたときに、古い取得先の点が紛れ込んで残らない）
      ・当月ぶんしか取れない系列（日経PERなど） … 過去の積み上げは外側なので残る
      ・今回まったく取れなかった系列 … 前回値をそのまま保つ
    """
    merged, dropped = {}, 0
    for key in set(previous) | set(fresh):
        new = {d: v for d, v in fresh.get(key, {}).items() if valid_date(d)}
        base = dict(previous.get(key, {}))
        if new:
            lo, hi = min(new), max(new)
            base = {d: v for d, v in base.items() if d < lo or d > hi}
        base.update(new)
        kept = {d: v for d, v in base.items() if valid_date(d)}
        dropped += len(base) - len(kept)
        merged[key] = kept
    if dropped:
        diag("おかしな日付を %d 点ぶん落とした（未来日・存在しない日付など）" % dropped)
    return merged


def thin(col):
    """直近400日=日次 / 5年まで=週次（週の最後の点） / それ以前=月次（月の最後の点）。
    何度かけても同じ結果になる（最後の点は最後の点のまま残る）。"""
    if not col:
        return {}
    d_daily = (TODAY - dt.timedelta(days=DAILY_DAYS)).isoformat()
    d_weekly = (TODAY - dt.timedelta(days=WEEKLY_DAYS)).isoformat()
    keep, week_last, month_last = {}, {}, {}
    for d in sorted(col):
        if d >= d_daily:
            keep[d] = col[d]
        elif d >= d_weekly:
            y, w, _ = dt.date.fromisoformat(d).isocalendar()
            week_last[(y, w)] = d
        else:
            month_last[d[:7]] = d
    for d in list(week_last.values()) + list(month_last.values()):
        keep[d] = col[d]
    return keep


def lookup(col, tol_days):
    """「その日以前でいちばん新しい値」を、tol_days 以内に限って引く関数を返す。
    古すぎる値を黙って使わないための歯止め。"""
    dates = sorted(col)
    tol = dt.timedelta(days=tol_days)

    def at(d):
        i = bisect.bisect_right(dates, d) - 1
        if i < 0:
            return None
        if dt.date.fromisoformat(d) - dt.date.fromisoformat(dates[i]) > tol:
            return None
        return col[dates[i]]
    return at


def splice_topix(real, etf, previous_real):
    """本物のTOPIXがない日を、TOPIX連動ETFの価格×倍率で埋める。
    倍率は本物と重なっている日の比から決める。
    基準にするのは、固定の実測値（TOPIX_ANCHORS）＋前回の保存値＋今回取れた本物。"""
    ref = dict(TOPIX_ANCHORS)
    ref.update(previous_real)
    ref.update(real)
    k, n = calibrate(ref, etf)
    if k is None:
        if etf:
            diag("  TOPIX: ETFと本物の重なりが%d点しかなく、倍率を決められないので補わない" % n)
        return dict(real), None
    have = sorted(ref)
    near = lookup({d: 1 for d in have}, 3)           # 本物が±3日以内にある日は補わない
    filled = {}
    for d, v in etf.items():
        if near(d) is None:
            filled[d] = round(v * k, 2)
    out = dict(filled)
    out.update(ref)                                  # 実測値（固定の基準点・前回分・今回分）は必ず残す
    if filled:
        diag("  TOPIX: ETFから %d点 補った（%s、倍率 %.4f を %d点で較正）"
             % (len(filled), span(filled), k, n))
    return out, (min(filled), max(filled)) if filled else None


def add_derived(table):
    """比率・BEI・イールドスプレッドを計算する。
    基準になる系列の日付ごとに、相手の系列の「直近で、しかも古すぎない値」を使う。"""
    g = lambda k: table.get(k, {})                   # noqa: E731
    out = {}

    def ratio(key, base, other, tol, fn):
        at = lookup(g(other), tol)
        res = {}
        for d, v in g(base).items():
            o = at(d)
            if v is not None and o is not None:
                r = fn(v, o)
                if r is not None:
                    res[d] = r
        out[key] = res
        table[key] = res                              # 後続の計算で使えるように

    ratio("bei10", "real10", "dgs10", 7, lambda r, n: round(n - r, 2))
    ratio("nt", "topix", "nikkei", 7, lambda t, n: round(n / t, 3) if t else None)
    ratio("gsr", "gold", "silver", 7, lambda gd, sv: round(gd / sv, 2) if sv else None)
    # ドル換算は「その日のドル円」で割る。円安で上がっただけなのかを見分けるため
    ratio("nikkei_usd", "nikkei", "usdjpy", 7, lambda n, fx: round(n / fx, 2) if fx else None)
    ratio("topix_usd", "topix", "usdjpy", 7, lambda t, fx: round(t / fx, 3) if fx else None)
    # 円建ては「その日のドル円」を掛ける。日本から買ったときの値動き（為替込み）を見るため
    ratio("spx_jpy", "spx", "usdjpy", 7, lambda s, fx: round(s * fx))
    ratio("ndx_jpy", "ndx", "usdjpy", 7, lambda n, fx: round(n * fx))
    ratio("nikkei_gold", "nikkei_usd", "gold", 7, lambda n, gd: round(n / gd, 4) if gd else None)
    ratio("ys_jp", "nikkei_per", "jp10y", 10, lambda p, j: round(100.0 / p - j, 2) if p else None)
    ratio("ys_us", "spx_per", "dgs10", 45, lambda p, u: round(100.0 / p - u, 2) if p else None)
    return out


def main():
    t0 = time.time()
    previous = load_previous()
    fresh = collect()

    # TOPIX は本物と ETF をつなぐ。倍率の較正には前回保存した本物も使う
    etf = fresh.pop("_topix_etf", {})
    real_topix = fresh.get("topix", {})
    topix, filled_span = splice_topix(real_topix, etf, previous.get("topix", {}))
    fresh["topix"] = topix
    if filled_span and real_topix:
        META["topix"]["source"] = ("Yahoo Finance ^TPX（%s〜%s は TOPIX連動ETF 1306 から換算）"
                                   % (filled_span[0][:7], filled_span[1][:7]))
    elif filled_span:
        # 本物の TOPIX が今回1点も取れなかった日。ほぼ全部が換算値であることを正直に書く
        META["topix"]["source"] = ("TOPIX連動ETF 1306 から換算（本物の TOPIX の取得先が不通のため。"
                                   "倍率は実測値で較正）")

    # 派生指標は毎回計算し直すので、前回ぶんは持ち越さない
    for k in DERIVED:
        previous.pop(k, None)
    table = merge(previous, fresh)
    table = {k: v for k, v in table.items() if k in META}   # 定義にない系列は捨てる
    add_derived(table)

    # 実測点だけを、間引いてから保存する
    series, asof, health = {}, {}, {}
    for key in META:
        col = thin(table.get(key, {}))
        dates = sorted(col)
        series[key] = {"t": [to_int_date(d) for d in dates], "v": [col[d] for d in dates]}
        asof[key] = dates[-1] if dates else None
        health[key] = {"points": len(dates), "first": dates[0] if dates else None,
                       "last": asof[key], "recent": [[d, col[d]] for d in dates[-3:]]}

    empty = [META[k]["label"] for k in META if not asof.get(k)]
    if empty:
        NOTES.append("未取得: " + " / ".join(empty))

    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    payload = {
        "version": 2,
        "updated": stamp,
        "asof": asof,
        "notes": NOTES,
        "meta": META,
        "series": series,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

    total = sum(h["points"] for h in health.values())
    status = {
        "updated": stamp,
        "version": 2,
        "elapsed_sec": round(time.time() - t0),
        "size_kb": round(os.path.getsize(OUT_PATH) / 1024.0),
        "total_points": total,
        "missing": empty,
        "notes": NOTES,
        "health": health,
        "diag": DIAG,
    }
    with open(STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=1)

    print("\n" + "=" * 60)
    print("系列 %d 本 / 合計 %d 点 / %.0f KB / %.0f 秒"
          % (len(META), total, os.path.getsize(OUT_PATH) / 1024.0, time.time() - t0))
    for key in META:
        h = health[key]
        print("  %-14s %5d点  %s〜%s" % (META[key]["label"], h["points"], h["first"], h["last"]))
    if empty:
        print("取れなかったもの: " + " / ".join(empty))
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
