"""MACANALIZ PRO - FastAPI uygulamasi.

Bu dosya YALNIZCA HTTP katmanidir: parametre dogrulama, yonlendirme, hata
cevirisi. Is mantigi ``services.py`` ve ``analysis_engine.py`` icindedir.

Render icin baslatma komutu:
    uvicorn backend:app --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Any, Sequence

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from api_client import ApiFootballClient, CallBudget
from bookmakers import BookmakerResolver
from config import APP_NAME, APP_VERSION, Settings, configure_logging, get_settings
from database import Database, get_database
from errors import AppError, ValidationError
from models import (
    HighlightCard,
    PlanAccess,
    MatchDetail,
    MatchesResponse,
    QuotaInfo,
    StatusResponse,
    Top5Response,
)
from scheduler import SnapshotScheduler
from services import (
    MatchService,
    build_highlights,
    default_date_range,
)
from snapshots import load_snapshots

logger = logging.getLogger(__name__)

MAX_RANGE_DAYS = 30

#: Backend/frontend sozlesme surumu. Frontend bunu /api/config'de arar; yoksa
#: veya tutmuyorsa "bu adreste baska bir uygulama calisiyor" der. Sozlesme
#: kirici degisikliklerde artirilir.
API_CONTRACT = "macanaliz-pro/1"


# ==========================================================================
# Uygulama yasam dongusu
# ==========================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings)
    logger.info("%s v%s baslatiliyor", APP_NAME, APP_VERSION)

    database = get_database(settings)
    try:
        database.init_schema()
    except AppError:
        logger.exception("Veritabani semasi olusturulamadi")

    client = ApiFootballClient(settings, database)
    resolver = BookmakerResolver(client, database, settings)
    service = MatchService(client, database, resolver, settings)
    scheduler = SnapshotScheduler(client, database, resolver, service, settings)

    app.state.settings = settings
    app.state.database = database
    app.state.client = client
    app.state.resolver = resolver
    app.state.service = service
    app.state.scheduler = scheduler

    if not settings.api_key_configured:
        logger.error(
            "API_FOOTBALL_KEY tanimli degil. Uygulama ayakta ama veri cekemez."
        )
    if not database.persistent:
        logger.warning(
            "Kalici olmayan veritabani (SQLite). Snapshot gecmisi restart'ta silinir."
        )

    await scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()
        await client.aclose()
        logger.info("%s kapatildi", APP_NAME)


app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    description=(
        "Futbol mac ve Bet365 oran analizi. Kazanc garantisi vermez, "
        "kesin sonuc iddia etmez, veri uydurmaz."
    ),
    lifespan=lifespan,
)

_settings_at_import = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings_at_import.allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    # X-Request-ID expose EDILMEZSE tarayici onu cross-origin okuyamaz ve
    # frontend'deki hata, Render logundaki satirla eslestirilemez.
    expose_headers=["X-Request-ID"],
)


# ==========================================================================
# Bagimliliklar
# ==========================================================================
def get_service(request: Request) -> MatchService:
    return request.app.state.service


def get_client(request: Request) -> ApiFootballClient:
    return request.app.state.client


def settings_dep() -> Settings:
    return get_settings()


# ==========================================================================
# Istek loglama - Render logunda TEK BIR istegi bulabilmek icin
# ==========================================================================
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Her istegi request-id ile loglar ve cevaba X-Request-ID ekler.

    Tarayicida bir hata gorulunce, ayni id ile Render logundaki satir
    bulunabilir. API anahtari loglanmaz (config.SecretRedactingFilter ayrica
    guvence saglar) ve query string'imiz sir icermez.
    """
    request_id = uuid.uuid4().hex[:12]
    started = time.perf_counter()
    query = request.url.query or "-"
    try:
        response = await call_next(request)
    except Exception:
        elapsed = (time.perf_counter() - started) * 1000
        logger.exception(
            "rid=%s %s %s ?%s -> ISTISNA (%.0f ms)",
            request_id, request.method, request.url.path, query, elapsed,
        )
        raise
    elapsed = (time.perf_counter() - started) * 1000
    log = logger.warning if response.status_code >= 400 else logger.info
    log(
        "rid=%s %s %s ?%s -> %s (%.0f ms)",
        request_id, request.method, request.url.path, query,
        response.status_code, elapsed,
    )
    response.headers["X-Request-ID"] = request_id
    return response


# ==========================================================================
# Hata yakalayicilar - ham traceback asla kullaniciya gitmez
# ==========================================================================
@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    logger.warning("%s -> %s (%s)", request.url.path, exc.code, exc.detail)
    status = exc.http_status if exc.http_status >= 400 else 400
    return JSONResponse(status_code=status, content=exc.to_payload())


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """404/405 gibi hatalar da ayni zarfla doner.

    Aksi halde FastAPI {"detail": "Not Found"} donduruyor ve frontend'de
    mesaj bulunamadigi icin "Sunucu 404 dondurdu" gibi bos bir metin
    goruluyordu - gercek sebep kayboluyordu.
    """
    messages = {
        404: (
            f"Bu backend '{request.url.path}' adresini tanimiyor. "
            "Adres yanlis olabilir ya da bu sunucuda BASKA bir uygulama calisiyor."
        ),
        405: f"'{request.method}' metodu bu adres icin desteklenmiyor.",
    }
    message = messages.get(exc.status_code, str(exc.detail))
    logger.warning("%s %s -> HTTP %s", request.method, request.url.path, exc.status_code)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": f"http_{exc.status_code}",
                "message": message,
                "path": request.url.path,
                "expected_contract": API_CONTRACT,
            }
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """FastAPI'nin 422'si de anlasilir bir mesaja cevrilir."""
    problems = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", []) if part != "query")
        problems.append(f"{location or 'parametre'}: {error.get('msg', 'gecersiz')}")
    message = "Gonderilen parametreler gecersiz. " + "; ".join(problems[:4])
    logger.warning("%s %s -> 422 %s", request.method, request.url.path, problems)
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "validation_error", "message": message,
                           "problems": problems}},
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Beklenmeyen hata: %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "internal_error",
                "message": "Beklenmeyen bir hata olustu. Sunucu loglarina bakin.",
            }
        },
    )


# ==========================================================================
# Parametre dogrulama
# ==========================================================================
def parse_league_ids(raw: str | None, settings: Settings) -> list[int]:
    if not raw or not raw.strip() or raw.strip().lower() in {"all", "tum", "tumu"}:
        return settings.league_ids
    result: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if not token.lstrip("-").isdigit():
            raise ValidationError(f"Gecersiz lig ID'si: {token}")
        value = int(token)
        if value <= 0:
            raise ValidationError(f"Gecersiz lig ID'si: {token}")
        result.append(value)
    if not result:
        raise ValidationError("En az bir lig secilmeli.")
    if len(result) > 20:
        raise ValidationError("En fazla 20 lig secilebilir.")
    return result


def parse_range(
    days: int | None, date_from: str | None, date_to: str | None, settings: Settings
) -> tuple[date, date]:
    if date_from or date_to:
        try:
            start = date.fromisoformat(date_from) if date_from else datetime.now(tz=settings.tzinfo).date()
            end = date.fromisoformat(date_to) if date_to else start
        except ValueError as exc:
            raise ValidationError("Tarih formati gecersiz. YYYY-AA-GG bekleniyor.") from exc
        if end < start:
            raise ValidationError("Bitis tarihi baslangictan once olamaz.")
        if (end - start).days + 1 > MAX_RANGE_DAYS:
            raise ValidationError(f"Tarih araligi en fazla {MAX_RANGE_DAYS} gun olabilir.")
        return start, end

    span = days if days is not None else 1
    if span < 1 or span > MAX_RANGE_DAYS:
        raise ValidationError(f"Gun araligi 1 ile {MAX_RANGE_DAYS} arasinda olmali.")
    return default_date_range(span, settings)


def quota_info(client: ApiFootballClient, note: str | None = None) -> QuotaInfo:
    return QuotaInfo(**client.quota.as_dict(note))


# ==========================================================================
# Saglik ve durum
# ==========================================================================
@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "app": APP_NAME,
        "version": APP_VERSION,
        "time": datetime.now(tz=get_settings().tzinfo).isoformat(),
    }


@app.get("/api/config")
async def read_config(settings: Settings = Depends(settings_dep)) -> dict[str, Any]:
    """Frontend'in ihtiyac duydugu yapilandirma. SIR ICERMEZ."""
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        # Frontend'in "bu adreste DOGRU backend mi var?" sorusunu
        # tahminle degil, acik bir isaretle cevaplamasi icin.
        "api_contract": API_CONTRACT,
        "timezone": settings.timezone_name,
        "bookmaker_name": settings.bookmaker_name,
        "leagues": [
            {"id": league.id, "slug": league.slug, "name": league.expected_name}
            for league in settings.leagues
        ],
        "day_options": [1, 2, 3, 7, 14],
        "top5_size": settings.top5_size,
        "snapshot_interval_minutes": settings.snapshot_interval_minutes,
        "max_range_days": MAX_RANGE_DAYS,
        "disclaimer": (
            "Bu uygulama kazanc garantisi vermez, kesin sonuc iddia etmez ve "
            "veri uydurmaz. Gosterilen olasiliklar istatistiksel bir modelin "
            "ciktisidir."
        ),
    }


@app.get("/api/status", response_model=StatusResponse)
async def read_status(request: Request) -> StatusResponse:
    """Bagli mi, anahtar var mi, Bet365 bulundu mu - hepsi ayri ayri.

    API ANAHTARININ KENDISI BU CEVAPTA ASLA YER ALMAZ.
    """
    state = request.app.state
    settings: Settings = state.settings
    client: ApiFootballClient = state.client
    database: Database = state.database
    resolver: BookmakerResolver = state.resolver
    scheduler: SnapshotScheduler = state.scheduler

    checks: list[dict[str, Any]] = [
        {"key": "backend", "label": "Backend", "ok": True, "detail": "Calisiyor"}
    ]
    warnings: list[str] = []

    api_connected = False
    bet365_available = False
    bookmaker_id: int | None = None
    leagues_info: list[dict[str, Any]] = []

    if not settings.api_key_configured:
        checks.append(
            {
                "key": "api_key",
                "label": "API anahtari",
                "ok": False,
                "detail": "API_FOOTBALL_KEY tanimli degil",
            }
        )
        warnings.append("Sunucuda API_FOOTBALL_KEY tanimli degil.")
    else:
        checks.append(
            {"key": "api_key", "label": "API anahtari", "ok": True, "detail": "Tanimli"}
        )
        budget = CallBudget(limit=12)
        try:
            bookmaker, error = await resolver.try_resolve(budget=budget)
            api_connected = True
            checks.append(
                {
                    "key": "api",
                    "label": "API-Football baglantisi",
                    "ok": True,
                    "detail": "Basarili",
                }
            )
            if bookmaker is not None:
                bet365_available = True
                bookmaker_id = bookmaker.id
                checks.append(
                    {
                        "key": "bookmaker",
                        "label": settings.bookmaker_name,
                        "ok": True,
                        "detail": f"Bulundu (id={bookmaker.id})",
                    }
                )
            else:
                checks.append(
                    {
                        "key": "bookmaker",
                        "label": settings.bookmaker_name,
                        "ok": False,
                        "detail": error or "Bulunamadi",
                    }
                )
                warnings.append(error or f"{settings.bookmaker_name} bulunamadi.")
        except AppError as exc:
            checks.append(
                {
                    "key": "api",
                    "label": "API-Football baglantisi",
                    "ok": False,
                    "detail": exc.user_message,
                }
            )
            warnings.append(exc.user_message)

        # Lig ID'lerini gercek API adiyla dogrula (yanlis ID sessiz kalmasin)
        for league in settings.leagues:
            try:
                context = await state.service.league_context(league.id, budget=budget)
                leagues_info.append(
                    {
                        "id": league.id,
                        "name": context.name,
                        "season": context.season,
                        "ok": context.usable and not context.warning,
                        "warning": context.warning,
                    }
                )
                if context.warning:
                    warnings.append(context.warning)
            except AppError as exc:
                leagues_info.append(
                    {"id": league.id, "name": league.expected_name, "ok": False,
                     "warning": exc.user_message}
                )

    # ---------------------------------------------------------------- plan
    # Ucretsiz planlar guncel sezona erisim vermiyor. Bunu ACIKCA raporla ki
    # kullanici "mac yok" ile "plan izin vermiyor" arasindaki farki gorsun.
    plan = PlanAccess()
    first_usable = next(
        (row for row in leagues_info if row.get("season") and row.get("ok") is not False),
        None,
    )
    if settings.api_key_configured and first_usable:
        season = int(first_usable["season"])
        league_id = int(first_usable["id"])
        candidates = [season, season - 1, season - 2, season - 3]
        try:
            access = await client.newest_accessible_season(
                league_id, candidates, budget=CallBudget(limit=8)
            )
            newest = access.get("newest_accessible_season")
            plan = PlanAccess(
                checked=True,
                season_access_ok=(newest == season),
                requested_season=season,
                league_id=league_id,
                provider_message=access.get("provider_message"),
                newest_accessible_season=newest,
                tried=access.get("tried") or [],
            )
            if plan.season_access_ok:
                plan.note = f"{season} sezonuna erisim var."
                checks.append({
                    "key": "plan", "label": "Plan / sezon erisimi", "ok": True,
                    "detail": f"{season} sezonu erisilebilir",
                })
            else:
                plan.note = (
                    "Guncel sezon verisi bu planla cekilemez. Uygulama eski sezonu "
                    "'bugunun maclari' olarak GOSTERMEZ. Plan yukseltilirse kod "
                    "degisikligi gerekmez; sezon her zaman API'den tespit edilir."
                )
                detail = f"{season} sezonuna erisim YOK"
                if newest:
                    detail += f" (erisilebilen en guncel sezon: {newest})"
                checks.append({
                    "key": "plan", "label": "Plan / sezon erisimi", "ok": False,
                    "detail": detail,
                })
                warnings.append(
                    f"API-Football planiniz {season} sezonuna erisim vermiyor"
                    + (f"; erisilebilen en guncel sezon {newest}." if newest else ".")
                )
                if access.get("provider_message"):
                    warnings.append(f"Saglayici mesaji: {access['provider_message']}")
        except AppError as exc:
            plan = PlanAccess(checked=True, requested_season=season, league_id=league_id,
                              note=exc.user_message)
            checks.append({"key": "plan", "label": "Plan / sezon erisimi",
                           "ok": False, "detail": exc.user_message})

    stats = {"total": None, "last_at": None}
    try:
        stats = database.snapshot_stats()
    except AppError:
        warnings.append("Snapshot istatistikleri okunamadi.")

    if not database.persistent:
        warnings.append(
            "Veritabani kalici degil (SQLite). Sunucu yeniden baslarsa snapshot "
            "gecmisi silinir. Kalici gecmis icin DATABASE_URL tanimlayin."
        )

    checks.append(
        {
            "key": "database",
            "label": "Veritabani",
            "ok": True,
            "detail": f"{database.describe()} - {'kalici' if database.persistent else 'GECICI'}",
        }
    )
    checks.append(
        {
            "key": "scheduler",
            "label": "Snapshot toplayici",
            "ok": scheduler.running,
            "detail": "Calisiyor" if scheduler.running else "Durdu / kapali",
        }
    )

    return StatusResponse(
        backend="ok",
        version=APP_VERSION,
        api_key_configured=settings.api_key_configured,
        api_connected=api_connected,
        bet365_available=bet365_available,
        bookmaker_id=bookmaker_id,
        database=database.describe(),
        database_persistent=database.persistent,
        snapshot_count=stats.get("total"),
        last_snapshot_at=stats.get("last_at"),
        scheduler_running=scheduler.running,
        quota=quota_info(client),
        timezone=settings.timezone_name,
        plan=plan,
        leagues=leagues_info,
        warnings=warnings,
        # Bu alan unutulmustu: frontend'deki "Bağlantı Durumu" paneli bunu
        # kullaniyor, gecmedigi icin panel bos kaliyordu.
        checks=checks,
    )


@app.get("/api/quota")
async def read_quota(client: ApiFootballClient = Depends(get_client)) -> dict[str, Any]:
    return client.quota.as_dict(
        "Degerler API-Football cevap header'larindan okunur; hic cagri yapilmadiysa bilinmez."
    )


@app.get("/api/capabilities")
async def read_capabilities(request: Request) -> dict[str, Any]:
    """Bu anahtarla gercekten NE yapilabildigini olcer (varsayim degil)."""
    state = request.app.state
    settings: Settings = state.settings
    resolver: BookmakerResolver = state.resolver

    result: dict[str, Any] = {
        "api_key_configured": settings.api_key_configured,
        "bookmaker_wanted": settings.bookmaker_name,
        "bookmaker_found": False,
        "bookmaker_id": None,
        "bookmaker_count": 0,
        "markets_detected": [],
        "note": None,
    }
    if not settings.api_key_configured:
        result["note"] = "API anahtari tanimli olmadan yetenek testi yapilamaz."
        return result

    budget = CallBudget(limit=6)
    # Teshis endpointi: fatal hatada bile 200 dondurup nedeni yazar.
    try:
        bookmaker, error = await resolver.try_resolve(budget=budget)
    except AppError as exc:
        result["note"] = exc.user_message
        return result
    result["bookmaker_count"] = len(resolver.known_bookmakers())
    if bookmaker is None:
        result["note"] = error
        return result

    result["bookmaker_found"] = True
    result["bookmaker_id"] = bookmaker.id
    result["note"] = (
        "Hangi marketlerin geldigi maca gore degisir; mac detayinda gercek "
        "market listesi gosterilir."
    )
    return result


@app.get("/api/bookmakers")
async def read_bookmakers(request: Request) -> dict[str, Any]:
    state = request.app.state
    resolver: BookmakerResolver = state.resolver
    budget = CallBudget(limit=4)
    # Teshis endpointi: fatal hatada bile 200 dondurup nedeni yazar.
    try:
        bookmaker, error = await resolver.try_resolve(budget=budget)
    except AppError as exc:
        bookmaker, error = None, exc.user_message
    return {
        "wanted": state.settings.bookmaker_name,
        "found": bookmaker.__dict__ if bookmaker else None,
        "error": error,
        "all": [item.__dict__ for item in resolver.known_bookmakers()],
    }


@app.get("/api/leagues")
async def read_leagues(request: Request) -> dict[str, Any]:
    state = request.app.state
    settings: Settings = state.settings
    budget = CallBudget(limit=12)
    leagues: list[dict[str, Any]] = []
    for league in settings.leagues:
        try:
            context = await state.service.league_context(league.id, budget=budget)
            leagues.append(
                {
                    "id": league.id,
                    "slug": league.slug,
                    "name": context.name,
                    "country": context.country,
                    "logo": context.logo,
                    "season": context.season,
                    "warning": context.warning,
                }
            )
        except AppError as exc:
            leagues.append(
                {
                    "id": league.id,
                    "slug": league.slug,
                    "name": league.expected_name,
                    "season": None,
                    "warning": exc.user_message,
                }
            )
    return {"leagues": leagues}


# ==========================================================================
# Maclar
# ==========================================================================
async def _load_matches(
    request: Request, leagues: str | None, days: int | None, date_from: str | None, date_to: str | None
):
    settings: Settings = request.app.state.settings
    service: MatchService = request.app.state.service
    league_ids = parse_league_ids(leagues, settings)
    start, end = parse_range(days, date_from, date_to, settings)
    summaries, analyses, budget, warnings, bookmaker = await service.list_matches(
        league_ids, start, end
    )
    return summaries, analyses, budget, warnings, bookmaker, league_ids, start, end


@app.get("/api/matches", response_model=MatchesResponse)
async def read_matches(
    request: Request,
    leagues: str | None = Query(None, description="Virgulle ayrilmis lig ID'leri, bos = hepsi"),
    days: int | None = Query(None, ge=1, le=MAX_RANGE_DAYS),
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
) -> MatchesResponse:
    settings: Settings = request.app.state.settings
    client: ApiFootballClient = request.app.state.client
    summaries, _, budget, warnings, _, league_ids, start, end = await _load_matches(
        request, leagues, days, date_from, date_to
    )
    with_odds = sum(1 for item in summaries if item.bet365.available)
    analyzed = sum(1 for item in summaries if item.analyzed)
    return MatchesResponse(
        generated_at=datetime.now(tz=settings.tzinfo),
        timezone=settings.timezone_name,
        filters={
            "leagues": league_ids,
            "from": start.isoformat(),
            "to": end.isoformat(),
        },
        counts={
            "total": len(summaries),
            "with_bet365_odds": with_odds,
            "analyzed": analyzed,
            "api_calls": budget.used,
        },
        matches=summaries,
        quota=quota_info(client),
        warnings=warnings,
    )


@app.get("/api/fixtures")
async def read_fixtures(
    request: Request,
    leagues: str | None = Query(None),
    days: int | None = Query(None, ge=1, le=MAX_RANGE_DAYS),
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
) -> dict[str, Any]:
    """Analiz yapmadan sadece fikstur listesi (hafif)."""
    summaries, _, budget, warnings, _, league_ids, start, end = await _load_matches(
        request, leagues, days, date_from, date_to
    )
    return {
        "filters": {"leagues": league_ids, "from": start.isoformat(), "to": end.isoformat()},
        "count": len(summaries),
        "api_calls": budget.used,
        "warnings": warnings,
        "fixtures": [
            {
                "fixture_id": item.fixture_id,
                "league": item.league.name,
                "home": item.home.name,
                "away": item.away.name,
                "kickoff": item.kickoff.display,
                "status": item.status_short,
                "bet365": item.bet365.available,
            }
            for item in summaries
        ],
    }


@app.get("/api/top5", response_model=Top5Response)
async def read_top5(
    request: Request,
    leagues: str | None = Query(None),
    days: int | None = Query(None, ge=1, le=MAX_RANGE_DAYS),
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
) -> Top5Response:
    settings: Settings = request.app.state.settings
    service: MatchService = request.app.state.service
    _, analyses, _, warnings, _, _, _, _ = await _load_matches(
        request, leagues, days, date_from, date_to
    )
    top, excluded = service.top_matches(analyses)
    return Top5Response(
        generated_at=datetime.now(tz=settings.tzinfo),
        criteria=(
            "Siralama: (model/piyasa farki) x (guven skoru). Yalnizca Bet365 orani "
            "olan, model calistirilabilen ve veri kalitesi esigini gecen maclar."
        ),
        matches=top,
        excluded_counts=excluded,
        warnings=warnings,
    )


@app.get("/api/highlights")
async def read_highlights(
    request: Request,
    leagues: str | None = Query(None),
    days: int | None = Query(None, ge=1, le=MAX_RANGE_DAYS),
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
) -> dict[str, Any]:
    summaries, _, _, warnings, _, _, _, _ = await _load_matches(
        request, leagues, days, date_from, date_to
    )
    cards: Sequence[HighlightCard] = build_highlights(summaries)
    return {"cards": [card.model_dump() for card in cards], "warnings": warnings}


# ==========================================================================
# Tek mac
# ==========================================================================
@app.get("/api/match/{fixture_id}", response_model=MatchDetail)
async def read_match(fixture_id: int, service: MatchService = Depends(get_service)) -> MatchDetail:
    if fixture_id <= 0:
        raise ValidationError("Gecersiz mac ID'si.")
    return await service.match_detail(fixture_id)


@app.get("/api/analysis/{fixture_id}", response_model=MatchDetail)
async def read_analysis(fixture_id: int, service: MatchService = Depends(get_service)) -> MatchDetail:
    if fixture_id <= 0:
        raise ValidationError("Gecersiz mac ID'si.")
    return await service.match_detail(fixture_id)


@app.get("/api/odds/{fixture_id}")
async def read_odds(fixture_id: int, request: Request) -> dict[str, Any]:
    if fixture_id <= 0:
        raise ValidationError("Gecersiz mac ID'si.")
    detail = await request.app.state.service.match_detail(fixture_id)
    return {
        "fixture_id": fixture_id,
        "bet365": detail.bet365.model_dump(),
        "implied": detail.implied.model_dump(),
        "odds_movement": detail.odds_movement.model_dump(),
    }


@app.get("/api/predictions/{fixture_id}")
async def read_predictions(fixture_id: int, request: Request) -> dict[str, Any]:
    if fixture_id <= 0:
        raise ValidationError("Gecersiz mac ID'si.")
    detail = await request.app.state.service.match_detail(fixture_id)
    return {
        "fixture_id": fixture_id,
        "model": detail.model.model_dump(),
        "api_prediction": detail.api_prediction.model_dump(),
        "value": detail.value.model_dump(),
    }


@app.get("/api/form/{fixture_id}")
async def read_form(fixture_id: int, request: Request) -> dict[str, Any]:
    if fixture_id <= 0:
        raise ValidationError("Gecersiz mac ID'si.")
    detail = await request.app.state.service.match_detail(fixture_id)
    return {
        "fixture_id": fixture_id,
        "home": {"team": detail.home.model_dump(), "form": detail.home_form.model_dump(),
                 "venue": detail.home_venue.model_dump()},
        "away": {"team": detail.away.model_dump(), "form": detail.away_form.model_dump(),
                 "venue": detail.away_venue.model_dump()},
    }


@app.get("/api/h2h/{fixture_id}")
async def read_h2h(fixture_id: int, request: Request) -> dict[str, Any]:
    if fixture_id <= 0:
        raise ValidationError("Gecersiz mac ID'si.")
    detail = await request.app.state.service.match_detail(fixture_id)
    return {"fixture_id": fixture_id, "h2h": detail.h2h.model_dump()}


# ==========================================================================
# Snapshot
# ==========================================================================
@app.get("/api/snapshots/{fixture_id}")
async def read_snapshots(fixture_id: int, request: Request) -> dict[str, Any]:
    if fixture_id <= 0:
        raise ValidationError("Gecersiz mac ID'si.")
    database: Database = request.app.state.database
    rows = load_snapshots(database, fixture_id)
    return {
        "fixture_id": fixture_id,
        "persistent_storage": database.persistent,
        "count": len(rows),
        "note": (
            None
            if rows
            else "Bu mac icin kaydedilmis Bet365 oran snapshot'i bulunmuyor. "
            "Gecmis oran uydurulmaz."
        ),
        "snapshots": [
            {
                "captured_at": row.get("captured_at"),
                "bookmaker": row.get("bookmaker_name"),
                "market": row.get("market"),
                "1": row.get("home_odds"),
                "X": row.get("draw_odds"),
                "2": row.get("away_odds"),
                "source": row.get("source"),
            }
            for row in rows
        ],
    }


@app.post("/api/snapshots/capture")
async def capture_snapshots(request: Request) -> dict[str, Any]:
    """Snapshot turunu elle tetikler (zamanlayicinin yaptigi isin aynisi)."""
    scheduler: SnapshotScheduler = request.app.state.scheduler
    result = await scheduler.run_once()
    scheduler.last_run_at = datetime.now(tz=request.app.state.settings.tzinfo)
    scheduler.last_result = result
    return result


@app.get("/api/calibration")
async def read_calibration(request: Request) -> dict[str, Any]:
    """Model kalibrasyonu: gecmis tahminler gercek sonuclarla karsilastirilir.

    Yeterli sonuclanmis mac yoksa sayi uretilmez.
    """
    database: Database = request.app.state.database
    rows = database.calibration_rows(limit=1000)
    if len(rows) < 20:
        return {
            "available": False,
            "settled_matches": len(rows),
            "note": (
                f"Kalibrasyon icin yeterli sonuclanmis tahmin yok ({len(rows)}/20). "
                "Maclar oynandikca bu rapor dolacak."
            ),
        }

    hits = 0
    brier_sum = 0.0
    for row in rows:
        home_goals = row.get("actual_home_goals")
        away_goals = row.get("actual_away_goals")
        if home_goals is None or away_goals is None:
            continue
        actual = "1" if home_goals > away_goals else ("X" if home_goals == away_goals else "2")
        probabilities = {
            "1": float(row.get("model_home") or 0),
            "X": float(row.get("model_draw") or 0),
            "2": float(row.get("model_away") or 0),
        }
        predicted = max(probabilities, key=lambda key: probabilities[key])
        if predicted == actual:
            hits += 1
        for key, value in probabilities.items():
            brier_sum += (value - (1.0 if key == actual else 0.0)) ** 2

    total = len(rows)
    return {
        "available": True,
        "settled_matches": total,
        "top_pick_accuracy": round(hits / total, 4),
        "brier_score": round(brier_sum / total, 4),
        "note": (
            "Brier skoru dusukse model daha iyi kalibre demektir. Bu rapor "
            "gercek sonuclarla olculur, iddia degildir."
        ),
    }
