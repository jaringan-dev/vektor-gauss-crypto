"""Tes logika analisis kalibrasi (dedupe & analyze) dengan CSV sintetis."""
import numpy as np
import pandas as pd

from smcbot import calibrate
from smcbot.calibrate import dedupe_non_overlapping


def _row(sym, minute, exit_minute, r=1.0, score=50.0):
    t0 = pd.Timestamp("2026-01-01", tz="UTC")
    return {"symbol": sym, "time": t0 + pd.Timedelta(minutes=minute),
            "exit_time": t0 + pd.Timedelta(minutes=exit_minute), "r": r, "score": score}


def test_dedupe_keeps_one_open_position_per_symbol():
    df = pd.DataFrame([_row("A", 0, 60), _row("A", 15, 45), _row("B", 15, 45), _row("A", 60, 90)])
    kept = dedupe_non_overlapping(df)
    # A@0 (sampai 60), A@15 ditolak (masih berjalan), B@15 diterima, A@60 diterima (exit == time => bebas)
    assert sorted(zip(kept["symbol"], kept["time"].dt.minute)) == [("A", 0), ("A", 0), ("B", 15)]
    assert sorted(kept[kept["symbol"] == "A"]["time"].dt.hour) == [0, 1]


def test_analyze_runs_end_to_end_on_synthetic_csv(tmp_path, monkeypatch, capsys):
    rng = np.random.default_rng(0)
    n = 400
    t0 = pd.Timestamp("2026-01-01", tz="UTC")
    times = t0 + pd.to_timedelta(np.arange(n) * 6, unit="h")
    df = pd.DataFrame({
        "symbol": rng.choice(list("ABCDEFGH"), n), "idx": np.arange(n), "time": times,
        "exit_time": times + pd.Timedelta(hours=1), "score": rng.uniform(35, 65, n),
        "s_sweep": 10.0, "s_zona": 10.0, "s_flow": 10.0, "wick": rng.uniform(0, 1, n),
        "depth_atr": rng.uniform(0.3, 2, n), "zone_tf": rng.choice(["1h", "15m"], n),
        "zone_kind": "OB", "b1": 1, "b4": rng.choice([0, 1], n),
        "cvd_hl": rng.random(n) > 0.5, "delta_pos": rng.random(n) > 0.5, "buy3": rng.uniform(0.3, 0.8, n),
        "risk_pct": 1.0, "net_rr": 2.0, "outcome": "SL", "r": rng.normal(-0.1, 1.0, n),
    })
    f = tmp_path / "candidates.csv"
    df.to_csv(f, index=False)
    monkeypatch.setattr(calibrate, "CSV", f)
    calibrate.analyze(min_train=20)
    out = capsys.readouterr().out
    assert "Spearman" in out and "UJI" in out and "ambang terbaik" in out
    assert "6. VONIS" in out and ("VONIS:" in out or "TIDAK ADA KESIMPULAN" in out)
