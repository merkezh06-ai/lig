"""Periyodik oran snapshot toplayici.

Neden gerekli: snapshot'lar yalnizca kullanici "Verileri Guncelle" dedigi
anda alinsaydi gecmis seyrek ve yanli olurdu. Bu dongu, kullanici ekranda
olmasa da duzenli araliklarla Bet365 oranini kaydeder.

Neden ~3 saat: API-Football pre-match oranlarini yaklasik 3 saatte bir
gunceller. Daha sik cagirmak yeni veri getirmez, sadece kota yakar.

Not: Render'in ucretsiz web servisi 15 dakika trafiksizlikte uyur ve bu
dongu de onunla birlikte durur. Dis bir cagirici (cron-job.org, UptimeRobot
vb.) ``/api/health`` adresini duzenli olarak pinglerse dongu ayakta kalir.
Servis uyursa snapshot alinmaz - eksik snapshot UYDURULMAZ, sadece
kaydedilmemis olur.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from api_client import ApiFootballClient, CallBudget
from bookmakers import BookmakerResolver
from config import Settings, get_settings
from database import Database, utcnow
from errors import AppError
from odds_parser import parse_odds_rows
from services import MatchService, parse_api_datetime
from snapshots import capture_many

logger = logging.getLogger(__name__)


class SnapshotScheduler:
    """Basit asyncio dongusu - ek bagimlilik gerektirmez."""

    def __init__(
        self,
        client: ApiFootballClient,
        database: Database,
        resolver: BookmakerResolver,
        service: MatchService,
        settings: Settings | None = None,
    ) -> None:
        self.client = client
        self.database = database
        self.resolver = resolver
        self.service = service
        self.settings = settings or get_settings()
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self.last_run_at: datetime | None = None
        self.last_result: dict[str, Any] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if not self.settings.snapshot_enabled:
            logger.info("Snapshot toplayici kapali (SNAPSHOT_ENABLED=false)")
            return
        if not self.settings.api_key_configured:
            logger.warning("API anahtari yok; snapshot toplayici baslatilmadi")
            return
        if self.running:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="snapshot-scheduler")
        logger.info(
            "Snapshot toplayici basladi (her %s dakikada bir)",
            self.settings.snapshot_interval_minutes,
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # pragma: no cover
                pass
            self._task = None

    async def _loop(self) -> None:
        # Acilista hemen bir tur at, sonra periyodik devam et.
        try:
            await asyncio.sleep(15)
            while not self._stopping.is_set():
                try:
                    self.last_result = await self.run_once()
                    self.last_run_at = utcnow()
                except AppError as exc:
                    logger.warning("Snapshot turu basarisiz: %s", exc.code)
                except Exception:  # pragma: no cover - dongu olmemeli
                    logger.exception("Snapshot turunda beklenmeyen hata")
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(),
                        timeout=self.settings.snapshot_interval_minutes * 60,
                    )
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:  # pragma: no cover
            logger.info("Snapshot toplayici durduruldu")
            raise

    async def run_once(self) -> dict[str, Any]:
        """Tek bir snapshot turu: yaklasan maclarin Bet365 oranini kaydet."""
        budget = CallBudget(limit=self.settings.max_calls_per_refresh)
        result: dict[str, Any] = {
            "started_at": utcnow().isoformat(),
            "written": 0,
            "fixtures_seen": 0,
            "leagues": [],
            "warnings": [],
        }

        bookmaker, error = await self.resolver.try_resolve(budget=budget)
        if bookmaker is None:
            result["warnings"].append(error or "Bookmaker bulunamadi")
            return result

        today = datetime.now(tz=self.settings.tzinfo).date()
        horizon = today + timedelta(days=self.settings.snapshot_horizon_days)

        for league_id in self.settings.league_ids:
            try:
                context = await self.service.league_context(league_id, budget=budget)
                if not context.usable:
                    continue

                fixtures = await self.client.fixtures(
                    league=league_id,
                    season=context.season,
                    date_from=today.isoformat(),
                    date_to=horizon.isoformat(),
                    budget=budget,
                )
                if not fixtures:
                    continue
                result["fixtures_seen"] += len(fixtures)

                kickoffs = {}
                days = set()
                for row in fixtures:
                    fixture = row.get("fixture") or {}
                    fixture_id = fixture.get("id")
                    if fixture_id is None:
                        continue
                    kickoffs[int(fixture_id)] = fixture.get("date")
                    parsed = parse_api_datetime(fixture.get("date"))
                    if parsed is not None:
                        days.add(parsed.date().isoformat())

                entries: dict[int, dict[str, Any]] = {}
                for day in sorted(days):
                    rows = await self.client.odds(
                        league=league_id,
                        season=context.season,
                        date=day,
                        bookmaker=bookmaker.id,
                        budget=budget,
                        # Snapshot turunda taze veri lazim; cache'i atla.
                    )
                    entries.update(parse_odds_rows(rows, bookmaker.id, bookmaker.name))

                written = capture_many(self.database, entries, kickoffs, self.settings)
                result["written"] += written
                result["leagues"].append(
                    {
                        "league_id": league_id,
                        "name": context.name,
                        "fixtures": len(fixtures),
                        "with_odds": len(entries),
                        "snapshots_written": written,
                    }
                )
            except AppError as exc:
                result["warnings"].append(f"Lig {league_id}: {exc.user_message}")
                if exc.code in {"api_quota_exceeded", "budget_exhausted"}:
                    break

        result["api_calls"] = budget.used
        result["finished_at"] = utcnow().isoformat()
        logger.info(
            "Snapshot turu tamamlandi: %s kayit, %s API cagrisi",
            result["written"],
            budget.used,
        )
        try:
            self.database.cache_purge_expired()
        except AppError:  # pragma: no cover
            pass
        return result
