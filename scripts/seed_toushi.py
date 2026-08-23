#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data/toushi.json の初期値を作る（1回だけ使う）。

GitHub Actions が初めて走るまでの間、ダッシュボードが空にならないように
2026-08-21 時点の実測値を1点だけ入れておく。
Actions が動けば20年分の履歴で上書きされる。
"""

import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collect_toushi import META, OUT_PATH        # noqa: E402

D = "2026-08-21"

VALUES = {
    "nikkei":     (66016.36, D),
    "topix":      (4067.29,  D),
    "spx":        (7674.37,  D),
    "gold":       (4607.35,  D),
    "silver":     (68.95,    D),
    "usdjpy":     (158.985,  D),
    "nikkei_per": (17.66,    "2026-08-17"),
    "nikkei_pbr": (1.87,     D),
    "spx_per":    (29.58,    D),
    "cape":       (41.96,    D),
    "dgs10":      (4.69,     "2026-08-20"),
    "real10":     (2.35,     "2026-08-20"),
    "bei10":      (2.34,     D),
    "jp10y":      (2.67,     "2026-06-01"),
    "us_cpi_yoy": (None,     None),
    "jp_cpi_yoy": (None,     None),
}

nk, tp = VALUES["nikkei"][0], VALUES["topix"][0]
gd, sv = VALUES["gold"][0], VALUES["silver"][0]
fx = VALUES["usdjpy"][0]
nper, sper = VALUES["nikkei_per"][0], VALUES["spx_per"][0]
j10, u10 = VALUES["jp10y"][0], VALUES["dgs10"][0]
nk_usd = nk / fx

VALUES.update({
    "nt":          (round(nk / tp, 3),          D),
    "gsr":         (round(gd / sv, 2),          D),
    "nikkei_usd":  (round(nk_usd, 2),           D),
    "nikkei_gold": (round(nk_usd / gd, 4),      D),
    "ys_jp":       (round(100 / nper - j10, 2), D),
    "ys_us":       (round(100 / sper - u10, 2), D),
})

payload = {
    "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    "years": 20,
    "dates": [D],
    "series": {k: [VALUES.get(k, (None, None))[0]] for k in META},
    "asof":   {k: VALUES.get(k, (None, None))[1] for k in META},
    "meta": META,
    "notes": ["初期値。GitHub Actions が初回実行されると20年分の履歴に置き換わります。"],
}

os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
with open(OUT_PATH, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
print("書き出し:", OUT_PATH, "%.1f KB" % (os.path.getsize(OUT_PATH) / 1024))
