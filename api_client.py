"""API-Football v3 istemcisi.

Sorumluluklari:

* Kimlik dogrulama (yalnizca ``x-apisports-key`` HEADER'i ile - anahtar
  asla URL'ye, loga veya cevaba yazilmaz)
* Cache (bellek + veritabani), boylece ayni veri tekrar tekrar cekilmez
* Pagination: ``paging.total > 1`` ise butun sayfalar birlestirilir
* Kota takibi: cevap header'larindan kalan istek sayisi okunur
* Hata haritalamasi: 401/403/429/timeout -> anlasilir Turkce mesaj

API-Football HTTP 200 icinde de hata dondurebildigi icin (``errors`` alani)
her cevabin govdesi ayrica kontrol edilir.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Iterable, Mapping

import httpx

from config import Settings, get_settings
from database import Database, get_database, utcnow
from errors import (
    ApiKeyMissingError,
    ApiQuotaExceededError,
    ApiTimeoutError,
    ApiUnavailableError,
    AppError,
    CallBudgetExhaustedError,
    SeasonNotAccessibleError,
    error_from_payload,
    error_from_status,
)

logger = logging.getLogger(__name__)

MAX_PAGES = 20  # guvenlik tavani; API'nin sayfa sayisi patlarsa sonsuz donmeyelim


@dataclass
class QuotaState:
    """Cevap header'larindan okunan kota bilgisi."""

    known: bool = False
    daily_limit: int | None = None
    daily_remaining: int | None = None
    per_minute_remaining: int | None = None
    calls_made: int = 0
    #: Kota bilgisi CANLI bir cevabin header'indan mi geldi? Cache'ten donen
    #: sonuclarda header yoktur; bu yuzden kota "bilinmiyor" gorunebilir.
    from_live_call: bool = False

    def as_dict(self, note: str | None = None) -> dict[str, Any]:
        return {
            "known": self.known,
            "from_live_call": self.from_live_call,
            "daily_limit": self.daily_limit,
            "daily_remaining": self.daily_remaining,
            "per_minute_remaining": self.per_minute_remaining,
            "calls_this_request": self.calls_made,
            "note": note,
        }


@dataclass
class CallRecord:
    """Tek bir API cagrisinin teshis kaydi.

    Bir sorunun "bizim kodumuz mu, API mi" sorusunu cevaplayabilmek icin ham
    HTTP kodu, gonderilen parametreler ve cevabin ozeti saklanir.
    """

    path: str
    params: dict[str, Any]
    from_cache: bool = False
    status: int | None = None
    results: int | None = None
    pages: int | None = None
    error_code: str | None = None
    error_detail: str | None = None
    quota_headers_seen: bool = False

    def describe(self) -> str:
        if self.from_cache:
            return f"{self.path} {self.params} -> CACHE ({self.results} kayit)"
        status = self.status if self.status is not None else "-"
        if self.error_code:
            return (
                f"{self.path} {self.params} -> HTTP {status} / {self.error_code}"
                f" :: {(self.error_detail or '')[:220]}"
            )
        return f"{self.path} {self.params} -> HTTP {status}, {self.results} kayit"


@dataclass
class CallBudget:
    """Tek bir yenileme icin cagri butcesi.

    Kotanin tek bir istekle tukenmesini engeller.
    """

    limit: int
    used: int = 0
    exhausted: bool = False
    reasons: list[str] = field(default_factory=list)

    def allow(self) -> bool:
        if self.used >= self.limit:
            if not self.exhausted:
                self.exhausted = True
                self.reasons.append(
                    f"Bu yenileme icin ayrilan {self.limit} API cagrisi doldu; "
                    "kalan maclar derinlemesine analiz edilmedi."
                )
            return False
        return True

    def spend(self, count: int = 1) -> None:
        self.used += count


class _MemoryCache:
    """Surec ici TTL cache. DB cache'in onunde durur."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at, payload = entry
        if expires_at <= time.time():
            self._data.pop(key, None)
            return None
        return payload

    def set(self, key: str, payload: Any, ttl: int) -> None:
        self._data[key] = (time.time() + ttl, payload)

    def clear(self) -> None:
        self._data.clear()


class ApiFootballClient:
    """API-Football v3 icin async istemci."""

    def __init__(
        self,
        settings: Settings | None = None,
        database: Database | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.database = database if database is not None else get_database(self.settings)
        self._client = client
        self._owns_client = client is None
        self._memory = _MemoryCache()
        self.quota = QuotaState()
        #: Son cagrilarin teshis kayitlari (probe_api bunlari raporlar).
        self.history: list[CallRecord] = []
        self.max_history = 60

    # ------------------------------------------------------------------
    # Yasam dongusu
    # ------------------------------------------------------------------
    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.api_base_url.rstrip("/"),
                timeout=httpx.Timeout(self.settings.api_timeout),
                headers={
                    # Anahtar YALNIZCA burada. URL'ye veya loga girmez.
                    "x-apisports-key": self.settings.api_key,
                    "Accept": "application/json",
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "ApiFootballClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Cache anahtari
    # ------------------------------------------------------------------
    @staticmethod
    def cache_key(path: str, params: Mapping[str, Any] | None) -> str:
        cleaned = {
            key: value
            for key, value in sorted((params or {}).items())
            if value is not None and value != ""
        }
        return f"{path}?{json.dumps(cleaned, sort_keys=True, ensure_ascii=False)}"

    # ------------------------------------------------------------------
    # Kota
    # ------------------------------------------------------------------
    def _update_quota(self, headers: httpx.Headers) -> bool:
        def read(*names: str) -> int | None:
            for name in names:
                raw = headers.get(name)
                if raw is None:
                    continue
                try:
                    return int(float(raw))
                except (TypeError, ValueError):
                    continue
            return None

        daily_limit = read("x-ratelimit-requests-limit")
        daily_remaining = read("x-ratelimit-requests-remaining")
        minute_remaining = read("x-ratelimit-remaining", "X-RateLimit-Remaining")

        if daily_limit is not None:
            self.quota.daily_limit = daily_limit
        if daily_remaining is not None:
            self.quota.daily_remaining = daily_remaining
        if minute_remaining is not None:
            self.quota.per_minute_remaining = minute_remaining
        seen = any(
            value is not None for value in (daily_limit, daily_remaining, minute_remaining)
        )
        if seen:
            self.quota.known = True
            self.quota.from_live_call = True
        return seen

    @property
    def quota_exhausted(self) -> bool:
        remaining = self.quota.daily_remaining
        return remaining is not None and remaining <= 0

    def quota_low(self) -> bool:
        remaining = self.quota.daily_remaining
        return remaining is not None and remaining <= self.settings.quota_reserve

    # ------------------------------------------------------------------
    # Tek HTTP istegi
    # ------------------------------------------------------------------
    async def _request_once(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        client = await self._get_client()
        try:
            response = await client.get("/" + path.lstrip("/"), params=params)
        except httpx.TimeoutException as exc:
            raise ApiTimeoutError(detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ApiUnavailableError(detail=str(exc)) from exc

        self.quota.calls_made += 1
        headers_seen = self._update_quota(response.headers)

        record = CallRecord(
            path=path,
            params=dict(params),
            status=response.status_code,
            quota_headers_seen=headers_seen,
        )
        self._remember(record)

        if response.status_code != 200:
            # Govde loglanir ama kullaniciya ham hali gitmez.
            logger.warning(
                "API-Football %s -> HTTP %s", path, response.status_code
            )
            error = error_from_status(response.status_code, detail=response.text[:500])
            record.error_code = error.code
            record.error_detail = response.text[:500]
            error.upstream_status = response.status_code
            raise error

        try:
            payload = response.json()
        except ValueError as exc:
            record.error_code = "bad_json"
            record.error_detail = response.text[:300]
            raise ApiUnavailableError(
                "API-Football gecerli bir JSON dondurmedi.", detail=str(exc)
            ) from exc

        if not isinstance(payload, dict):
            record.error_code = "bad_body"
            raise ApiUnavailableError("API-Football beklenmeyen bir govde dondurdu.")

        record.results = len(payload.get("response") or [])
        record.pages = int((payload.get("paging") or {}).get("total") or 1)

        body_error = error_from_payload(payload.get("errors"))
        if body_error is not None:
            logger.warning("API-Football %s govde hatasi: %s", path, body_error.detail)
            record.error_code = body_error.code
            record.error_detail = body_error.detail
            body_error.upstream_status = response.status_code
            raise body_error

        return payload

    async def _request_with_retry(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        attempt = 0
        delay = 1.0
        while True:
            try:
                return await self._request_once(path, params)
            except (ApiTimeoutError, ApiUnavailableError) as exc:
                if attempt >= self.settings.api_max_retries:
                    raise
                attempt += 1
                logger.info(
                    "API-Football %s yeniden deneniyor (%s/%s): %s",
                    path,
                    attempt,
                    self.settings.api_max_retries,
                    exc.code,
                )
                await asyncio.sleep(delay)
                delay *= 2
            except ApiQuotaExceededError:
                raise  # kota hatasinda tekrar denemek kotayi daha da yakar

    # ------------------------------------------------------------------
    # Ana giris noktasi
    # ------------------------------------------------------------------
    async def get(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        ttl: int = 300,
        paginate: bool = True,
        budget: CallBudget | None = None,
        use_cache: bool = True,
    ) -> list[Any]:
        """Bir endpoint'i cagirir ve ``response`` listesini dondurur.

        Sayfalama varsa tum sayfalar birlestirilir.
        """
        if not self.settings.api_key_configured:
            raise ApiKeyMissingError()

        params = {
            key: value
            for key, value in (params or {}).items()
            if value is not None and value != ""
        }
        key = self.cache_key(path, params)

        if use_cache:
            cached = self._memory.get(key)
            if cached is not None:
                self._remember(CallRecord(path, dict(params), from_cache=True,
                                          results=len(cached)))
                return cached
            db_cached = self._safe_db_cache_get(key)
            if db_cached is not None:
                self._memory.set(key, db_cached, ttl)
                self._remember(CallRecord(path, dict(params), from_cache=True,
                                          results=len(db_cached)))
                return db_cached

        if budget is not None and not budget.allow():
            raise CallBudgetExhaustedError()
        if self.quota_exhausted:
            raise ApiQuotaExceededError()

        if budget is not None:
            budget.spend()

        payload = await self._request_with_retry(path, params)
        results: list[Any] = list(payload.get("response") or [])

        if paginate:
            paging = payload.get("paging") or {}
            total_pages = int(paging.get("total") or 1)
            current = int(paging.get("current") or 1)
            total_pages = min(total_pages, MAX_PAGES)
            for page in range(current + 1, total_pages + 1):
                if budget is not None and not budget.allow():
                    logger.warning(
                        "%s icin sayfalama yarida kesildi (butce doldu): %s/%s",
                        path,
                        page - 1,
                        total_pages,
                    )
                    break
                if budget is not None:
                    budget.spend()
                page_payload = await self._request_with_retry(path, {**params, "page": page})
                results.extend(page_payload.get("response") or [])

        if use_cache and ttl > 0:
            self._memory.set(key, results, ttl)
            self._safe_db_cache_set(key, results, ttl)

        logger.info("API-Football %s -> %s kayit", path, len(results))
        return results

    def _remember(self, record: CallRecord) -> None:
        self.history.append(record)
        if len(self.history) > self.max_history:
            del self.history[: len(self.history) - self.max_history]

    # Cache islemleri kritik degil; hata verirse istek yine de calismali.
    def _safe_db_cache_get(self, key: str) -> Any | None:
        try:
            return self.database.cache_get(key)
        except AppError:  # pragma: no cover
            return None

    def _safe_db_cache_set(self, key: str, payload: Any, ttl: int) -> None:
        try:
            self.database.cache_set(key, payload, utcnow() + timedelta(seconds=ttl))
        except AppError:  # pragma: no cover
            logger.debug("Cache yazilamadi: %s", key)

    # ------------------------------------------------------------------
    # Endpoint kisayollari
    # ------------------------------------------------------------------
    async def bookmakers(
        self, budget: CallBudget | None = None, use_cache: bool = True
    ) -> list[dict[str, Any]]:
        return await self.get(
            "odds/bookmakers", ttl=self.settings.ttl.bookmakers,
            budget=budget, use_cache=use_cache,
        )

    async def bets(self, budget: CallBudget | None = None) -> list[dict[str, Any]]:
        return await self.get("odds/bets", ttl=self.settings.ttl.bets, budget=budget)

    async def league(
        self, league_id: int, budget: CallBudget | None = None, use_cache: bool = True
    ) -> list[dict[str, Any]]:
        return await self.get(
            "leagues", {"id": league_id}, ttl=self.settings.ttl.leagues,
            budget=budget, use_cache=use_cache,
        )

    async def fixtures(
        self,
        *,
        league: int | None = None,
        season: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        date: str | None = None,
        team: int | None = None,
        last: int | None = None,
        next_count: int | None = None,
        status: str | None = None,
        fixture_id: int | None = None,
        budget: CallBudget | None = None,
        ttl: int | None = None,
        use_cache: bool = True,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "league": league,
            "season": season,
            "from": date_from,
            "to": date_to,
            "date": date,
            "team": team,
            "last": last,
            # API-Football'in "next" parametresi: sezon/tarih filtresi OLMADAN
            # ligin siradaki maclarini verir. Sezon tespitini dogrulamak icin
            # en kesin kontroldur.
            "next": next_count,
            "status": status,
            "id": fixture_id,
            "timezone": "UTC",  # tek dogruluk kaynagi UTC; goruntuleme TR saatinde
        }
        return await self.get(
            "fixtures",
            params,
            ttl=ttl if ttl is not None else self.settings.ttl.fixtures,
            budget=budget,
            use_cache=use_cache,
        )

    async def odds(
        self,
        *,
        fixture: int | None = None,
        league: int | None = None,
        season: int | None = None,
        date: str | None = None,
        bookmaker: int | None = None,
        bet: int | None = None,
        budget: CallBudget | None = None,
        use_cache: bool = True,
    ) -> list[dict[str, Any]]:
        params = {
            "fixture": fixture,
            "league": league,
            "season": season,
            "date": date,
            "bookmaker": bookmaker,
            "bet": bet,
            "timezone": "UTC",
        }
        return await self.get(
            "odds", params, ttl=self.settings.ttl.odds, budget=budget, use_cache=use_cache
        )

    async def predictions(
        self, fixture_id: int, budget: CallBudget | None = None
    ) -> list[dict[str, Any]]:
        return await self.get(
            "predictions",
            {"fixture": fixture_id},
            ttl=self.settings.ttl.predictions,
            budget=budget,
        )

    async def head_to_head(
        self,
        home_id: int,
        away_id: int,
        last: int = 10,
        budget: CallBudget | None = None,
    ) -> list[dict[str, Any]]:
        return await self.get(
            "fixtures/headtohead",
            {"h2h": f"{home_id}-{away_id}", "last": last, "timezone": "UTC"},
            ttl=self.settings.ttl.h2h,
            budget=budget,
        )

    async def team_statistics(
        self, team_id: int, league_id: int, season: int, budget: CallBudget | None = None
    ) -> list[dict[str, Any]]:
        return await self.get(
            "teams/statistics",
            {"team": team_id, "league": league_id, "season": season},
            ttl=self.settings.ttl.team_statistics,
            paginate=False,
            budget=budget,
        )

    async def injuries(
        self, fixture_id: int, budget: CallBudget | None = None
    ) -> list[dict[str, Any]]:
        return await self.get(
            "injuries",
            {"fixture": fixture_id},
            ttl=self.settings.ttl.injuries,
            budget=budget,
        )

    async def standings(
        self, league_id: int, season: int, budget: CallBudget | None = None
    ) -> list[dict[str, Any]]:
        return await self.get(
            "standings",
            {"league": league_id, "season": season},
            ttl=self.settings.ttl.standings,
            paginate=False,
            budget=budget,
        )

    # ------------------------------------------------------------------
    # Sezon tespiti
    # ------------------------------------------------------------------
    async def current_season(
        self, league_id: int, budget: CallBudget | None = None, use_cache: bool = True
    ) -> tuple[int | None, dict[str, Any]]:
        """Ligin ICINDE BULUNDUGUMUZ sezonunu API'den belirler.

        Sezon asla kodda sabitlenmez. Secim sirasi:

        1. ``start <= bugun <= end`` olan sezon  (EN GUVENILIR)
        2. ``current: true`` isaretli sezon
        3. En buyuk sezon yili

        Neden 1. kural once: API'nin ``current`` bayragi sezon gecislerinde
        gecikebiliyor. Yanlis sezonla ``/fixtures?league&season&from&to``
        cagrisi HATA VERMEZ, sessizce 0 mac dondurur - "bugun mac yok" gibi
        gorunen sinsi bir hata.
        """
        from datetime import date as _date

        rows = await self.league(league_id, budget=budget, use_cache=use_cache)
        if not rows:
            return None, {"league_id": league_id, "found": False,
                          "reason": "/leagues bos dondu"}

        row = rows[0]
        league_info = row.get("league") or {}
        country = (row.get("country") or {}).get("name")
        seasons = row.get("seasons") or []
        today = _date.today()

        def parse(value: Any) -> _date | None:
            try:
                return _date.fromisoformat(str(value)[:10])
            except (TypeError, ValueError):
                return None

        season_year: int | None = None
        reason = "sezon bilgisi yok"
        by_date: list[int] = []

        for season in seasons:
            start, end = parse(season.get("start")), parse(season.get("end"))
            year = season.get("year")
            if start and end and start <= today <= end and isinstance(year, int):
                by_date.append(year)

        if by_date:
            season_year = max(by_date)
            reason = "tarih araligi (start <= bugun <= end)"
        else:
            for season in seasons:
                if season.get("current") and isinstance(season.get("year"), int):
                    season_year = season["year"]
                    reason = "current: true bayragi"
                    break
        if season_year is None and seasons:
            years = [s.get("year") for s in seasons if isinstance(s.get("year"), int)]
            if years:
                season_year = max(years)
                reason = "en buyuk sezon yili (son care)"

        return season_year, {
            "league_id": league_id,
            "found": True,
            "name": league_info.get("name"),
            "country": country,
            "logo": league_info.get("logo"),
            "season": season_year,
            "season_reason": reason,
            "season_count": len(seasons),
            "seasons_tail": [
                {
                    "year": item.get("year"),
                    "start": item.get("start"),
                    "end": item.get("end"),
                    "current": item.get("current"),
                }
                for item in seasons[-4:]
            ],
        }


    # ------------------------------------------------------------------
    # Plan / sezon erisimi
    # ------------------------------------------------------------------
    async def season_is_accessible(
        self, league_id: int, season: int, budget: CallBudget | None = None
    ) -> tuple[bool, str | None]:
        """Plan bu sezonun VERISINE erisim veriyor mu?

        En ucuz gercek veri cagrisiyla olcer (``last=1``). Sezon listesi
        ``/leagues`` ucretsiz planda da donuyor; asil kisit veri
        endpointlerinde ortaya cikiyor, bu yuzden olcum orada yapilmali.

        Returns:
            (erisilebilir_mi, saglayicinin_mesaji)
        """
        try:
            await self.fixtures(
                league=league_id, season=season, last=1, budget=budget, use_cache=False
            )
            return True, None
        except SeasonNotAccessibleError as exc:
            return False, exc.provider_message or exc.detail
        except AppError:
            # Baska bir hata sezon erisimi hakkinda bilgi vermez.
            raise

    async def newest_accessible_season(
        self,
        league_id: int,
        seasons: Iterable[int],
        budget: CallBudget | None = None,
        max_tries: int = 6,
    ) -> dict[str, Any]:
        """Planin izin verdigi EN GUNCEL sezonu bulur (yeniden eskiye).

        Sonuc 24 saat cache'lenir. Bu YALNIZCA TESPIT icindir - bulunan eski
        sezon "bugunun maclari" olarak GOSTERILMEZ.
        """
        cache_key = f"season-access:{league_id}"
        cached = self._safe_db_cache_get(cache_key)
        if cached is not None:
            return cached

        years = sorted({int(year) for year in seasons if isinstance(year, int)}, reverse=True)
        tried: list[dict[str, Any]] = []
        accessible: int | None = None
        provider_message: str | None = None

        for year in years[:max_tries]:
            try:
                ok, message = await self.season_is_accessible(league_id, year, budget=budget)
            except AppError as exc:
                tried.append({"season": year, "accessible": None, "error": exc.code})
                break
            tried.append({"season": year, "accessible": ok,
                          "provider_message": message})
            if ok:
                accessible = year
                break
            if provider_message is None:
                provider_message = message

        result = {
            "league_id": league_id,
            "newest_accessible_season": accessible,
            "tried": tried,
            "provider_message": provider_message,
        }
        self._safe_db_cache_set(cache_key, result, self.settings.ttl.season_access)
        return result


def flatten(items: Iterable[Iterable[Any]]) -> list[Any]:
    return [item for group in items for item in group]
