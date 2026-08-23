#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
投資ダッシュボード用のデータ収集スクリプト。

GitHub Actions から毎日実行され、data/toushi.json を更新する。

方針:
  - 取得先はすべて無料・APIキー不要（Stooq / FRED / 日経公式 / multpl）。
  - どれか1つが壊れても全体は止めない。取れたものだけ更新し、
    取れなかったものは前回の値をそのまま残す。
  - 履歴は「直近3ヶ月=日次 / 2年前まで=週次 / 20年前まで=月次」に間引いて
    ファイルを軽く保つ（スマホで開くため）。
"""

import io
import json
import os
import re
import sys
import time
import datetime as dt
from urllib.parse import quote

import requests

# ---------------------------------------------------------------------------
# 基本設定
# ---------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_PATH = os.path.join(ROOT, "data", "toushi.json")

YEARS = 20                      # 何年分もつか
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"})

TODAY = dt.date.today()
START = TODAY - dt.timedelta(days=365 * YEARS + 10)

# 収集中に起きた問題を貯めて、最後に JSON へ書き出す（画面に出すため）
NOTES = []


def note(msg):
    print("  ! " + msg, file=sys.stderr)
    NOTES.append(msg)


def get(url, **kw):
    """GET。失敗しても例外を投げずに None を返す。"""
    for attempt in range(3):
        try:
            r = SESSION.get(url, timeout=45, **kw)
            if r.status_code == 200:
                return r
            note("HTTP %s: %s" % (r.status_code, url))
        except Exception as e:                      # noqa: BLE001
            note("取得失敗(%d回目) %s: %s" % (attempt + 1, url, e))
        time.sleep(3 * (attempt + 1))
    return None


# ---------------------------------------------------------------------------
# 系列の定義
#   id            : JSON でのキー
#   label         : 画面に出す日本語名
#   unit          : 単位
#   group         : 画面のどのブロックに置くか
#   expensive_high: True なら「値が高い＝割高」。False なら「高い＝割安」
#                   （イールドスプレッドは高いほど株が有利＝割安なので False）
#   None なら割安割高の判定をしない（ただの水準）
# ---------------------------------------------------------------------------

META = {
    # --- 価格 ---
    "nikkei":      dict(label="日経平均株価",      unit="円",   group="price", expensive_high=None, source="Stooq ^NKX"),
    "topix":       dict(label="TOPIX",             unit="pt",   group="price", expensive_high=None, source="Stooq ^TPX"),
    "spx":         dict(label="S&P500",            unit="pt",   group="price", expensive_high=None, source="Stooq ^SPX"),
    "gold":        dict(label="金",                unit="$/oz", group="price", expensive_high=None, source="Stooq XAUUSD"),
    "silver":      dict(label="銀",                unit="$/oz", group="price", expensive_high=None, source="Stooq XAGUSD"),
    "usdjpy":      dict(label="ドル円",            unit="円",   group="price", expensive_high=None, source="Stooq USDJPY"),

    # --- バリュエーション（割安・割高の本体） ---
    "nikkei_per":  dict(label="日経平均 PER",      unit="倍",   group="value", expensive_high=True,  source="日経平均プロフィル(加重平均)"),
    "nikkei_pbr":  dict(label="日経平均 PBR",      unit="倍",   group="value", expensive_high=True,  source="日経平均プロフィル(加重平均)"),
    "spx_per":     dict(label="S&P500 PER",        unit="倍",   group="value", expensive_high=True,  source="multpl.com"),
    "cape":        dict(label="S&P500 CAPE",       unit="倍",   group="value", expensive_high=True,  source="multpl.com (Shiller PE)"),

    # --- 金利・インフレ ---
    "dgs10":       dict(label="米10年金利",        unit="%",    group="rate",  expensive_high=None, source="FRED DGS10"),
    "real10":      dict(label="米10年 実質金利",   unit="%",    group="rate",  expensive_high=None, source="FRED DFII10"),
    "bei10":       dict(label="10年 BEI",          unit="%",    group="rate",  expensive_high=None, source="FRED T10YIE"),
    "jp10y":       dict(label="日本10年金利",      unit="%",    group="rate",  expensive_high=None, source="FRED IRLTLT01JPM156N"),
    "us_cpi_yoy":  dict(label="米CPI 前年比",      unit="%",    group="rate",  expensive_high=None, source="FRED CPIAUCSL"),
    "jp_cpi_yoy":  dict(label="日本CPI 前年比",    unit="%",    group="rate",  expensive_high=None, source="FRED JPNCPIALLMINMEI"),

    # --- 比率（ここから下は計算で作る） ---
    "nt":          dict(label="NT倍率",            unit="倍",   group="ratio", expensive_high=None, source="日経平均 ÷ TOPIX"),
    "gsr":         dict(label="金銀比価",          unit="倍",   group="ratio", expensive_high=None, source="金 ÷ 銀"),
    "nikkei_usd":  dict(label="ドル建て日経",      unit="$",    group="ratio", expensive_high=None, source="日経平均 ÷ ドル円"),
    "nikkei_gold": dict(label="日経 ÷ 金",         unit="oz",   group="ratio", expensive_high=True,  source="ドル建て日経 ÷ 金価格"),
    "ys_jp":       dict(label="日本 イールドスプレッド", unit="%", group="spread", expensive_high=False, source="100÷日経PER − 日本10年金利"),
    "ys_us":       dict(label="米国 イールドスプレッド", unit="%", group="spread", expensive_high=False, source="100÷S&P500PER − 米10年金利"),
}


# ---------------------------------------------------------------------------
# 取得先ごとの処理
#   どれも {"YYYY-MM-DD": 値} の辞書を返す
# ---------------------------------------------------------------------------

def fetch_stooq(symbol):
    """Stooq から日次のヒストリカルCSVを取る。"""
    url = ("https://stooq.com/q/d/l/?s=%s&d1=%s&d2=%s&i=d"
           % (quote(symbol), START.strftime("%Y%m%d"), TODAY.strftime("%Y%m%d")))
    r = get(url)
    if r is None:
        return {}
    text = r.text.strip()
    if not text or text.lower().startswith("no data") or "," not in text:
        note("Stooq にデータなし: %s" % symbol)
        return {}
    out = {}
    lines = text.splitlines()
    header = [h.strip().lower() for h in lines[0].split(",")]
    try:
        i_date = header.index("date")
        i_close = header.index("close")
    except ValueError:
        note("Stooq の列が想定と違う: %s (%s)" % (symbol, header))
        return {}
    for line in lines[1:]:
        cells = line.split(",")
        if len(cells) <= max(i_date, i_close):
            continue
        try:
            out[cells[i_date]] = float(cells[i_close])
        except ValueError:
            continue
    return out


def fetch_fred(series_id):
    """FRED から日次/月次のCSVを取る。APIキーは不要。"""
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s" % series_id
    r = get(url)
    if r is None:
        return {}
    out = {}
    lines = r.text.strip().splitlines()
    if len(lines) < 2:
        note("FRED の応答が空: %s" % series_id)
        return {}
    for line in lines[1:]:
        cells = line.split(",")
        if len(cells) < 2:
            continue
        date, val = cells[0].strip(), cells[1].strip()
        if val in (".", "", "NA"):      # FRED は欠測を "." で返す
            continue
        try:
            out[date] = float(val)
        except ValueError:
            continue
    return out


def to_yoy(monthly):
    """月次の指数から前年同月比(%)を作る。CPI 用。"""
    out = {}
    for date, val in monthly.items():
        try:
            y, m, d = (int(x) for x in date.split("-"))
        except ValueError:
            continue
        prev = "%04d-%02d-%02d" % (y - 1, m, d)
        if prev in monthly and monthly[prev]:
            out[date] = round((val / monthly[prev] - 1.0) * 100.0, 2)
    return out


def fetch_nikkei_ratio(kind):
    """
    日経平均プロフィルから PER / PBR を取る。
    表の列は「日付 / 加重平均(倍) / 指数ベース(倍)」。加重平均のほうを使う。
    """
    url = "https://indexes.nikkei.co.jp/nkave/archives/data?list=%s" % kind
    r = get(url)
    if r is None:
        return {}
    r.encoding = r.apparent_encoding or "utf-8"
    html = r.text
    # 「2026.08.21」形式の日付と、その直後に現れる数値2つを拾う
    rows = re.findall(
        r"(\d{4})\.(\d{2})\.(\d{2})\s*</td>\s*<td[^>]*>\s*([\d.]+)\s*</td>\s*<td[^>]*>\s*([\d.]+)",
        html)
    if not rows:
        # タグの形が変わっている場合に備えた、ゆるい拾い方
        rows = re.findall(r"(\d{4})\.(\d{2})\.(\d{2})[^\d]{1,80}?([\d]+\.[\d]+)[^\d]{1,80}?([\d]+\.[\d]+)", html)
    if not rows:
        note("日経%sの表を読めなかった（サイト構造の変更かも）" % kind.upper())
        return {}
    out = {}
    for y, m, d, weighted, _index_based in rows:
        try:
            out["%s-%s-%s" % (y, m, d)] = float(weighted)
        except ValueError:
            continue
    return out


def fetch_multpl(path):
    """multpl.com の月次テーブルを取る（S&P500 PER / CAPE）。"""
    url = "https://www.multpl.com/%s/table/by-month" % path
    r = get(url)
    if r is None:
        return {}
    html = r.text
    # 例: <td>Aug 1, 2026</td><td>29.58</td>
    rows = re.findall(
        r"<td[^>]*>\s*([A-Z][a-z]{2})\s+(\d{1,2}),\s*(\d{4})\s*</td>\s*<td[^>]*>\s*([\d.]+)",
        html)
    if not rows:
        note("multpl の表を読めなかった: %s" % path)
        return {}
    months = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
              "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
    out = {}
    for mon, day, year, val in rows:
        if mon not in months:
            continue
        try:
            out["%s-%02d-%02d" % (year, months[mon], int(day))] = float(val)
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------------------
# 収集本体
# ---------------------------------------------------------------------------

def collect():
    raw = {}

    print("Stooq から価格を取得...")
    for key, sym in [("nikkei", "^nkx"), ("topix", "^tpx"), ("spx", "^spx"),
                     ("gold", "xauusd"), ("silver", "xagusd"), ("usdjpy", "usdjpy")]:
        raw[key] = fetch_stooq(sym)
        print("  %-10s %d 点" % (key, len(raw[key])))
        time.sleep(1.5)          # Stooq に負荷をかけない

    # ドル円が取れなければ FRED で代替する
    if not raw.get("usdjpy"):
        note("Stooq のドル円が取れないので FRED DEXJPUS を使う")
        raw["usdjpy"] = fetch_fred("DEXJPUS")

    print("FRED から金利・インフレを取得...")
    for key, sid in [("dgs10", "DGS10"), ("real10", "DFII10"), ("bei10", "T10YIE"),
                     ("jp10y", "IRLTLT01JPM156N")]:
        raw[key] = fetch_fred(sid)
        print("  %-10s %d 点" % (key, len(raw[key])))

    raw["us_cpi_yoy"] = to_yoy(fetch_fred("CPIAUCSL"))
    raw["jp_cpi_yoy"] = to_yoy(fetch_fred("JPNCPIALLMINMEI"))
    print("  CPI 前年比 (米 %d 点 / 日 %d 点)"
          % (len(raw["us_cpi_yoy"]), len(raw["jp_cpi_yoy"])))

    print("日経平均プロフィルから PER / PBR を取得...")
    raw["nikkei_per"] = fetch_nikkei_ratio("per")
    raw["nikkei_pbr"] = fetch_nikkei_ratio("pbr")
    print("  PER %d 点 / PBR %d 点" % (len(raw["nikkei_per"]), len(raw["nikkei_pbr"])))

    print("multpl から S&P500 の PER / CAPE を取得...")
    raw["spx_per"] = fetch_multpl("s-p-500-pe-ratio")
    time.sleep(1.5)
    raw["cape"] = fetch_multpl("shiller-pe")
    print("  PER %d 点 / CAPE %d 点" % (len(raw["spx_per"]), len(raw["cape"])))

    return raw


# ---------------------------------------------------------------------------
# 既存データとの統合・間引き・派生指標
# ---------------------------------------------------------------------------

def load_previous():
    """前回の JSON を {系列: {日付: 値}} の形に戻す。"""
    if not os.path.exists(OUT_PATH):
        return {}
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            prev = json.load(f)
    except Exception as e:                          # noqa: BLE001
        note("前回の JSON を読めなかった: %s" % e)
        return {}
    dates = prev.get("dates", [])
    out = {}
    for key, values in prev.get("series", {}).items():
        d = {}
        for i, v in enumerate(values):
            if v is not None and i < len(dates):
                d[dates[i]] = v
        out[key] = d
    return out


def merge(previous, fresh):
    """前回値をベースに、今回取れた値で上書きする。取れなかった系列は温存。"""
    merged = {}
    for key in set(list(previous.keys()) + list(fresh.keys())):
        base = dict(previous.get(key, {}))
        new = fresh.get(key, {})
        if not new and base:
            note("%s は今回取得できなかったので前回値を保持" % META.get(key, {}).get("label", key))
        base.update(new)
        # 20年より古いものは捨てる
        cutoff = START.isoformat()
        merged[key] = {d: v for d, v in base.items() if d >= cutoff}
    return merged


def latest_on_or_before(series, target):
    """target 以前でいちばん新しい値を返す（前方補完）。"""
    best = None
    for d in series:
        if d <= target and (best is None or d > best):
            best = d
    return series[best] if best is not None else None


def build_date_axis(all_dates):
    """
    直近3ヶ月=日次 / そこから2年前まで=週次 / 20年前まで=月次 に間引く。
    ファイルを軽くしてスマホで一瞬で開けるようにするための処理。
    """
    dates = sorted(set(all_dates))
    d90 = (TODAY - dt.timedelta(days=90)).isoformat()
    d2y = (TODAY - dt.timedelta(days=730)).isoformat()

    keep = set()
    seen_week, seen_month = set(), set()
    for d in dates:
        if d >= d90:
            keep.add(d)                              # 日次
        elif d >= d2y:
            y, w, _ = dt.date.fromisoformat(d).isocalendar()
            if (y, w) not in seen_week:              # その週の最初の営業日
                seen_week.add((y, w))
                keep.add(d)
        else:
            ym = d[:7]
            if ym not in seen_month:
                seen_month.add(ym)
                keep.add(d)
    return sorted(keep)


def add_derived(table, dates):
    """比率とイールドスプレッドを計算して足す。"""
    def col(key):
        return table.get(key, {})

    nikkei, topix, spx = col("nikkei"), col("topix"), col("spx")
    gold, silver, usdjpy = col("gold"), col("silver"), col("usdjpy")
    nper, sper = col("nikkei_per"), col("spx_per")
    jp10y, dgs10 = col("jp10y"), col("dgs10")

    derived = {k: {} for k in
               ("nt", "gsr", "nikkei_usd", "nikkei_gold", "ys_jp", "ys_us")}

    for d in dates:
        nk = latest_on_or_before(nikkei, d)
        tp = latest_on_or_before(topix, d)
        gd = latest_on_or_before(gold, d)
        sv = latest_on_or_before(silver, d)
        fx = latest_on_or_before(usdjpy, d)
        np_ = latest_on_or_before(nper, d)
        sp_ = latest_on_or_before(sper, d)
        j10 = latest_on_or_before(jp10y, d)
        u10 = latest_on_or_before(dgs10, d)

        if nk and tp:
            derived["nt"][d] = round(nk / tp, 3)
        if gd and sv:
            derived["gsr"][d] = round(gd / sv, 2)
        nk_usd = None
        if nk and fx:
            nk_usd = nk / fx
            derived["nikkei_usd"][d] = round(nk_usd, 2)
        if nk_usd and gd:
            derived["nikkei_gold"][d] = round(nk_usd / gd, 4)
        if np_ and j10 is not None:
            derived["ys_jp"][d] = round(100.0 / np_ - j10, 2)
        if sp_ and u10 is not None:
            derived["ys_us"][d] = round(100.0 / sp_ - u10, 2)

    table.update(derived)
    return table


def main():
    fresh = collect()
    previous = load_previous()
    table = merge(previous, fresh)

    # 日付軸は「価格系列に実際に値がある日」を土台にする
    axis_source = []
    for key in ("nikkei", "spx", "gold", "usdjpy"):
        axis_source += list(table.get(key, {}).keys())
    if not axis_source:
        # 価格が全滅した場合でも、なにかしら軸を作る
        for values in table.values():
            axis_source += list(values.keys())
    if not axis_source:
        print("データがまったく取れませんでした。既存ファイルを保持して終了します。")
        return 1

    dates = build_date_axis(axis_source)
    table = add_derived(table, dates)

    # 出力を組み立てる（各系列は日付軸に前方補完して並べる）
    series, asof = {}, {}
    for key in META:
        col = table.get(key, {})
        if not col:
            series[key] = [None] * len(dates)
            asof[key] = None
            continue
        series[key] = [latest_on_or_before(col, d) for d in dates]
        asof[key] = max(col.keys())

    payload = {
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "years": YEARS,
        "dates": dates,
        "series": series,
        "asof": asof,
        "meta": META,
        "notes": NOTES,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

    size = os.path.getsize(OUT_PATH) / 1024.0
    filled = sum(1 for k in META if asof.get(k))
    print("\n書き出し完了: %s" % OUT_PATH)
    print("  日付 %d 点 / 系列 %d 本中 %d 本にデータあり / %.0f KB"
          % (len(dates), len(META), filled, size))
    if NOTES:
        print("  気になった点 %d 件（JSON の notes に記録）" % len(NOTES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
