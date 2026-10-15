"""Tes detektor SMC dengan candle sintetis (deterministik, tanpa internet)."""
import pandas as pd

from smcbot.smc import (
    Zone, find_long_sweep, htf_bias, latest_bullish_fvg, latest_bullish_ob,
    price_in_zone, swing_points,
)


def make_df(rows):
    """rows: list of (open, high, low, close)."""
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["volume"] = 1.0
    return df


def zigzag(prices, spread=0.5):
    """Bangun candle dari rangkaian harga penutupan; high/low = close +- spread."""
    # high/low mengikuti close agar pivot unik (tanpa high/low kembar antar candle)
    rows, prev = [], prices[0]
    for p in prices:
        hi, lo = p + spread, p - spread
        rows.append((min(max(prev, lo), hi), hi, lo, p))
        prev = p
    return make_df(rows)


def test_swing_points_detects_pivots_with_confirmation_delay():
    df = zigzag([10, 11, 12, 13, 12, 11, 10, 11, 12, 13, 14, 15, 16, 17])
    sw = swing_points(df, left=3, right=3)
    highs = sw[sw["kind"] == "high"]
    assert 3 in highs["idx"].values
    row = highs[highs["idx"] == 3].iloc[0]
    assert row["confirmed_at"] == 6  # idx + right: tidak ada lookahead


def test_htf_bias_bullish_on_higher_highs_and_lows():
    prices = [10, 12, 14, 12, 10, 12, 15, 18, 15, 12, 14, 17, 20, 17, 14, 16, 19, 22, 25]
    assert htf_bias(zigzag(prices)) == 1


def test_htf_bias_bearish_on_lower_highs_and_lows():
    prices = [25, 22, 19, 22, 25, 22, 18, 15, 18, 21, 18, 14, 11, 14, 17, 14, 10, 7, 4]
    assert htf_bias(zigzag(prices)) == -1


def _sweep_scenario(final_close):
    """Swing low 94.5 terbentuk (idx 6), lalu candle sapuan menembus, lalu candle terakhir."""
    prices = [100, 98, 96, 95, 96, 98, 100, 102, 104, 103, 102, 101, 102, 103, 104,
              103, 102, 103, 104, 105, 104, 103]
    df = zigzag(prices)
    swing_low = df["low"].min()  # level likuiditas
    rows = df[["open", "high", "low", "close"]].values.tolist()
    # candle sapuan: tembus di bawah level, tutup kembali tipis di atas
    rows.append((103, 103.5, swing_low - 1.0, swing_low - 0.5))
    rows.append((swing_low - 0.5, final_close + 0.5, swing_low - 1.2, final_close))
    return make_df(rows), swing_low


def test_sweep_detected_when_last_candle_reclaims_level():
    df, level = _sweep_scenario(final_close=None or 97.0)
    sw = find_long_sweep(df)
    assert sw is not None
    assert abs(sw.level - level) < 1e-9
    assert sw.sweep_low < sw.level
    assert sw.reclaim_idx == len(df) - 1
    assert sw.sweep_low < level  # dasar SL berada di bawah sapuan


def test_no_sweep_when_close_stays_below_level():
    df, level = _sweep_scenario(final_close=None or 93.0)
    assert find_long_sweep(df) is None


def test_no_stale_sweep_after_reclaim_already_happened():
    df, level = _sweep_scenario(final_close=97.0)
    rows = df[["open", "high", "low", "close"]].values.tolist()
    rows.append((97.0, 99.0, 96.5, 98.5))  # candle lanjutan: reclaim sudah lewat
    assert find_long_sweep(make_df(rows)) is None


def test_bullish_fvg_found_and_mitigation_removes_it():
    rows = [(10, 10.5, 9.5, 10)] * 3 + [
        (10, 10.5, 9.8, 10.2),   # i-2: high 10.5
        (10.2, 12.5, 10.1, 12),  # impuls
        (12, 13, 11.5, 12.8),    # i: low 11.5 > 10.5 -> gap 10.5..11.5
        (12.8, 13.5, 12.0, 13),  # low 12.0 < high impuls 12.5 -> tidak ada gap kedua
    ]
    zone = latest_bullish_fvg(make_df(rows))
    assert zone is not None and zone.kind == "FVG"
    assert (zone.bottom, zone.top) == (10.5, 11.5)

    rows.append((13, 13.1, 9.9, 10.0))  # harga menembus gap sepenuhnya
    assert latest_bullish_fvg(make_df(rows)) is None


def test_bullish_ob_is_last_bearish_candle_before_displacement():
    rows = [(10 + (i % 2) * 0.2, 10.6, 9.6, 10.1 + (i % 2) * 0.1) for i in range(20)]
    rows += [
        (10.2, 10.3, 9.4, 9.5),   # bearish terakhir -> kandidat OB
        (9.5, 13.5, 9.4, 13.2),   # displacement besar menembus high sebelumnya
        (13.2, 13.8, 13.0, 13.6),
    ]
    zone = latest_bullish_ob(make_df(rows))
    assert zone is not None and zone.kind == "OB"
    assert zone.idx == 20 and (zone.bottom, zone.top) == (9.4, 10.3)


def test_price_in_zone():
    z = Zone("OB", 9.4, 10.3, 20)
    assert price_in_zone(z, low=10.0, close=10.8)
    assert not price_in_zone(z, low=11.0, close=11.5)   # belum menyentuh
    assert not price_in_zone(z, low=9.0, close=9.2)     # menutup di bawah zona
    assert not price_in_zone(None, 1, 1)
