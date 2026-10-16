"""Tes perekam data posisi dengan getter palsu (tanpa jaringan)."""
import pytest

from smcbot import recorder as rec

T0 = 1_792_000_500_000  # sembarang; dibulatkan ke 15m oleh floor15


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(rec, "PACE_S", 0)


@pytest.fixture
def db():
    d = rec.connect(":memory:")
    yield d
    d.close()


def fake_get(extra_points=0, fail=None):
    """Getter palsu: tiap endpoint mengembalikan titik sesuai bentuk respons Binance."""
    calls = []

    def get(path, params):
        calls.append((path, dict(params)))
        if fail and fail(path, params):
            raise RuntimeError(fail(path, params))
        if path == "/fapi/v1/premiumIndex":
            return [{"symbol": "AAAUSDT", "markPrice": "10.5", "indexPrice": "10.4",
                     "lastFundingRate": "-0.0001", "nextFundingTime": 123},
                    {"symbol": "ZZZUSDT", "markPrice": "1", "indexPrice": "1", "lastFundingRate": "0", "nextFundingTime": 1}]
        ts = rec.floor15(T0)
        pts = []
        for k in range(1 + extra_points):
            t = ts - k * rec.PERIOD_MS
            if path.endswith("openInterestHist"):
                pts.append({"timestamp": t, "sumOpenInterest": "100", "sumOpenInterestValue": "2000"})
            elif path.endswith("takerlongshortRatio"):
                pts.append({"timestamp": t, "buySellRatio": "1.2", "buyVol": "60", "sellVol": "50"})
            else:
                pts.append({"timestamp": t, "longAccount": "0.6", "shortAccount": "0.4", "longShortRatio": "1.5"})
        return pts

    get.calls = calls
    return get


def test_floor15():
    assert rec.floor15(900_000 * 7 + 123) == 900_000 * 7
    assert rec.floor15(900_000 * 7) == 900_000 * 7


def test_available_ms_taker_is_known_only_after_candle_closes():
    ts = rec.floor15(T0)
    assert rec.available_ms(ts, "taker_ratio") == ts + rec.PERIOD_MS
    assert rec.available_ms(ts, "taker_buy") == ts + rec.PERIOD_MS
    for col in ("oi", "oi_value", "g_long", "g_ratio", "ta_long", "tp_ratio"):
        assert rec.available_ms(ts, col) == ts


def test_cycle_writes_all_five_sources_into_one_row(db):
    res = rec.run_cycle(db, fake_get(), ["AAAUSDT"], now_ms=T0)
    assert res["calls"] == 5 and res["errors"] == 0 and res["aborted"] is None
    row = db.execute("SELECT oi, oi_value, g_long, g_ratio, ta_long, tp_ratio, taker_buy, taker_sell, taker_ratio "
                     "FROM snap WHERE symbol='AAAUSDT'").fetchone()
    assert row == (100.0, 2000.0, 0.6, 1.5, 0.6, 1.5, 60.0, 50.0, 1.2)
    assert db.execute("SELECT COUNT(*) FROM snap").fetchone()[0] == 1  # semua endpoint ke satu baris


def test_mark_only_recorded_for_tracked_symbols(db):
    rec.run_cycle(db, fake_get(), ["AAAUSDT"], now_ms=T0)
    rows = db.execute("SELECT symbol, mark_price, funding_rate FROM mark").fetchall()
    assert rows == [("AAAUSDT", 10.5, -0.0001)]


def test_rerun_is_idempotent_and_heals_gaps(db):
    get = fake_get(extra_points=3)
    rec.run_cycle(db, get, ["AAAUSDT"], now_ms=T0)
    rec.run_cycle(db, get, ["AAAUSDT"], now_ms=T0)  # ulang: tidak menggandakan baris
    assert db.execute("SELECT COUNT(*) FROM snap").fetchone()[0] == 4
    # siklus berikutnya melihat titik lama -> baris yang hilang di tengah terisi lagi
    db.execute("DELETE FROM snap WHERE ts = ?", (rec.floor15(T0) - rec.PERIOD_MS,))
    rec.run_cycle(db, get, ["AAAUSDT"], now_ms=T0)
    assert db.execute("SELECT COUNT(*) FROM snap").fetchone()[0] == 4


def test_single_failed_call_does_not_abort_cycle(db):
    get = fake_get(fail=lambda p, _: "boom" if p.endswith("topLongShortAccountRatio") else None)
    res = rec.run_cycle(db, get, ["AAAUSDT", "BBBUSDT"], now_ms=T0)
    assert res["errors"] == 2 and res["aborted"] is None
    row = db.execute("SELECT ta_long, oi FROM snap WHERE symbol='BBBUSDT'").fetchone()
    assert row == (None, 100.0)  # kolom endpoint yang gagal NULL, yang lain tetap terisi


def test_ip_block_418_aborts_cycle_and_is_logged(db):
    get = fake_get(fail=lambda p, prm: "Binance memblokir IP sementara (418)" if prm.get("symbol") == "BBBUSDT" else None)
    res = rec.run_cycle(db, get, ["AAAUSDT", "BBBUSDT", "CCCUSDT"], now_ms=T0)
    assert res["aborted"] and "418" in res["aborted"]
    assert db.execute("SELECT COUNT(*) FROM snap WHERE symbol='CCCUSDT'").fetchone()[0] == 0  # berhenti, tidak menekan
    assert db.execute("SELECT aborted FROM runs").fetchone()[0] is not None


def test_malformed_point_is_skipped_not_fatal(db):
    def get(path, params):
        if path == "/fapi/v1/premiumIndex":
            return []
        return [{"timestamp": 900_000, "sumOpenInterest": "abc"}, {"timestamp": 1_800_000, "sumOpenInterest": "5",
                                                                    "sumOpenInterestValue": "50"}]
    rec.run_cycle(db, get, ["AAAUSDT"], now_ms=T0)
    assert db.execute("SELECT COUNT(*) FROM snap WHERE oi IS NOT NULL").fetchone()[0] == 1


def test_status_reports_completeness_and_runs(db):
    assert rec.status(db) == "DB kosong."
    rec.run_cycle(db, fake_get(), ["AAAUSDT"], now_ms=T0)
    out = rec.status(db)
    assert "1 pair" in out and "100.0%" in out and "runs[cycle]: 1 siklus" in out


def test_backfill_pages_with_start_and_end_time(db, monkeypatch):
    get = fake_get()
    rec.backfill(db, get, ["AAAUSDT"], days=12)       # 288 jam / 125 jam per halaman (500 titik x 15m) = 3 halaman
    oi_calls = [p for path, p in get.calls if path.endswith("openInterestHist")]
    assert len(oi_calls) == 3
    assert all("startTime" in p and p["limit"] == rec.PAGE for p in oi_calls)
    starts = [p["startTime"] for p in oi_calls]
    ends = [p["endTime"] for p in oi_calls]
    assert starts == sorted(starts) and all(s > e_prev for s, e_prev in zip(starts[1:], ends))  # tanpa tumpang tindih
    assert db.execute("SELECT kind FROM runs").fetchone()[0] == "backfill"
