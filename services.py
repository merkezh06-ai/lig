"""Orkestrasyon katmani: API -> analiz -> frontend sozlesmesi.

Kota stratejisi (iki kademeli)
------------------------------
Kademe 1 (liste):  lig basina 1 sezon + 1 fikstur + 1 gecmis + tarih basina
                   1 oran cagrisi. 6 lig / 7 gun icin ~50-60 cagri.
Kademe 2 (detay):  H2H, sakatlik ve API predictions YALNIZCA kullanici bir
                   maca tikladiginda cekilir (mac basina 3 cagri).

Liste ekranindaki model, piyasa olasiligi, fark, guven ve veri kalitesi
kademe 1 verisiyle hesaplanir; ek cagri gerektirmez.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from analysis_engine import (
    LeagueAverages,
    MatchRecord,
    calculate_league_averages,
    generate_match_analysis,
    rank_top_matches,
)
from api_client import ApiFootballClient, CallBudget
from bookmakers import BookmakerRef, BookmakerResolver
from config import Settings, get_settings
from database import Database, utcnow
from errors import AppError, FixtureNotFoundError, SeasonNotAccessibleError
from models import (
    ApiPredictionBlock,
    Bet365Block,
    ConfidenceScore,
    DataQuality,
    H2HBlock,
    HighlightCard,
    HomeAwayBlock,
    ImpliedProbabilities,
    InjuriesBlock,
    Kickoff,
    LeagueRef,
    MarketOdds,
    MarketProbabilities,
    MatchDetail,
    MatchSummary,
    ModelBlock,
    OddsMovement,
    ScoreComponent,
    SnapshotPoint,
    TeamFormBlock,
    TeamRef,
    ValueEdge,
)
from odds_parser import MARKET_1X2, market_1x2, parse_odds_rows
from snapshots import capture_many, snapshots_bulk

logger = logging.getLogger(__name__)

FINISHED_STATUSES = {"FT", "AET", "PEN"}
UPCOMING_STATUSES = {"NS", "TBD", "PST", "1H", "HT", "2H", "ET", "BT", "P", "LIVE"}


# ==========================================================================
# Yardimcilar
# ==========================================================================
def parse_api_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def build_kickoff(value: Any, settings: Settings) -> Kickoff:
    """API'nin UTC zamanini Turkiye saatine cevirip formatlar."""
    parsed = parse_api_datetime(value)
    if parsed is None:
        return Kickoff(iso="", display="-", date="-", time="-", timezone=settings.timezone_name)
    local = parsed.astimezone(settings.tzinfo)
    minutes = int((parsed - utcnow()).total_seconds() // 60)
    return Kickoff(
        iso=parsed.isoformat(),
        display=local.strftime("%d.%m.%Y %H:%M"),
        date=local.strftime("%d.%m.%Y"),
        time=local.strftime("%H:%M"),
        timezone=settings.timezone_name,
        minutes_until=minutes,
    )


def _components(raw: Iterable[Mapping[str, Any]]) -> list[ScoreComponent]:
    return [ScoreComponent(**dict(item)) for item in raw]


def _snapshot_point(raw: Mapping[str, Any] | None) -> SnapshotPoint | None:
    if not raw or not raw.get("captured_at"):
        return None
    return SnapshotPoint(
        label=raw["label"],
        captured_at=raw["captured_at"],
        values=raw.get("values") or {},
        minutes_to_kickoff=raw.get("minutes_to_kickoff"),
    )


def _round_map(values: Mapping[str, float] | None, digits: int = 4) -> dict[str, float]:
    return {key: round(float(value), digits) for key, value in (values or {}).items()}


def _movement_block(raw: Mapping[str, Any]) -> OddsMovement:
    return OddsMovement(
        available=raw["available"],
        source=raw["source"],
        note=raw["note"],
        snapshot_count=raw["snapshot_count"],
        first_recorded=_snapshot_point(raw["first_recorded"]),
        latest=_snapshot_point(raw["latest"]),
        closest_to_kickoff=_snapshot_point(raw["closest_to_kickoff"]),
        change_pct=raw["change_pct"],
        direction=raw["direction"],
    )


# ==========================================================================
# Lig baglami
# ==========================================================================
@dataclass
class LeagueContext:
    league_id: int
    season: int | None = None
    name: str = ""
    country: str | None = None
    logo: str | None = None
    warning: str | None = None

    @property
    def usable(self) -> bool:
        return self.season is not None


@dataclass
class LeagueData:
    """Bir lig icin kademe-1'de toplanan her sey."""

    context: LeagueContext
    fixtures: list[dict[str, Any]] = field(default_factory=list)
    team_records: dict[int, list[MatchRecord]] = field(default_factory=dict)
    averages: LeagueAverages = field(default_factory=lambda: LeagueAverages(0.0, 0.0, 0))
    odds: dict[int, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def build_team_records(
    rows: Sequence[Mapping[str, Any]]
) -> tuple[dict[int, list[MatchRecord]], LeagueAverages]:
    """Bitmis maclardan takim bazli kayit listesi ve lig ortalamalari uretir."""
    per_team: dict[int, list[MatchRecord]] = {}
    all_records: list[MatchRecord] = []

    for row in rows:
        fixture = row.get("fixture") or {}
        status = ((fixture.get("status") or {}).get("short") or "").upper()
        if status not in FINISHED_STATUSES:
            continue

        goals = row.get("goals") or {}
        home_goals, away_goals = goals.get("home"), goals.get("away")
        if home_goals is None or away_goals is None:
            continue

        halftime = (row.get("score") or {}).get("halftime") or {}
        ht_home, ht_away = halftime.get("home"), halftime.get("away")

        teams = row.get("teams") or {}
        try:
            home_id = int((teams.get("home") or {}).get("id"))
            away_id = int((teams.get("away") or {}).get("id"))
            fixture_id = int(fixture.get("id"))
        except (TypeError, ValueError):
            continue

        kickoff = parse_api_datetime(fixture.get("date"))

        home_record = MatchRecord(
            fixture_id=fixture_id,
            is_home=True,
            goals_for=int(home_goals),
            goals_against=int(away_goals),
            kickoff=kickoff,
            ht_goals_for=int(ht_home) if ht_home is not None else None,
            ht_goals_against=int(ht_away) if ht_away is not None else None,
        )
        away_record = MatchRecord(
            fixture_id=fixture_id,
            is_home=False,
            goals_for=int(away_goals),
            goals_against=int(home_goals),
            kickoff=kickoff,
            ht_goals_for=int(ht_away) if ht_away is not None else None,
            ht_goals_against=int(ht_home) if ht_home is not None else None,
        )

        per_team.setdefault(home_id, []).append(home_record)
        per_team.setdefault(away_id, []).append(away_record)
        all_records.extend((home_record, away_record))

    return per_team, calculate_league_averages(all_records)


# ==========================================================================
# Servis
# ==========================================================================
class MatchService:
    def __init__(
        self,
        client: ApiFootballClient,
        database: Database,
        resolver: BookmakerResolver,
        settings: Settings | None = None,
    ) -> None:
        self.client = client
        self.database = database
        self.resolver = resolver
        self.settings = settings or get_settings()
        self._league_cache: dict[int, LeagueContext] = {}

    # ------------------------------------------------------------------
    # Lig / sezon
    # ------------------------------------------------------------------
    async def league_context(
        self, league_id: int, budget: CallBudget | None = None
    ) -> LeagueContext:
        """Ligin guncel sezonunu ve gercek adini API'den ogrenir."""
        cached = self._league_cache.get(league_id)
        if cached is not None:
            return cached

        season, info = await self.client.current_season(league_id, budget=budget)
        configured = self.settings.league(league_id)
        context = LeagueContext(
            league_id=league_id,
            season=season,
            name=info.get("name") or (configured.expected_name if configured else f"Lig {league_id}"),
            country=info.get("country") or (configured.country if configured else None),
            logo=info.get("logo"),
        )

        if not info.get("found"):
            context.warning = f"Lig {league_id} API'de bulunamadi."
        elif configured and configured.expected_name and info.get("name"):
            if configured.expected_name.strip().lower() != str(info["name"]).strip().lower():
                context.warning = (
                    f"Lig ID {league_id} icin beklenen ad '{configured.expected_name}', "
                    f"API '{info['name']}' dondurdu. Lig ID'sini kontrol edin."
                )
        if season is None and info.get("found"):
            context.warning = f"Lig {league_id} icin guncel sezon tespit edilemedi."

        if season is not None:
            try:
                self.database.upsert_league(
                    league_id, context.name, context.country, season
                )
            except AppError:  # pragma: no cover
                pass

        self._league_cache[league_id] = context
        return context

    # ------------------------------------------------------------------
    # Kademe 1: lig verisi
    # ------------------------------------------------------------------
    async def collect_league(
        self,
        league_id: int,
        date_from: date,
        date_to: date,
        budget: CallBudget,
        bookmaker: BookmakerRef | None,
    ) -> LeagueData:
        context = await self.league_context(league_id, budget=budget)
        data = LeagueData(context=context)
        if context.warning:
            data.warnings.append(context.warning)
        if not context.usable:
            return data

        season = context.season
        assert season is not None

        # --- Yaklasan maclar ---
        try:
            data.fixtures = await self.client.fixtures(
                league=league_id,
                season=season,
                date_from=date_from.isoformat(),
                date_to=date_to.isoformat(),
                budget=budget,
            )
        except SeasonNotAccessibleError as exc:
            # Plan bu sezona erisim vermiyor. BOS LISTE DONDURMUYORUZ - sebebi
            # ligin ve sezonun adiyla zenginlestirip yukari firlatiyoruz.
            exc.league_id = league_id
            exc.season = season
            exc.user_message = (
                f"API-Football planiniz {season} sezonuna erisim vermiyor "
                f"({context.name}). Guncel sezon maclari bu planla cekilemez."
            )
            logger.warning(
                "Plan kisiti: lig=%s sezon=%s -> %s",
                league_id, season, exc.provider_message,
            )
            raise

        if not data.fixtures:
            return data

        # --- Sezonun bitmis maclari (model icin) ---
        # Tek cagri ile butun takimlarin gecmisi gelir; takim basina ayri
        # istek atmaktan cok daha ucuzdur.
        try:
            history = await self.client.fixtures(
                league=league_id,
                season=season,
                status="FT-AET-PEN",
                ttl=self.settings.ttl.fixtures_finished,
                budget=budget,
            )
        except AppError as exc:
            if exc.fatal:
                raise
            data.warnings.append(
                f"{context.name}: gecmis mac verisi alinamadi ({exc.user_message})"
            )
            history = []

        data.team_records, data.averages = build_team_records(history)

        # --- Bet365 oranlari (tarih bazinda toplu) ---
        if bookmaker is not None:
            fixture_dates = sorted(
                {
                    (parse_api_datetime((row.get("fixture") or {}).get("date")) or utcnow())
                    .date()
                    .isoformat()
                    for row in data.fixtures
                }
            )
            for day in fixture_dates:
                try:
                    rows = await self.client.odds(
                        league=league_id,
                        season=season,
                        date=day,
                        bookmaker=bookmaker.id,
                        budget=budget,
                    )
                except AppError as exc:
                    if exc.fatal:
                        raise
                    data.warnings.append(
                        f"{context.name} {day}: oran verisi alinamadi ({exc.user_message})"
                    )
                    continue
                data.odds.update(parse_odds_rows(rows, bookmaker.id, bookmaker.name))

        return data

    # ------------------------------------------------------------------
    # Mac ozeti uretimi
    # ------------------------------------------------------------------
    def _bet365_block(
        self, entry: Mapping[str, Any] | None, bookmaker: BookmakerRef | None
    ) -> Bet365Block:
        if entry is None:
            if bookmaker is None:
                reason = (
                    f"{self.settings.bookmaker_name} bookmaker'i bulunamadigi icin "
                    "oran cekilemedi"
                )
            else:
                reason = f"Bu mac icin {bookmaker.name} orani mevcut degil"
            return Bet365Block(available=False, reason=reason, source="unavailable")

        markets = entry.get("markets") or {}
        main = markets.get(MARKET_1X2) or {}
        return Bet365Block(
            available=bool(main),
            reason=None if main else "1X2 marketi bulunamadi",
            bookmaker_id=entry.get("bookmaker_id"),
            bookmaker_name=entry.get("bookmaker_name"),
            updated_at=parse_api_datetime(entry.get("updated_at")),
            source="api" if main else "unavailable",
            home=main.get("1"),
            draw=main.get("X"),
            away=main.get("2"),
            markets={
                key: MarketOdds(available=True, source="api", values=values)
                for key, values in markets.items()
            },
        )

    def _model_block(self, model_result: Mapping[str, Any]) -> ModelBlock:
        if not model_result.get("available"):
            return ModelBlock(
                available=False,
                source="unavailable",
                note=model_result.get("note"),
                matches_used_home=int(model_result.get("matches_used_home") or 0),
                matches_used_away=int(model_result.get("matches_used_away") or 0),
            )

        markets = model_result["markets"]
        first_half = model_result.get("first_half") or {}
        return ModelBlock(
            available=True,
            source="model",
            expected_goals_home=model_result.get("expected_goals_home"),
            expected_goals_away=model_result.get("expected_goals_away"),
            match_result=MarketProbabilities(
                available=True, source="model", probabilities=_round_map(markets["match_result"])
            ),
            first_half=MarketProbabilities(
                available=bool(first_half.get("available")),
                source="model" if first_half.get("available") else "unavailable",
                probabilities=_round_map(first_half.get("probabilities")),
                note=first_half.get("note"),
            ),
            both_teams_to_score=MarketProbabilities(
                available=True,
                source="model",
                probabilities=_round_map(markets["both_teams_to_score"]),
            ),
            over_under_25=MarketProbabilities(
                available=True, source="model", probabilities=_round_map(markets["over_under_25"])
            ),
            double_chance=MarketProbabilities(
                available=True, source="model", probabilities=_round_map(markets["double_chance"])
            ),
            top_scoreline=model_result.get("top_scoreline"),
            matches_used_home=int(model_result.get("matches_used_home") or 0),
            matches_used_away=int(model_result.get("matches_used_away") or 0),
        )

    def build_summary(
        self,
        fixture_row: Mapping[str, Any],
        league_data: LeagueData,
        bookmaker: BookmakerRef | None,
        snapshots: Sequence[Mapping[str, Any]] = (),
    ) -> tuple[MatchSummary, dict[str, Any]]:
        fixture = fixture_row.get("fixture") or {}
        teams = fixture_row.get("teams") or {}
        league_row = fixture_row.get("league") or {}
        status = fixture.get("status") or {}

        fixture_id = int(fixture.get("id"))
        home_team = teams.get("home") or {}
        away_team = teams.get("away") or {}
        home_id = int(home_team.get("id") or 0)
        away_id = int(away_team.get("id") or 0)

        kickoff = build_kickoff(fixture.get("date"), self.settings)
        odds_entry = league_data.odds.get(fixture_id)
        odds_1x2 = market_1x2(odds_entry)

        home_records = league_data.team_records.get(home_id, [])
        away_records = league_data.team_records.get(away_id, [])

        analysis = generate_match_analysis(
            home_name=str(home_team.get("name") or "Ev sahibi"),
            away_name=str(away_team.get("name") or "Deplasman"),
            home_records=home_records,
            away_records=away_records,
            odds_1x2=odds_1x2,
            snapshots=list(snapshots),
            kickoff=parse_api_datetime(fixture.get("date")),
            h2h_matches=0,          # kademe 2'de doldurulur
            injuries_available=False,
            predictions_available=False,
            model_config=self.settings.model,
            confidence_weights=self.settings.confidence_weights,
            data_quality_weights=self.settings.data_quality_weights,
            league_averages=league_data.averages if league_data.averages.available else None,
        )

        market = analysis["market"]
        value = analysis["value"]

        summary = MatchSummary(
            fixture_id=fixture_id,
            league=LeagueRef(
                id=int(league_row.get("id") or league_data.context.league_id),
                name=str(league_row.get("name") or league_data.context.name),
                country=league_row.get("country") or league_data.context.country,
                season=league_row.get("season") or league_data.context.season,
                logo=league_row.get("logo") or league_data.context.logo,
                round=league_row.get("round"),
            ),
            home=TeamRef(id=home_id, name=str(home_team.get("name") or "-"), logo=home_team.get("logo")),
            away=TeamRef(id=away_id, name=str(away_team.get("name") or "-"), logo=away_team.get("logo")),
            kickoff=kickoff,
            status=str(status.get("long") or "-"),
            status_short=str(status.get("short") or "-"),
            bet365=self._bet365_block(odds_entry, bookmaker),
            implied=ImpliedProbabilities(
                available=market["available"],
                source=market["source"],
                raw=market["raw"],
                normalized=market["normalized"],
                overround=market["overround"],
                margin_pct=market["margin_pct"],
                method=market["method"],
            ),
            model=self._model_block(analysis["model"]),
            value=ValueEdge(
                available=value["available"],
                source=value["source"],
                edges=value["edges"],
                best_outcome=value["best_outcome"],
                best_edge=value["best_edge"],
                note=value["note"],
            ),
            confidence=ConfidenceScore(
                score=analysis["confidence"]["score"],
                max_score=analysis["confidence"]["max_score"],
                components=_components(analysis["confidence"]["components"]),
            ),
            data_quality=DataQuality(
                score=analysis["data_quality"]["score"],
                max_score=analysis["data_quality"]["max_score"],
                components=_components(analysis["data_quality"]["components"]),
                missing=analysis["data_quality"]["missing"],
            ),
            odds_movement=_movement_block(analysis["movement"]),
            analyzed=analysis["model"]["available"],
            analysis_note=analysis["model"].get("note"),
        )
        return summary, analysis

    # ------------------------------------------------------------------
    # Ana liste
    # ------------------------------------------------------------------
    async def list_matches(
        self,
        league_ids: Sequence[int],
        date_from: date,
        date_to: date,
    ) -> tuple[list[MatchSummary], list[dict[str, Any]], CallBudget, list[str], BookmakerRef | None]:
        budget = CallBudget(limit=self.settings.max_calls_per_refresh)
        warnings: list[str] = []

        bookmaker, bookmaker_error = await self.resolver.try_resolve(budget=budget)
        if bookmaker is None and bookmaker_error:
            warnings.append(bookmaker_error)

        if not self.database.persistent:
            warnings.append(
                "Veritabani kalici degil (SQLite). Sunucu her yeniden basladiginda "
                "oran snapshot gecmisi sifirlanir; gecmis oran uydurulmaz."
            )

        summaries: list[MatchSummary] = []
        analyses: list[dict[str, Any]] = []
        # Kismi basari ile tam basarisizligi ayirt edebilmek icin.
        successful_leagues = 0
        league_errors: list[AppError] = []
        plan_limits: list[SeasonNotAccessibleError] = []

        for league_id in league_ids:
            try:
                league_data = await self.collect_league(
                    league_id, date_from, date_to, budget, bookmaker
                )
            except SeasonNotAccessibleError as exc:
                # Plan kisiti: sessizce "mac yok" gosterilmez.
                league_errors.append(exc)
                plan_limits.append(exc)
                warnings.append(exc.user_message)
                continue
            except AppError as exc:
                # Yapilandirma/kimlik hatasi: hicbir lig calismayacak, hemen bildir.
                if exc.fatal:
                    raise
                league_errors.append(exc)
                warnings.append(f"Lig {league_id}: {exc.user_message}")
                continue

            successful_leagues += 1
            warnings.extend(league_data.warnings)
            if not league_data.fixtures:
                continue

            # Snapshot: once yaz, sonra oku (guncel oran da gecmise girsin)
            kickoffs = {
                int((row.get("fixture") or {}).get("id")): (row.get("fixture") or {}).get("date")
                for row in league_data.fixtures
                if (row.get("fixture") or {}).get("id") is not None
            }
            capture_many(self.database, league_data.odds, kickoffs, self.settings)

            fixture_ids = [int((row.get("fixture") or {}).get("id")) for row in league_data.fixtures]
            grouped_snapshots = snapshots_bulk(self.database, fixture_ids)

            for row in league_data.fixtures:
                try:
                    fixture_id = int((row.get("fixture") or {}).get("id"))
                except (TypeError, ValueError):
                    continue
                summary, analysis = self.build_summary(
                    row, league_data, bookmaker, grouped_snapshots.get(fixture_id, [])
                )
                summaries.append(summary)
                analyses.append({"summary": summary, "analysis": analysis})
                self._log_prediction_once(summary, analysis)

        # Hicbir lig cekilemediyse bos liste dondurup "mac yok" izlenimi verme;
        # gercek sebebi yukari firlat. (Bos liste + uyari, kullaniciya
        # "bugun mac yok" gibi gorunuyordu.)
        if successful_leagues == 0 and league_errors:
            # Plan kisiti varsa onu firlat: en aciklayici ve eyleme donuk hata odur.
            raise plan_limits[0] if plan_limits else league_errors[0]

        if budget.reasons:
            warnings.extend(budget.reasons)
        if self.client.quota_low():
            warnings.append(
                f"API kotasi azaliyor (kalan: {self.client.quota.daily_remaining})."
            )

        summaries.sort(key=lambda item: item.kickoff.iso or "")
        return summaries, analyses, budget, warnings, bookmaker

    def _log_prediction_once(self, summary: MatchSummary, analysis: Mapping[str, Any]) -> None:
        """Model ciktisini kalibrasyon icin bir kez kaydeder."""
        if not summary.model.available or not summary.implied.available:
            return
        try:
            existing = self.database.query_one(
                "SELECT id FROM predictions_log WHERE fixture_id = ? LIMIT 1",
                (summary.fixture_id,),
            )
            if existing:
                return
            probabilities = summary.model.match_result.probabilities
            market = summary.implied.normalized
            self.database.log_prediction(
                {
                    "fixture_id": summary.fixture_id,
                    "kickoff_utc": summary.kickoff.iso,
                    "model_home": probabilities.get("1"),
                    "model_draw": probabilities.get("X"),
                    "model_away": probabilities.get("2"),
                    "market_home": market.get("1"),
                    "market_draw": market.get("X"),
                    "market_away": market.get("2"),
                    "confidence": summary.confidence.score,
                    "data_quality": summary.data_quality.score,
                }
            )
        except AppError:  # pragma: no cover
            logger.debug("Tahmin kaydi yazilamadi: %s", summary.fixture_id)

    # ------------------------------------------------------------------
    # Kademe 2: mac detayi
    # ------------------------------------------------------------------
    async def match_detail(self, fixture_id: int) -> MatchDetail:
        budget = CallBudget(limit=max(12, self.settings.max_calls_per_refresh // 10))
        bookmaker, _ = await self.resolver.try_resolve(budget=budget)

        rows = await self.client.fixtures(fixture_id=fixture_id, budget=budget)
        if not rows:
            raise FixtureNotFoundError()
        fixture_row = rows[0]

        fixture = fixture_row.get("fixture") or {}
        league_row = fixture_row.get("league") or {}
        teams = fixture_row.get("teams") or {}
        league_id = int(league_row.get("id") or 0)
        season = league_row.get("season")
        if season is None:
            context = await self.league_context(league_id, budget=budget)
            season = context.season

        home_id = int((teams.get("home") or {}).get("id") or 0)
        away_id = int((teams.get("away") or {}).get("id") or 0)

        # --- Gecmis maclar (model) ---
        history: list[dict[str, Any]] = []
        if league_id and season:
            try:
                history = await self.client.fixtures(
                    league=league_id,
                    season=season,
                    status="FT-AET-PEN",
                    ttl=self.settings.ttl.fixtures_finished,
                    budget=budget,
                )
            except AppError as exc:
                logger.warning("Detay icin gecmis alinamadi: %s", exc.code)

        team_records, averages = build_team_records(history)

        # --- Oranlar ---
        odds_entry: dict[str, Any] | None = None
        if bookmaker is not None:
            try:
                odds_rows = await self.client.odds(
                    fixture=fixture_id, bookmaker=bookmaker.id, budget=budget
                )
                parsed = parse_odds_rows(odds_rows, bookmaker.id, bookmaker.name)
                odds_entry = parsed.get(fixture_id)
                if odds_entry:
                    capture_many(
                        self.database,
                        {fixture_id: odds_entry},
                        {fixture_id: fixture.get("date")},
                        self.settings,
                    )
            except AppError as exc:
                logger.warning("Detay icin oran alinamadi: %s", exc.code)

        # --- Kademe 2 verileri (paralel) ---
        h2h_rows, injury_rows, prediction_rows = await self._detail_extras(
            home_id, away_id, fixture_id, budget
        )

        league_data = LeagueData(
            context=LeagueContext(
                league_id=league_id,
                season=season,
                name=str(league_row.get("name") or ""),
                country=league_row.get("country"),
                logo=league_row.get("logo"),
            ),
            fixtures=[fixture_row],
            team_records=team_records,
            averages=averages,
            odds={fixture_id: odds_entry} if odds_entry else {},
        )

        snapshots = snapshots_bulk(self.database, [fixture_id]).get(fixture_id, [])
        summary, _ = self.build_summary(fixture_row, league_data, bookmaker, snapshots)

        # H2H / sakatlik / predictions ile yeniden hesapla
        h2h_block = self._h2h_block(h2h_rows, home_id, away_id)
        injuries_block = self._injuries_block(injury_rows, home_id, away_id)
        prediction_block = self._prediction_block(prediction_rows)

        analysis = generate_match_analysis(
            home_name=summary.home.name,
            away_name=summary.away.name,
            home_records=team_records.get(home_id, []),
            away_records=team_records.get(away_id, []),
            odds_1x2=market_1x2(odds_entry),
            snapshots=snapshots,
            kickoff=parse_api_datetime(fixture.get("date")),
            h2h_matches=h2h_block.matches_used,
            injuries_available=injuries_block.available,
            home_injuries=injuries_block.home_count,
            away_injuries=injuries_block.away_count,
            predictions_available=prediction_block.available,
            model_config=self.settings.model,
            confidence_weights=self.settings.confidence_weights,
            data_quality_weights=self.settings.data_quality_weights,
            league_averages=averages if averages.available else None,
        )

        summary.confidence = ConfidenceScore(
            score=analysis["confidence"]["score"],
            max_score=analysis["confidence"]["max_score"],
            components=_components(analysis["confidence"]["components"]),
        )
        summary.data_quality = DataQuality(
            score=analysis["data_quality"]["score"],
            max_score=analysis["data_quality"]["max_score"],
            components=_components(analysis["data_quality"]["components"]),
            missing=analysis["data_quality"]["missing"],
        )

        summary.odds_movement = _movement_block(analysis["movement"])

        detail = MatchDetail(
            **summary.model_dump(),
            home_form=self._form_block(team_records.get(home_id, [])),
            away_form=self._form_block(team_records.get(away_id, [])),
            home_venue=self._venue_block(team_records.get(home_id, []), at_home=True),
            away_venue=self._venue_block(team_records.get(away_id, []), at_home=False),
            h2h=h2h_block,
            injuries=injuries_block,
            api_prediction=prediction_block,
            explanation=analysis["explanation"],
        )
        return detail

    async def _detail_extras(
        self, home_id: int, away_id: int, fixture_id: int, budget: CallBudget
    ) -> tuple[list[Any], list[Any], list[Any]]:
        async def safe(coro):
            try:
                return await coro
            except AppError as exc:
                logger.info("Detay verisi alinamadi: %s", exc.code)
                return []

        tasks = [
            safe(self.client.head_to_head(home_id, away_id, budget=budget)),
            safe(self.client.injuries(fixture_id, budget=budget)),
            safe(self.client.predictions(fixture_id, budget=budget)),
        ]
        results = await asyncio.gather(*tasks)
        return results[0], results[1], results[2]

    # ------------------------------------------------------------------
    # Detay bloklari
    # ------------------------------------------------------------------
    def _form_block(self, records: Sequence[MatchRecord]) -> TeamFormBlock:
        if not records:
            return TeamFormBlock(
                available=False, source="unavailable", note="Mac gecmisi bulunamadi"
            )
        recent = sorted(records, key=lambda item: item.kickoff or datetime.min, reverse=True)[:5]
        wins = sum(1 for item in recent if item.goals_for > item.goals_against)
        draws = sum(1 for item in recent if item.goals_for == item.goals_against)
        losses = len(recent) - wins - draws
        results = [
            "G" if item.goals_for > item.goals_against
            else ("B" if item.goals_for == item.goals_against else "M")
            for item in recent
        ]
        return TeamFormBlock(
            available=True,
            source="api",
            matches_used=len(recent),
            wins=wins,
            draws=draws,
            losses=losses,
            goals_for=sum(item.goals_for for item in recent),
            goals_against=sum(item.goals_against for item in recent),
            points_per_game=round((wins * 3 + draws) / len(recent), 2) if recent else None,
            results=results,
        )

    def _venue_block(self, records: Sequence[MatchRecord], at_home: bool) -> HomeAwayBlock:
        subset = [item for item in records if item.is_home == at_home]
        if not subset:
            return HomeAwayBlock(
                available=False,
                source="unavailable",
                note="Ev/deplasman verisi bulunamadi",
            )
        return HomeAwayBlock(
            available=True,
            source="api",
            matches=len(subset),
            goals_for_avg=round(sum(item.goals_for for item in subset) / len(subset), 2),
            goals_against_avg=round(sum(item.goals_against for item in subset) / len(subset), 2),
        )

    def _h2h_block(self, rows: Sequence[Mapping[str, Any]], home_id: int, away_id: int) -> H2HBlock:
        finished = []
        for row in rows:
            status = (((row.get("fixture") or {}).get("status") or {}).get("short") or "").upper()
            goals = row.get("goals") or {}
            if status in FINISHED_STATUSES and goals.get("home") is not None:
                finished.append(row)

        if not finished:
            return H2HBlock(available=False, source="unavailable", note="H2H verisi bulunamadi")

        home_wins = draws = away_wins = 0
        goals_home = goals_away = 0
        recent: list[dict[str, Any]] = []
        for row in finished[:10]:
            teams = row.get("teams") or {}
            goals = row.get("goals") or {}
            row_home_id = int((teams.get("home") or {}).get("id") or 0)
            gh, ga = int(goals.get("home") or 0), int(goals.get("away") or 0)
            # Skorlari her zaman "bizim ev sahibi" perspektifine cevir.
            if row_home_id == home_id:
                our, theirs = gh, ga
            else:
                our, theirs = ga, gh
            goals_home += our
            goals_away += theirs
            if our > theirs:
                home_wins += 1
            elif our == theirs:
                draws += 1
            else:
                away_wins += 1
            recent.append(
                {
                    "date": build_kickoff(
                        (row.get("fixture") or {}).get("date"), self.settings
                    ).date,
                    "home": (teams.get("home") or {}).get("name"),
                    "away": (teams.get("away") or {}).get("name"),
                    "score": f"{gh}-{ga}",
                }
            )

        return H2HBlock(
            available=True,
            source="api",
            matches_used=len(finished[:10]),
            home_wins=home_wins,
            draws=draws,
            away_wins=away_wins,
            goals_home=goals_home,
            goals_away=goals_away,
            recent=recent,
        )

    def _injuries_block(
        self, rows: Sequence[Mapping[str, Any]], home_id: int, away_id: int
    ) -> InjuriesBlock:
        if not rows:
            return InjuriesBlock(
                available=False, source="unavailable", note="Sakatlik verisi bulunamadi"
            )
        home_players: list[dict[str, Any]] = []
        away_players: list[dict[str, Any]] = []
        for row in rows:
            team_id = int((row.get("team") or {}).get("id") or 0)
            player = row.get("player") or {}
            record = {
                "name": player.get("name"),
                "type": player.get("type"),
                "reason": player.get("reason"),
            }
            if team_id == home_id:
                home_players.append(record)
            elif team_id == away_id:
                away_players.append(record)
        return InjuriesBlock(
            available=True,
            source="api",
            home_count=len(home_players),
            away_count=len(away_players),
            home_players=home_players,
            away_players=away_players,
            note="Sakatlik verisi gol beklentisini degistirmez, yalnizca guven skorunu etkiler",
        )

    def _prediction_block(self, rows: Sequence[Mapping[str, Any]]) -> ApiPredictionBlock:
        if not rows:
            return ApiPredictionBlock(
                available=False,
                source="unavailable",
                note="API predictions verisi bulunamadi",
            )
        predictions = (rows[0] or {}).get("predictions") or {}
        percent = predictions.get("percent") or {}
        parsed: dict[str, float] = {}
        for key, label in (("home", "1"), ("draw", "X"), ("away", "2")):
            raw = percent.get(key)
            if isinstance(raw, str) and raw.endswith("%"):
                try:
                    parsed[label] = float(raw.rstrip("%")) / 100.0
                except ValueError:
                    continue
        return ApiPredictionBlock(
            available=True,
            source="api",
            winner_name=(predictions.get("winner") or {}).get("name"),
            advice=predictions.get("advice"),
            percent=parsed,
            note="API-Football'in kendi tahmini - bizim modelimiz degildir",
        )

    # ------------------------------------------------------------------
    # Top 5 ve one cikanlar
    # ------------------------------------------------------------------
    def top_matches(
        self, analyses: Sequence[Mapping[str, Any]]
    ) -> tuple[list[MatchSummary], dict[str, int]]:
        ranked, excluded = rank_top_matches(
            list(analyses),
            size=self.settings.top5_size,
            min_data_quality=self.settings.top5_min_data_quality,
        )
        return [item["summary"] for item in ranked], excluded


def build_highlights(summaries: Sequence[MatchSummary]) -> list[HighlightCard]:
    """One cikan analizler. Her kart GERCEK bir maca ve gercek bir sayiya baglidir.

    Aday bulunamayan kart ``available=False`` doner; bos kart uydurulmaz.
    """

    def card(
        key: str,
        label: str,
        candidates: list[tuple[float, MatchSummary, str]],
        formatter,
    ) -> HighlightCard:
        if not candidates:
            return HighlightCard(
                key=key, label=label, available=False, note="Uygun veri bulunamadi"
            )
        value, match, detail = max(candidates, key=lambda item: item[0])
        return HighlightCard(
            key=key,
            label=label,
            available=True,
            fixture_id=match.fixture_id,
            headline=f"{match.home.name} - {match.away.name}",
            detail=detail,
            value=round(value, 2),
        )

    ms: list[tuple[float, MatchSummary, str]] = []
    ht: list[tuple[float, MatchSummary, str]] = []
    btts: list[tuple[float, MatchSummary, str]] = []
    ou: list[tuple[float, MatchSummary, str]] = []
    movement: list[tuple[float, MatchSummary, str]] = []
    edge: list[tuple[float, MatchSummary, str]] = []
    quality: list[tuple[float, MatchSummary, str]] = []

    labels = {"1": "MS 1", "X": "MS X", "2": "MS 2"}

    for match in summaries:
        if match.model.match_result.available:
            key, value = max(
                match.model.match_result.probabilities.items(), key=lambda item: item[1]
            )
            ms.append((value * 100, match, f"{labels[key]} %{value * 100:.1f}"))
        if match.model.first_half.available:
            key, value = max(
                match.model.first_half.probabilities.items(), key=lambda item: item[1]
            )
            ht.append((value * 100, match, f"IY {key} %{value * 100:.1f}"))
        if match.model.both_teams_to_score.available:
            key, value = max(
                match.model.both_teams_to_score.probabilities.items(), key=lambda item: item[1]
            )
            btts.append((value * 100, match, f"KG {key.upper()} %{value * 100:.1f}"))
        if match.model.over_under_25.available:
            key, value = max(
                match.model.over_under_25.probabilities.items(), key=lambda item: item[1]
            )
            ou.append((value * 100, match, f"2.5 {key.upper()} %{value * 100:.1f}"))
        if match.odds_movement.available and match.odds_movement.change_pct:
            key, value = max(
                match.odds_movement.change_pct.items(), key=lambda item: abs(item[1])
            )
            direction = match.odds_movement.direction.get(key, "")
            movement.append((abs(value), match, f"{key} orani %{abs(value):.2f} {direction}"))
        if match.value.available and match.value.best_edge is not None:
            edge.append(
                (
                    match.value.best_edge,
                    match,
                    f"{labels.get(match.value.best_outcome or '', match.value.best_outcome or '')}"
                    f" icin +{match.value.best_edge:.1f} puan",
                )
            )
        if match.data_quality.score > 0:
            quality.append(
                (
                    match.data_quality.score,
                    match,
                    f"{match.data_quality.score:.0f}/{match.data_quality.max_score:.0f}",
                )
            )

    return [
        card("en_yuksek_ms", "En yuksek MS olasiligi", ms, None),
        card("en_yuksek_iy", "En yuksek IY olasiligi", ht, None),
        card("en_guclu_kg", "En guclu KG sinyali", btts, None),
        card("en_guclu_25", "En guclu 2.5 ust/alt", ou, None),
        card("en_guclu_hareket", "En guclu oran hareketi", movement, None),
        card("en_yuksek_fark", "En yuksek model / piyasa farki", edge, None),
        card("en_yuksek_kalite", "En yuksek veri kalitesi", quality, None),
    ]


def default_date_range(days: int, settings: Settings | None = None) -> tuple[date, date]:
    """Bugunden itibaren ``days`` gunluk araligi Turkiye takvimine gore uretir."""
    settings = settings or get_settings()
    today = datetime.now(tz=settings.tzinfo).date()
    return today, today + timedelta(days=max(0, days - 1))
