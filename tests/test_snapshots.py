"""Veritabani ve snapshot sistemi testleri.

Snapshot sistemi bu uygulamanin "gecmis oran" iddiasinin TEK kaynagidir;
bu yuzden kaydedilmemis bir anin uydurulmadigi burada test edilir.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_module  # noqa: E402
from database import Database, from_iso, to_iso, utcnow  # noqa: E402
from odds_parser import MARKET_1X2  # noqa: E402
from snapshots import (  # noqa: E402
    capture_many,
    capture_snapshot,
    load_snapshots,
    movement_for,
    snapshot_counts,
    snapshots_bulk,
)


def entry(fixture_id: int, home: float, draw: float, away: float) -> dict:
    return {
        "fixture_id": fixture_id,
        "bookmaker_id": 8,
        "bookmaker_name": "Bet365",
        "updated_at": "2026-09-07T09:00:00+00:00",
        "kickoff": "2026-09-08T18:00:00+00:00",
        "markets": {
            MARKET_1X2: {"1": home, "X": draw, "2": away},
            "kg": {"var": 1.72, "yok": 2.05},
        },
    }


class SnapshotCase(unittest.TestCase):
    #: Bu modulun dokundugu tum ortam degiskenleri - test sonrasi geri alinir.
    ENV_KEYS = ("SQLITE_PATH", "DATABASE_URL", "SNAPSHOT_ENABLED", "SNAPSHOT_MIN_GAP_MINUTES")

    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self._saved_env = {key: os.environ.get(key) for key in self.ENV_KEYS}
        os.environ["SQLITE_PATH"] = self.tmp.name
        os.environ.pop("DATABASE_URL", None)
        # ACIKCA set ediliyor: disaridan gelen SNAPSHOT_ENABLED=false sizintisi
        # bu testleri sessizce dusuruyordu.
        os.environ["SNAPSHOT_ENABLED"] = "true"
        os.environ["SNAPSHOT_MIN_GAP_MINUTES"] = "30"
        self.settings = config_module.get_settings(refresh=True)
        self.db = Database(self.settings)
        self.db.init_schema()

    def tearDown(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config_module.get_settings(refresh=True)
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass


class TestDatabaseBasics(SnapshotCase):
    def test_sqlite_is_reported_as_not_persistent(self) -> None:
        self.assertEqual(self.db.dialect, "sqlite")
        self.assertFalse(self.db.persistent)
        self.assertIn("sqlite", self.db.describe())

    def test_cache_expiry_is_honoured(self) -> None:
        self.db.cache_set("a", {"v": 1}, utcnow() + timedelta(seconds=30))
        self.assertEqual(self.db.cache_get("a"), {"v": 1})
        self.db.cache_set("b", {"v": 2}, utcnow() - timedelta(seconds=1))
        self.assertIsNone(self.db.cache_get("b"))

    def test_cache_purge_removes_only_expired(self) -> None:
        self.db.cache_set("live", {"v": 1}, utcnow() + timedelta(minutes=5))
        self.db.cache_set("dead", {"v": 2}, utcnow() - timedelta(minutes=5))
        self.db.cache_purge_expired()
        self.assertIsNotNone(self.db.cache_get("live"))
        self.assertEqual(len(self.db.query("SELECT * FROM api_cache")), 1)

    def test_upsert_does_not_duplicate(self) -> None:
        self.db.upsert_team(10, "Team A", None)
        self.db.upsert_team(10, "Team A Renamed", "logo.png")
        rows = self.db.query("SELECT * FROM teams")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Team A Renamed")

    def test_iso_round_trip_keeps_utc(self) -> None:
        moment = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(from_iso(to_iso(moment)), moment)

    def test_from_iso_handles_garbage(self) -> None:
        self.assertIsNone(from_iso("not-a-date"))
        self.assertIsNone(from_iso(None))


class TestSnapshotWriting(SnapshotCase):
    def test_first_capture_is_written(self) -> None:
        self.assertTrue(capture_snapshot(self.db, entry(1, 2.40, 3.30, 3.00), settings=self.settings))
        self.assertEqual(len(load_snapshots(self.db, 1)), 1)

    def test_identical_odds_within_gap_are_not_rewritten(self) -> None:
        capture_snapshot(self.db, entry(1, 2.40, 3.30, 3.00), settings=self.settings)
        written = capture_snapshot(self.db, entry(1, 2.40, 3.30, 3.00), settings=self.settings)
        self.assertFalse(written)
        self.assertEqual(len(load_snapshots(self.db, 1)), 1)

    def test_changed_odds_are_always_written(self) -> None:
        capture_snapshot(self.db, entry(1, 2.40, 3.30, 3.00), settings=self.settings)
        self.assertTrue(capture_snapshot(self.db, entry(1, 2.15, 3.40, 3.30), settings=self.settings))
        self.assertEqual(len(load_snapshots(self.db, 1)), 2)

    def test_entry_without_1x2_is_ignored(self) -> None:
        broken = entry(1, 2.4, 3.3, 3.0)
        broken["markets"].pop(MARKET_1X2)
        self.assertFalse(capture_snapshot(self.db, broken, settings=self.settings))
        self.assertEqual(load_snapshots(self.db, 1), [])

    def test_extra_markets_are_stored_alongside(self) -> None:
        capture_snapshot(self.db, entry(1, 2.40, 3.30, 3.00), settings=self.settings)
        row = load_snapshots(self.db, 1)[0]
        self.assertIn("kg", row["extra"])

    def test_provenance_is_recorded(self) -> None:
        capture_snapshot(self.db, entry(1, 2.40, 3.30, 3.00), settings=self.settings)
        row = load_snapshots(self.db, 1)[0]
        self.assertEqual(row["bookmaker_name"], "Bet365")
        self.assertEqual(row["bookmaker_id"], 8)
        self.assertTrue(row["source"])
        self.assertIsNotNone(row["captured_at"])

    def test_capture_many_counts_writes(self) -> None:
        written = capture_many(
            self.db,
            {1: entry(1, 2.4, 3.3, 3.0), 2: entry(2, 1.8, 3.6, 4.2)},
            settings=self.settings,
        )
        self.assertEqual(written, 2)

    def test_capture_many_respects_disabled_flag(self) -> None:
        os.environ["SNAPSHOT_ENABLED"] = "false"
        try:
            settings = config_module.get_settings(refresh=True)
            written = capture_many(self.db, {1: entry(1, 2.4, 3.3, 3.0)}, settings=settings)
            self.assertEqual(written, 0)
        finally:
            # tearDown zaten geri aliyor; yine de test ici durumu temiz birak.
            os.environ["SNAPSHOT_ENABLED"] = "true"
            config_module.get_settings(refresh=True)


class TestMovement(SnapshotCase):
    def _write(self, fixture_id: int, hours_ago: int, home: float, draw: float, away: float) -> None:
        self.db.insert_snapshot(
            {
                "fixture_id": fixture_id,
                "bookmaker_id": 8,
                "bookmaker_name": "Bet365",
                "market": MARKET_1X2,
                "home_odds": home,
                "draw_odds": draw,
                "away_odds": away,
                "captured_at": to_iso(utcnow() - timedelta(hours=hours_ago)),
                "kickoff_utc": to_iso(utcnow() + timedelta(hours=3)),
                "source": "test",
            }
        )

    def test_no_snapshot_means_no_movement(self) -> None:
        movement = movement_for(self.db, 42, None)
        self.assertFalse(movement["available"])
        self.assertEqual(movement["snapshot_count"], 0)
        self.assertIn("bulunmuyor", movement["note"])

    def test_two_snapshots_produce_real_change(self) -> None:
        self._write(1, 9, 2.40, 3.30, 3.00)
        self._write(1, 1, 2.15, 3.40, 3.30)
        movement = movement_for(self.db, 1, utcnow() + timedelta(hours=3))
        self.assertTrue(movement["available"])
        self.assertAlmostEqual(movement["change_pct"]["1"], -10.42, places=2)
        self.assertEqual(movement["first_recorded"]["label"], "Ilk kaydedilen")
        self.assertEqual(movement["latest"]["label"], "Guncel")

    def test_closest_to_kickoff_ignores_post_kickoff_records(self) -> None:
        kickoff = utcnow() + timedelta(hours=3)
        self._write(1, 9, 2.40, 3.30, 3.00)
        self._write(1, 1, 2.15, 3.40, 3.30)
        # Baslama saatinden SONRA alinmis bir kayit
        self.db.insert_snapshot(
            {
                "fixture_id": 1, "bookmaker_id": 8, "bookmaker_name": "Bet365",
                "market": MARKET_1X2, "home_odds": 9.9, "draw_odds": 9.9, "away_odds": 9.9,
                "captured_at": to_iso(kickoff + timedelta(hours=1)),
                "kickoff_utc": to_iso(kickoff), "source": "test",
            }
        )
        movement = movement_for(self.db, 1, kickoff)
        self.assertNotIn(9.9, movement["closest_to_kickoff"]["values"].values())

    def test_bulk_helpers(self) -> None:
        self._write(1, 5, 2.4, 3.3, 3.0)
        self._write(1, 1, 2.2, 3.3, 3.1)
        self._write(2, 1, 1.8, 3.6, 4.2)
        counts = snapshot_counts(self.db, [1, 2, 3])
        self.assertEqual(counts[1], 2)
        self.assertEqual(counts[2], 1)
        self.assertNotIn(3, counts)
        grouped = snapshots_bulk(self.db, [1, 2])
        self.assertEqual(len(grouped[1]), 2)
        self.assertEqual(len(grouped[2]), 1)

    def test_bulk_helpers_handle_empty_input(self) -> None:
        self.assertEqual(snapshot_counts(self.db, []), {})
        self.assertEqual(snapshots_bulk(self.db, []), {})


class TestCalibrationLog(SnapshotCase):
    def test_prediction_can_be_logged_and_settled(self) -> None:
        self.db.log_prediction(
            {
                "fixture_id": 1, "model_home": 0.6, "model_draw": 0.2, "model_away": 0.2,
                "market_home": 0.55, "market_draw": 0.25, "market_away": 0.20,
                "confidence": 70, "data_quality": 84,
            }
        )
        self.assertEqual(self.db.calibration_rows(), [])  # henuz sonuclanmadi
        self.db.settle_prediction(1, 2, 1)
        rows = self.db.calibration_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actual_home_goals"], 2)

    def test_settling_twice_does_not_duplicate(self) -> None:
        self.db.log_prediction({"fixture_id": 1, "model_home": 0.5})
        self.db.settle_prediction(1, 1, 0)
        self.db.settle_prediction(1, 3, 3)
        rows = self.db.calibration_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actual_home_goals"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
