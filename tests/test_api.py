"""API istemcisi + servis katmani entegrasyon testleri.

httpx.MockTransport ile sahte bir API-Football sunucusu kurulur; boylece
gercek anahtar veya ag erisimi olmadan tum zincir test edilir:

    fixtures -> odds -> Bet365 filtresi -> snapshot -> model -> skorlar
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date as _real_date
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_module  # noqa: E402
import database as database_module  # noqa: E402
from api_client import ApiFootballClient, CallBudget  # noqa: E402
from bookmakers import BookmakerResolver  # noqa: E402
from database import Database  # noqa: E402
from errors import (  # noqa: E402
    ApiForbiddenError,
    ApiKeyInvalidError,
    ApiKeyMissingError,
    ApiQuotaExceededError,
    ApiTimeoutError,
    ApiUnavailableError,
    BookmakerNotFoundError,
    CallBudgetExhaustedError,
    SeasonNotAccessibleError,
)
from services import MatchService, build_highlights  # noqa: E402

BET365_ID = 8
OTHER_ID = 6
LEAGUE_ID = 39
SEASON = 2025

TODAY = datetime.now(tz=timezone.utc).date()
KICKOFF_1 = datetime.combine(TODAY, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=30)
KICKOFF_2 = KICKOFF_1 + timedelta(hours=2)


def _fixture(fixture_id, home_id, home, away_id, away, kickoff, status="NS",
             goals=None, halftime=None):
    return {
        "fixture": {
            "id": fixture_id,
            "date": kickoff.isoformat(),
            "status": {"short": status, "long": "Not Started" if status == "NS" else "Match Finished"},
        },
        "league": {"id": LEAGUE_ID, "name": "Premier League", "country": "England",
                   "season": SEASON, "round": "Regular Season - 5"},
        "teams": {"home": {"id": home_id, "name": home}, "away": {"id": away_id, "name": away}},
        "goals": goals or {"home": None, "away": None},
        "score": {"halftime": halftime or {"home": None, "away": None}},
    }


def _history() -> list[dict]:
    """Model icin yeterli bitmis mac uretir (iki takim x 8 mac)."""
    rows = []
    fixture_id = 500
    base = datetime.now(tz=timezone.utc) - timedelta(days=90)
    # 10 numarali takim guclu, 20 numarali takim zayif, 30/40 dolgu.
    schedule = [
        (10, 30, 3, 0), (30, 10, 0, 2), (10, 40, 2, 1), (40, 10, 1, 2),
        (10, 20, 2, 0), (20, 10, 0, 3), (10, 30, 2, 1), (40, 10, 0, 1),
        (20, 30, 0, 2), (30, 20, 3, 0), (20, 40, 1, 2), (40, 20, 2, 0),
        (20, 30, 1, 1), (30, 20, 2, 1), (20, 40, 0, 1), (40, 20, 3, 1),
        (30, 40, 1, 1), (40, 30, 2, 2), (30, 40, 2, 0), (40, 30, 1, 1),
    ]
    for index, (home_id, away_id, gh, ga) in enumerate(schedule):
        fixture_id += 1
        rows.append(
            _fixture(
                fixture_id,
                home_id, f"Team {home_id}",
                away_id, f"Team {away_id}",
                base + timedelta(days=index * 3),
                status="FT",
                goals={"home": gh, "away": ga},
                halftime={"home": max(0, gh - 1), "away": max(0, ga - 1)},
            )
        )
    return rows


def _bet(name, values):
    return {"name": name, "values": [{"value": v, "odd": o} for v, o in values]}


BET365_BETS = [
    _bet("Match Winner", [("Home", "2.10"), ("Draw", "3.40"), ("Away", "3.20")]),
    _bet("Both Teams Score", [("Yes", "1.72"), ("No", "2.05")]),
    _bet("Goals Over/Under", [("Over 2.5", "1.85"), ("Under 2.5", "1.95")]),
]
OTHER_BETS = [_bet("Match Winner", [("Home", "9.99"), ("Draw", "9.99"), ("Away", "9.99")])]


class FakeApi:
    """Sahte API-Football. Cagri sayaci ve senaryo anahtarlari tutar."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.bet365_present = True
        self.home_odd = "2.10"
        self.fail_with: int | None = None
        self.body_error: dict | None = None
        self.timeout_times = 0
        self.history_pages = 2
        #: Testler /leagues cevabini buradan degistirir. (Transport handler'i
        #: kurulum aninda yakaladigi icin handler'i sonradan degistirmek
        #: etkisizdir; bu yuzden veri seviyesinde kanca kullaniyoruz.)
        self.leagues_rows: list | None = None
        #: Ucretsiz plan kisiti simulasyonu: bu sezonlar icin API, HTTP 200
        #: govdesinde {"plan": "..."} hatasi doner (gercek davranis budur).
        self.blocked_seasons: set[int] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = {key: value[0] for key, value in parse_qs(request.url.query.decode()).items()}
        self.calls.append((path, params))

        if self.timeout_times > 0:
            self.timeout_times -= 1
            raise httpx.ReadTimeout("timeout", request=request)

        if self.fail_with:
            return httpx.Response(self.fail_with, json={"errors": {}}, request=request)

        if self.body_error is not None:
            return self._wrap([], errors=self.body_error)

        if path.endswith("/odds/bookmakers"):
            rows = [{"id": OTHER_ID, "name": "Bwin"}, {"id": 27, "name": "Betsson"}]
            if self.bet365_present:
                rows.append({"id": BET365_ID, "name": "Bet365"})
            return self._wrap(rows)

        if path.endswith("/leagues"):
            # Gercek API gibi davran: bilinmeyen lig ID'si bos doner.
            if params.get("id") and int(params["id"]) != LEAGUE_ID:
                return self._wrap([])
            if self.leagues_rows is not None:
                return self._wrap(self.leagues_rows)
            return self._wrap([
                {
                    "league": {"id": LEAGUE_ID, "name": "Premier League", "logo": "x.png"},
                    "country": {"name": "England"},
                    "seasons": [
                        {"year": SEASON - 1, "current": False},
                        {"year": SEASON, "current": True},
                    ],
                }
            ])

        if path.endswith("/fixtures"):
            if self._season_blocked(params):
                return self._plan_error()
            if params.get("league") and int(params["league"]) != LEAGUE_ID:
                return self._wrap([])
            if params.get("id"):
                wanted = int(params["id"])
                for row in self._upcoming():
                    if row["fixture"]["id"] == wanted:
                        return self._wrap([row])
                return self._wrap([])
            if params.get("status", "").startswith("FT"):
                return self._paged_history(int(params.get("page", 1)))
            return self._wrap(self._upcoming())

        if path.endswith("/fixtures/headtohead"):
            return self._wrap([
                _fixture(900, 10, "Team 10", 20, "Team 20",
                         datetime.now(tz=timezone.utc) - timedelta(days=200),
                         status="FT", goals={"home": 2, "away": 1}),
            ])

        if path.endswith("/odds"):
            if self._season_blocked(params):
                return self._plan_error()
            book_param = params.get("bookmaker")
            rows = []
            books = []
            if self.bet365_present and (book_param is None or int(book_param) == BET365_ID):
                books.append({"id": BET365_ID, "name": "Bet365", "bets": self._bet365_bets()})
            if book_param is None:
                books.append({"id": OTHER_ID, "name": "Bwin", "bets": OTHER_BETS})
            if books:
                rows.append({
                    "fixture": {"id": 1, "date": KICKOFF_1.isoformat()},
                    "update": "2026-09-07T09:00:00+00:00",
                    "bookmakers": books,
                })
            # 2 numarali mac icin Bet365 YOK - sadece Bwin var.
            rows.append({
                "fixture": {"id": 2, "date": KICKOFF_2.isoformat()},
                "update": "2026-09-07T09:00:00+00:00",
                "bookmakers": [{"id": OTHER_ID, "name": "Bwin", "bets": OTHER_BETS}],
            })
            return self._wrap(rows)

        if path.endswith("/injuries"):
            return self._wrap([
                {"team": {"id": 10}, "player": {"name": "A", "type": "Missing Fixture",
                                                "reason": "Injury"}},
            ])

        if path.endswith("/predictions"):
            return self._wrap([
                {"predictions": {"winner": {"name": "Team 10"}, "advice": "Double chance",
                                 "percent": {"home": "55%", "draw": "25%", "away": "20%"}}}
            ])

        return self._wrap([])

    def _season_blocked(self, params: dict) -> bool:
        season = params.get("season")
        return bool(season) and int(season) in self.blocked_seasons

    @staticmethod
    def _plan_error() -> httpx.Response:
        return FakeApi._wrap(
            [], errors={"plan": "Free plans do not have access to this season, from 2022"}
        )

    def _bet365_bets(self):
        bets = [dict(item) for item in BET365_BETS]
        bets[0] = _bet("Match Winner",
                       [("Home", self.home_odd), ("Draw", "3.40"), ("Away", "3.20")])
        return bets

    def _upcoming(self):
        return [
            _fixture(1, 10, "Team 10", 20, "Team 20", KICKOFF_1),
            _fixture(2, 30, "Team 30", 40, "Team 40", KICKOFF_2),
        ]

    def _paged_history(self, page: int) -> httpx.Response:
        rows = _history()
        half = len(rows) // 2
        chunk = rows[:half] if page == 1 else rows[half:]
        return self._wrap(chunk, paging={"current": page, "total": self.history_pages})

    @staticmethod
    def _wrap(response, errors=None, paging=None) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "get": "test",
                "parameters": {},
                "errors": errors if errors is not None else [],
                "results": len(response),
                "paging": paging or {"current": 1, "total": 1},
                "response": response,
            },
            headers={
                "x-ratelimit-requests-limit": "7500",
                "x-ratelimit-requests-remaining": "7421",
                "X-RateLimit-Remaining": "295",
            },
        )


class BaseCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        # Testler ortamdan BAGIMSIZ olmali: ihtiyac duydugumuz her degiskeni
        # burada acikca set ediyoruz. (Onceki surumde preflight'in
        # SNAPSHOT_ENABLED=false ayari alt surece sizip snapshot testini
        # dusuruyordu.)
        self._saved_env = {
            key: os.environ.get(key)
            for key in (
                "API_FOOTBALL_KEY", "SQLITE_PATH", "LEAGUE_IDS",
                "DATABASE_URL", "SNAPSHOT_ENABLED", "SNAPSHOT_MIN_GAP_MINUTES",
            )
        }
        os.environ["API_FOOTBALL_KEY"] = "test-key-not-real"
        os.environ["SQLITE_PATH"] = self.tmp.name
        os.environ["LEAGUE_IDS"] = str(LEAGUE_ID)
        os.environ["SNAPSHOT_ENABLED"] = "true"
        os.environ["SNAPSHOT_MIN_GAP_MINUTES"] = "30"
        os.environ.pop("DATABASE_URL", None)
        self.settings = config_module.get_settings(refresh=True)

        self.api = FakeApi()
        transport = httpx.MockTransport(self.api.handler)
        http = httpx.AsyncClient(
            base_url=self.settings.api_base_url,
            transport=transport,
            headers={"x-apisports-key": self.settings.api_key},
        )
        self.database = Database(self.settings)
        self.database.init_schema()
        database_module._db = self.database
        self.client = ApiFootballClient(self.settings, self.database, client=http)
        self.resolver = BookmakerResolver(self.client, self.database, self.settings)
        self.service = MatchService(self.client, self.database, self.resolver, self.settings)

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
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

    async def load(self):
        return await self.service.list_matches([LEAGUE_ID], TODAY, TODAY + timedelta(days=3))


class TestClientBasics(BaseCase):
    async def test_key_is_sent_as_header_never_in_url(self) -> None:
        await self.client.bookmakers()
        path, params = self.api.calls[0]
        self.assertNotIn("key", params)
        self.assertNotIn("test-key-not-real", json.dumps(params))

    async def test_missing_key_raises_before_any_call(self) -> None:
        os.environ["API_FOOTBALL_KEY"] = ""
        settings = config_module.get_settings(refresh=True)
        client = ApiFootballClient(settings, self.database, client=self.client._client)
        with self.assertRaises(ApiKeyMissingError):
            await client.bookmakers()
        self.assertEqual(self.api.calls, [])

    async def test_quota_headers_are_read(self) -> None:
        await self.client.bookmakers()
        self.assertTrue(self.client.quota.known)
        self.assertEqual(self.client.quota.daily_limit, 7500)
        self.assertEqual(self.client.quota.daily_remaining, 7421)

    async def test_http_401_maps_to_invalid_key(self) -> None:
        self.api.fail_with = 401
        with self.assertRaises(ApiKeyInvalidError):
            await self.client.bookmakers()

    async def test_http_429_maps_to_quota(self) -> None:
        self.api.fail_with = 429
        with self.assertRaises(ApiQuotaExceededError):
            await self.client.bookmakers()

    async def test_body_error_token_maps_to_invalid_key(self) -> None:
        self.api.body_error = {"token": "Error/Missing application key."}
        with self.assertRaises(ApiKeyInvalidError):
            await self.client.bookmakers()

    async def test_body_error_requests_maps_to_quota(self) -> None:
        self.api.body_error = {"requests": "You have reached the request limit."}
        with self.assertRaises(ApiQuotaExceededError):
            await self.client.bookmakers()

    async def test_timeout_is_retried_then_succeeds(self) -> None:
        self.api.timeout_times = 1
        rows = await self.client.bookmakers()
        self.assertTrue(rows)

    async def test_timeout_beyond_retries_raises(self) -> None:
        self.api.timeout_times = 99
        with self.assertRaises(ApiTimeoutError):
            await self.client.bookmakers()

    async def test_cache_prevents_duplicate_calls(self) -> None:
        await self.client.bookmakers()
        first = len(self.api.calls)
        await self.client.bookmakers()
        self.assertEqual(len(self.api.calls), first)

    async def test_pagination_merges_all_pages(self) -> None:
        rows = await self.client.fixtures(league=LEAGUE_ID, season=SEASON, status="FT-AET-PEN")
        self.assertEqual(len(rows), len(_history()))
        pages = [params.get("page") for path, params in self.api.calls if path.endswith("/fixtures")]
        self.assertIn("2", pages)

    async def test_call_budget_stops_runaway_usage(self) -> None:
        budget = CallBudget(limit=1)
        await self.client.bookmakers(budget=budget)
        with self.assertRaises(CallBudgetExhaustedError):
            await self.client.league(LEAGUE_ID, budget=budget)
        self.assertTrue(budget.exhausted)

    async def test_season_is_discovered_not_hardcoded(self) -> None:
        season, info = await self.client.current_season(LEAGUE_ID)
        self.assertEqual(season, SEASON)
        self.assertEqual(info["name"], "Premier League")


class TestSeasonDetection(BaseCase):
    """Sezon tespiti: 'current' bayragina KOR GUVENME.

    API'nin ``current: true`` bayragi sezon gecislerinde gecikebiliyor. Yanlis
    sezonla ``/fixtures?league&season&from&to`` cagrisi hata vermez, sessizce
    0 mac dondurur - ekranda "bugun mac yok" gibi gorunur. Bu yuzden once
    ``start <= bugun <= end`` kurali uygulanir.

    Testler bugunun tarihine GORECELI araliklar kurar; boylece takvimden
    bagimsizdir.
    """

    def _install_seasons(self, seasons: list[dict]) -> None:
        self.api.leagues_rows = [{
            "league": {"id": LEAGUE_ID, "name": "Premier League"},
            "country": {"name": "England"},
            "seasons": seasons,
        }]

    @staticmethod
    def _span(start_offset: int, end_offset: int) -> tuple[str, str]:
        today = _real_date.today()
        return (
            (today + timedelta(days=start_offset)).isoformat(),
            (today + timedelta(days=end_offset)).isoformat(),
        )

    async def test_date_range_wins_over_stale_current_flag(self) -> None:
        covering_start, covering_end = self._span(-40, 250)   # bugunu KAPSAR
        past_start, past_end = self._span(-400, -100)         # gecmis sezon
        self._install_seasons([
            {"year": 2025, "start": past_start, "end": past_end, "current": True},
            {"year": 2026, "start": covering_start, "end": covering_end, "current": False},
        ])
        season, info = await self.client.current_season(LEAGUE_ID, use_cache=False)
        self.assertEqual(season, 2026, "gecikmis 'current' bayragi takip edilmis")
        self.assertIn("tarih", info["season_reason"])

    async def test_current_flag_used_when_no_season_covers_today(self) -> None:
        past_start, past_end = self._span(-400, -100)
        future_start, future_end = self._span(100, 400)
        self._install_seasons([
            {"year": 2025, "start": past_start, "end": past_end, "current": False},
            {"year": 2026, "start": future_start, "end": future_end, "current": True},
        ])
        season, info = await self.client.current_season(LEAGUE_ID, use_cache=False)
        self.assertEqual(season, 2026)
        self.assertIn("current", info["season_reason"])

    async def test_falls_back_to_latest_year_without_dates_or_flag(self) -> None:
        self._install_seasons([
            {"year": 2024, "current": False},
            {"year": 2025, "current": False},
        ])
        season, info = await self.client.current_season(LEAGUE_ID, use_cache=False)
        self.assertEqual(season, 2025)
        self.assertIn("son care", info["season_reason"])

    async def test_info_exposes_seasons_for_diagnosis(self) -> None:
        covering_start, covering_end = self._span(-40, 250)
        self._install_seasons([
            {"year": 2026, "start": covering_start, "end": covering_end, "current": True},
        ])
        _, info = await self.client.current_season(LEAGUE_ID, use_cache=False)
        self.assertTrue(info["seasons_tail"])
        self.assertIn("start", info["seasons_tail"][0])
        self.assertEqual(info["season_count"], 1)

    async def test_missing_league_reports_reason(self) -> None:
        season, info = await self.client.current_season(99999, use_cache=False)
        self.assertIsNone(season)
        self.assertFalse(info["found"])
        self.assertIn("reason", info)


class TestBookmakerDiscovery(BaseCase):
    async def test_discovers_bet365_id_at_runtime(self) -> None:
        ref = await self.resolver.resolve()
        self.assertEqual(ref.id, BET365_ID)
        self.assertEqual(ref.name, "Bet365")

    async def test_missing_bet365_is_an_explicit_error(self) -> None:
        self.api.bet365_present = False
        with self.assertRaises(BookmakerNotFoundError):
            await self.resolver.resolve()
        ref, error = await self.resolver.try_resolve()
        self.assertIsNone(ref)
        self.assertIn("bulunamadi", error or "")


class TestMatchPipeline(BaseCase):
    async def test_matches_are_built_with_provenance(self) -> None:
        summaries, _, budget, warnings, bookmaker = await self.load()
        self.assertEqual(len(summaries), 2)
        self.assertEqual(bookmaker.id, BET365_ID)  # type: ignore[union-attr]
        by_id = {item.fixture_id: item for item in summaries}

        with_odds = by_id[1]
        self.assertTrue(with_odds.bet365.available)
        self.assertEqual(with_odds.bet365.bookmaker_name, "Bet365")
        self.assertAlmostEqual(with_odds.bet365.home, 2.10)
        self.assertEqual(with_odds.bet365.source, "api")

    async def test_match_without_bet365_is_marked_not_filled(self) -> None:
        summaries, _, _, _, _ = await self.load()
        without = {item.fixture_id: item for item in summaries}[2]
        self.assertFalse(without.bet365.available)
        self.assertIsNone(without.bet365.home)
        self.assertIn("mevcut degil", without.bet365.reason or "")
        self.assertFalse(without.implied.available)
        self.assertFalse(without.value.available)

    async def test_other_bookmaker_odds_never_appear(self) -> None:
        summaries, _, _, _, _ = await self.load()
        payload = json.dumps([item.model_dump(mode="json") for item in summaries])
        self.assertNotIn("9.99", payload)
        self.assertNotIn("Bwin", payload)

    async def test_implied_and_overround_are_computed(self) -> None:
        summaries, _, _, _, _ = await self.load()
        match = {item.fixture_id: item for item in summaries}[1]
        self.assertTrue(match.implied.available)
        self.assertGreater(match.implied.overround, 1.0)
        self.assertAlmostEqual(sum(match.implied.normalized.values()), 1.0, places=6)

    async def test_model_runs_and_markets_are_consistent(self) -> None:
        summaries, _, _, _, _ = await self.load()
        match = {item.fixture_id: item for item in summaries}[1]
        self.assertTrue(match.model.available)
        self.assertAlmostEqual(sum(match.model.match_result.probabilities.values()), 1.0, places=3)
        self.assertGreater(match.model.expected_goals_home, 0)

    async def test_stronger_team_gets_higher_probability(self) -> None:
        summaries, _, _, _, _ = await self.load()
        match = {item.fixture_id: item for item in summaries}[1]
        probabilities = match.model.match_result.probabilities
        self.assertGreater(probabilities["1"], probabilities["2"])

    async def test_snapshot_is_written_and_movement_appears_after_change(self) -> None:
        await self.load()
        stats = self.database.snapshot_stats()
        self.assertGreaterEqual(stats["total"], 1)

        summaries, _, _, _, _ = await self.load()
        first = {item.fixture_id: item for item in summaries}[1]
        # Tek snapshot varken hareket URETILMEZ.
        self.assertFalse(first.odds_movement.available)
        self.assertEqual(first.odds_movement.change_pct, {})

        # Oran degisti + cache temizlendi -> ikinci snapshot
        self.api.home_odd = "1.90"
        self.client._memory.clear()
        self.database.execute("DELETE FROM api_cache")
        summaries, _, _, _, _ = await self.load()
        second = {item.fixture_id: item for item in summaries}[1]
        self.assertTrue(second.odds_movement.available)
        self.assertEqual(second.odds_movement.snapshot_count, 2)
        self.assertLess(second.odds_movement.change_pct["1"], 0)
        self.assertEqual(second.odds_movement.direction["1"], "dustu")

    async def test_no_bet365_warns_and_leaves_all_matches_unpriced(self) -> None:
        self.api.bet365_present = False
        summaries, _, _, warnings, bookmaker = await self.load()
        self.assertIsNone(bookmaker)
        self.assertTrue(any("bulunamadi" in w for w in warnings))
        self.assertTrue(all(not item.bet365.available for item in summaries))

    async def test_top5_excludes_matches_without_odds(self) -> None:
        _, analyses, _, _, _ = await self.load()
        top, excluded = self.service.top_matches(analyses)
        self.assertTrue(all(item.bet365.available for item in top))
        self.assertGreaterEqual(excluded["bet365_yok"], 1)

    async def test_highlights_are_backed_by_real_matches(self) -> None:
        summaries, _, _, _, _ = await self.load()
        cards = build_highlights(summaries)
        self.assertEqual(len(cards), 7)
        for card in cards:
            if card.available:
                self.assertIsNotNone(card.fixture_id)
                self.assertIsNotNone(card.value)
            else:
                self.assertIsNone(card.fixture_id)

    async def test_call_count_stays_reasonable(self) -> None:
        _, _, budget, _, _ = await self.load()
        # 1 bookmaker + 1 lig + 1 fikstur + 2 gecmis sayfasi + gun basina oran
        self.assertLessEqual(budget.used, 12)

    async def test_prediction_is_logged_once_for_calibration(self) -> None:
        await self.load()
        await self.load()
        rows = self.database.query("SELECT * FROM predictions_log WHERE fixture_id = 1")
        self.assertEqual(len(rows), 1)


class TestFatalErrorsAreNotSwallowed(BaseCase):
    """Yapilandirma/kimlik hatalari uyariya donusturulup yutulamaz.

    Regresyon testi: eski surumde anahtar yokken ``list_matches`` sessizce
    bos liste + uyari donuyordu; kullanici bunu "bugun mac yok" sanabiliyordu.
    """

    async def test_missing_key_raises_instead_of_returning_empty_list(self) -> None:
        os.environ["API_FOOTBALL_KEY"] = ""
        settings = config_module.get_settings(refresh=True)
        service = MatchService(
            ApiFootballClient(settings, self.database, client=self.client._client),
            self.database,
            BookmakerResolver(
                ApiFootballClient(settings, self.database, client=self.client._client),
                self.database,
                settings,
            ),
            settings,
        )
        with self.assertRaises(ApiKeyMissingError):
            await service.list_matches([LEAGUE_ID], TODAY, TODAY + timedelta(days=1))

    async def test_invalid_key_raises(self) -> None:
        self.api.fail_with = 401
        with self.assertRaises(ApiKeyInvalidError):
            await self.load()

    async def test_forbidden_plan_raises(self) -> None:
        self.api.fail_with = 403
        with self.assertRaises(ApiForbiddenError):
            await self.load()

    async def test_resolver_reraises_fatal_but_swallows_missing_bookmaker(self) -> None:
        self.api.bet365_present = False
        reference, error = await self.resolver.try_resolve()
        self.assertIsNone(reference)
        self.assertIsNotNone(error)  # fatal degil -> yutulur

        self.api.fail_with = 401
        self.client._memory.clear()
        self.database.execute("DELETE FROM api_cache")
        with self.assertRaises(ApiKeyInvalidError):
            await self.resolver.try_resolve()  # fatal -> yutulmaz

    async def test_total_failure_raises_instead_of_empty_result(self) -> None:
        """Tek lig var ve o da alinamiyorsa bos liste degil, sebep donmeli."""
        await self.resolver.resolve()          # bookmaker cache'e girsin
        self.api.fail_with = 500               # fatal degil, ama her sey basarisiz
        self.client._memory.clear()
        self.database.execute("DELETE FROM api_cache")
        with self.assertRaises(ApiUnavailableError):
            await self.load()

    async def test_partial_failure_keeps_real_data(self) -> None:
        """Bir lig calisiyorsa sonuc doner; sorun uyari olarak bildirilir."""
        os.environ["LEAGUE_IDS"] = f"{LEAGUE_ID},99999"
        settings = config_module.get_settings(refresh=True)
        service = MatchService(self.client, self.database, self.resolver, settings)
        summaries, _, _, warnings, _ = await service.list_matches(
            [LEAGUE_ID, 99999], TODAY, TODAY + timedelta(days=3)
        )
        self.assertEqual(len(summaries), 2)     # calisan ligin maclari duruyor
        self.assertTrue(any("99999" in w for w in warnings))

    async def test_quota_error_is_not_fatal(self) -> None:
        from errors import ApiQuotaExceededError, CallBudgetExhaustedError

        self.assertFalse(ApiQuotaExceededError.fatal)
        self.assertFalse(CallBudgetExhaustedError.fatal)
        self.assertTrue(ApiKeyMissingError.fatal)
        self.assertTrue(ApiKeyInvalidError.fatal)
        self.assertTrue(ApiForbiddenError.fatal)


class TestPlanSeasonLimitation(BaseCase):
    """Ucretsiz plan guncel sezona erisim vermiyorsa ne olur?

    Gercek API davranisi: HTTP 200 + {"errors": {"plan": "Free plans do not
    have access to this season, from 2022"}}.

    Sart: uygulama bunu BOS LISTE gibi gostermez, sahte veri uretmez ve
    kullaniciya saglayicinin kendi mesajini iletir.
    """

    def test_body_error_maps_to_season_not_accessible(self) -> None:
        from errors import error_from_payload

        error = error_from_payload(
            {"plan": "Free plans do not have access to this season, from 2022"}
        )
        self.assertIsInstance(error, SeasonNotAccessibleError)
        self.assertEqual(error.code, "season_not_accessible")
        self.assertFalse(error.fatal, "plan kisiti fatal olmamali (kismi veri mumkun)")
        self.assertIn("this season", error.provider_message or "")

    def test_endpoint_scope_error_is_still_forbidden(self) -> None:
        """Sezon kisiti ile endpoint kapsami kisiti karistirilmamali."""
        from errors import error_from_payload

        error = error_from_payload({"plan": "Your plan does not allow this endpoint"})
        self.assertIsInstance(error, ApiForbiddenError)
        self.assertNotIsInstance(error, SeasonNotAccessibleError)

    async def test_blocked_season_raises_instead_of_empty_list(self) -> None:
        self.api.blocked_seasons = {SEASON}
        with self.assertRaises(SeasonNotAccessibleError) as ctx:
            await self.load()
        error = ctx.exception
        self.assertEqual(error.season, SEASON)
        self.assertIn(str(SEASON), error.user_message)
        self.assertIn("this season", error.provider_message or "")

    async def test_error_payload_carries_provider_message_and_hint(self) -> None:
        self.api.blocked_seasons = {SEASON}
        try:
            await self.load()
            self.fail("hata bekleniyordu")
        except SeasonNotAccessibleError as error:
            payload = error.to_payload()["error"]
            self.assertEqual(payload["code"], "season_not_accessible")
            self.assertIn("this season", payload["provider_message"])
            self.assertIn("Plan yukseltilirse", payload["hint"])
            self.assertEqual(payload["season"], SEASON)

    async def test_no_fabricated_matches_when_season_blocked(self) -> None:
        self.api.blocked_seasons = {SEASON}
        with self.assertRaises(SeasonNotAccessibleError):
            await self.load()
        # Snapshot tablosuna da hicbir sey yazilmamali.
        self.assertEqual(self.database.snapshot_stats()["total"], 0)

    async def test_partial_access_still_returns_real_data(self) -> None:
        """Bir lig erisilebilirse sonuc doner; engellenen lig uyari olur."""
        os.environ["LEAGUE_IDS"] = f"{LEAGUE_ID},77"
        settings = config_module.get_settings(refresh=True)
        service = MatchService(self.client, self.database, self.resolver, settings)
        # 77 numarali lig FakeApi'de yok -> bos doner, hata degil.
        summaries, _, _, warnings, _ = await service.list_matches(
            [LEAGUE_ID, 77], TODAY, TODAY + timedelta(days=3)
        )
        self.assertEqual(len(summaries), 2)

    async def test_season_accessibility_probe(self) -> None:
        self.api.blocked_seasons = {SEASON}
        ok, message = await self.client.season_is_accessible(LEAGUE_ID, SEASON)
        self.assertFalse(ok)
        self.assertIn("this season", message or "")

        ok2, message2 = await self.client.season_is_accessible(LEAGUE_ID, SEASON - 1)
        self.assertTrue(ok2)
        self.assertIsNone(message2)

    async def test_newest_accessible_season_walks_back(self) -> None:
        self.api.blocked_seasons = {SEASON, SEASON - 1}
        access = await self.client.newest_accessible_season(
            LEAGUE_ID, [SEASON, SEASON - 1, SEASON - 2, SEASON - 3]
        )
        self.assertEqual(access["newest_accessible_season"], SEASON - 2)
        self.assertIn("this season", access["provider_message"] or "")
        tried = {row["season"]: row["accessible"] for row in access["tried"]}
        self.assertFalse(tried[SEASON])
        self.assertTrue(tried[SEASON - 2])

    async def test_newest_accessible_season_reports_none_when_all_blocked(self) -> None:
        self.api.blocked_seasons = {SEASON, SEASON - 1, SEASON - 2, SEASON - 3}
        access = await self.client.newest_accessible_season(
            LEAGUE_ID, [SEASON, SEASON - 1, SEASON - 2, SEASON - 3]
        )
        self.assertIsNone(access["newest_accessible_season"])

    async def test_bet365_rule_intact_under_plan_limit(self) -> None:
        """Plan kisiti Bet365-only kuralini gevsetmemeli."""
        self.api.blocked_seasons = {SEASON}
        with self.assertRaises(SeasonNotAccessibleError):
            await self.load()
        rows = self.database.query("SELECT * FROM odds_snapshots")
        self.assertEqual(rows, [])
        # Bet365 kesfi hala calisiyor olmali (bookmakers plan kisitindan etkilenmez)
        reference, error = await self.resolver.try_resolve()
        self.assertIsNotNone(reference)
        self.assertEqual(reference.name, "Bet365")  # type: ignore[union-attr]


class TestMatchDetail(BaseCase):
    async def test_detail_includes_deep_blocks(self) -> None:
        detail = await self.service.match_detail(1)
        self.assertEqual(detail.fixture_id, 1)
        self.assertTrue(detail.h2h.available)
        self.assertTrue(detail.injuries.available)
        self.assertTrue(detail.api_prediction.available)
        self.assertEqual(detail.api_prediction.percent["1"], 0.55)
        self.assertTrue(detail.home_form.available)
        self.assertTrue(detail.explanation)

    async def test_api_prediction_is_separate_from_our_model(self) -> None:
        detail = await self.service.match_detail(1)
        self.assertIn("bizim modelimiz degildir", detail.api_prediction.note or "")
        self.assertNotEqual(
            detail.model.match_result.probabilities.get("1"),
            detail.api_prediction.percent.get("1"),
        )

    async def test_injuries_do_not_move_the_model(self) -> None:
        detail = await self.service.match_detail(1)
        self.assertIn("gol beklentisini degistirmez", detail.injuries.note or "")

    async def test_unknown_fixture_raises_not_found(self) -> None:
        from errors import FixtureNotFoundError

        with self.assertRaises(FixtureNotFoundError):
            await self.service.match_detail(999999)


if __name__ == "__main__":
    unittest.main(verbosity=2)
