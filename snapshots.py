"""Oran snapshot sistemi.

API-Football pre-match oranlarini yaklasik 7 gunluk bir pencerede sunar ve
~3 saatte bir gunceller. Yani "acilis orani" veya "maca 15 dakika kala oran"
diye sorgulanabilecek bir gecmis YOKTUR.

Bu yuzden uygulama kendi gecmisini olusturur: her veri yenilemesinde gorulen
Bet365 orani zaman damgasiyla kaydedilir. Kaydedilmemis bir an icin oran
URETILMEZ; o an icin "snapshot yok" denir.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from analysis_engine import calculate_odds_movement
from config import Settings, get_settings
from database import Database, from_iso, to_iso, utcnow
from errors import AppError
from odds_parser import MARKET_1X2

logger = logging.getLogger(__name__)

SOURCE = "api-football/odds"


def _changed(previous: Mapping[str, Any] | None, values: Mapping[str, float]) -> bool:
    if previous is None:
        return True
    for key, column in (("1", "home_odds"), ("X", "draw_odds"), ("2", "away_odds")):
        old = previous.get(column)
        new = values.get(key)
        if old is None and new is None:
            continue
        if old is None or new is None:
            return True
        if abs(float(old) - float(new)) > 1e-9:
            return True
    return False


def _too_soon(previous: Mapping[str, Any] | None, min_gap_minutes: int) -> bool:
    if previous is None:
        return False
    captured = from_iso(previous.get("captured_at"))
    if captured is None:
        return False
    return utcnow() - captured < timedelta(minutes=min_gap_minutes)


def capture_snapshot(
    database: Database,
    entry: Mapping[str, Any],
    *,
    kickoff_utc: str | None = None,
    settings: Settings | None = None,
) -> bool:
    """Bir mac icin 1X2 snapshot'i kaydeder.

    Yalnizca oran DEGISTIYSE veya son kayittan bu yana yeterli sure gectiyse
    yazar; boylece tablo gereksiz sisirilmez.

    Returns:
        Kayit yazildiysa True.
    """
    settings = settings or get_settings()
    values = (entry.get("markets") or {}).get(MARKET_1X2)
    if not values or len(values) != 3:
        return False

    fixture_id = int(entry["fixture_id"])
    try:
        previous = database.latest_snapshot(fixture_id, MARKET_1X2)
    except AppError:  # pragma: no cover
        previous = None

    changed = _changed(previous, values)
    if not changed and _too_soon(previous, settings.snapshot_min_gap_minutes):
        return False
    if not changed and previous is not None:
        # Oran ayni ama arada yeterli zaman gecmis: yine de kaydet ki
        # "sabit kaldi" bilgisi de gercek veriyle desteklensin.
        pass

    try:
        database.insert_snapshot(
            {
                "fixture_id": fixture_id,
                "bookmaker_id": entry["bookmaker_id"],
                "bookmaker_name": entry["bookmaker_name"],
                "market": MARKET_1X2,
                "home_odds": values.get("1"),
                "draw_odds": values.get("X"),
                "away_odds": values.get("2"),
                "extra": {
                    key: market
                    for key, market in (entry.get("markets") or {}).items()
                    if key != MARKET_1X2
                },
                "captured_at": to_iso(utcnow()),
                "kickoff_utc": kickoff_utc or entry.get("kickoff"),
                "source": SOURCE,
            }
        )
        return True
    except AppError:
        logger.warning("Snapshot yazilamadi: fixture=%s", fixture_id)
        return False


def capture_many(
    database: Database,
    entries: Mapping[int, Mapping[str, Any]],
    kickoffs: Mapping[int, str | None] | None = None,
    settings: Settings | None = None,
) -> int:
    """Toplu snapshot kaydi. Yazilan kayit sayisini dondurur."""
    settings = settings or get_settings()
    if not settings.snapshot_enabled:
        return 0
    written = 0
    for fixture_id, entry in entries.items():
        kickoff = (kickoffs or {}).get(fixture_id)
        if capture_snapshot(database, entry, kickoff_utc=kickoff, settings=settings):
            written += 1
    if written:
        logger.info("%s yeni oran snapshot'i kaydedildi", written)
    return written


def load_snapshots(
    database: Database, fixture_id: int, market: str = MARKET_1X2
) -> list[dict[str, Any]]:
    try:
        return database.snapshots_for(fixture_id, market)
    except AppError:  # pragma: no cover
        return []


def movement_for(
    database: Database, fixture_id: int, kickoff: datetime | None
) -> dict[str, Any]:
    """Kaydedilmis snapshot'lardan oran hareketi uretir."""
    return calculate_odds_movement(load_snapshots(database, fixture_id), kickoff)


def snapshot_counts(database: Database, fixture_ids: Sequence[int]) -> dict[int, int]:
    """Birden cok mac icin snapshot sayisini tek sorguda getirir."""
    if not fixture_ids:
        return {}
    unique = sorted({int(value) for value in fixture_ids})
    placeholders = ", ".join("?" for _ in unique)
    try:
        rows = database.query(
            f"SELECT fixture_id, COUNT(*) AS total FROM odds_snapshots "
            f"WHERE market = ? AND fixture_id IN ({placeholders}) GROUP BY fixture_id",
            [MARKET_1X2, *unique],
        )
    except AppError:  # pragma: no cover
        return {}
    return {int(row["fixture_id"]): int(row["total"]) for row in rows}


def snapshots_bulk(
    database: Database, fixture_ids: Sequence[int]
) -> dict[int, list[dict[str, Any]]]:
    """Birden cok mac icin tum snapshot'lari tek sorguda getirir."""
    if not fixture_ids:
        return {}
    unique = sorted({int(value) for value in fixture_ids})
    placeholders = ", ".join("?" for _ in unique)
    try:
        rows = database.query(
            f"SELECT * FROM odds_snapshots WHERE market = ? AND fixture_id IN ({placeholders}) "
            f"ORDER BY fixture_id, captured_at ASC",
            [MARKET_1X2, *unique],
        )
    except AppError:  # pragma: no cover
        return {}

    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(int(row["fixture_id"]), []).append(row)
    return grouped


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)
