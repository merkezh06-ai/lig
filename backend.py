import os
import asyncio
from datetime import date, datetime, timedelta
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware


# =========================================================
# MACANALIZ PRO - BACKEND
# API-FOOTBALL + BET365
# =========================================================

BASE_URL = "https://v3.football.api-sports.io"
BET365_NAME = "Bet365"

DEFAULT_LEAGUES = "39,140,135,78,61,203"

app = FastAPI(title="MACANALIZ PRO API", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)

client: Optional[httpx.AsyncClient] = None

# Cache
bookmaker_cache = None
bookmaker_cache_time = None

# Çalışma sırasında oran snapshotları.
# Kalıcı T-15 geçmişi için ileride PostgreSQL eklenebilir.
odds_snapshots = {}


# =========================================================
# HTTP CLIENT
# =========================================================

@app.on_event("startup")
async def startup():
    global client

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(25.0, connect=10.0)
    )


@app.on_event("shutdown")
async def shutdown():
    global client

    if client:
        await client.aclose()
        client = None


# =========================================================
# API KEY
# =========================================================

def get_api_key():
    value = os.getenv("API_FOOTBALL_KEY", "").strip()

    if not value:
        raise HTTPException(
            status_code=503,
            detail=(
                "API_FOOTBALL_KEY bulunamadı. "
                "Render > Environment Variables bölümüne "
                "API_FOOTBALL_KEY eklenmelidir."
            ),
        )

    return value


# =========================================================
# API-FOOTBALL REQUEST
# =========================================================

async def api_get(path: str, params=None):
    global client

    if client is None:
        raise HTTPException(
            status_code=503,
            detail="Backend HTTP istemcisi henüz hazır değil."
        )

    headers = {
        "x-apisports-key": get_api_key(),
        "Accept": "application/json",
    }

    try:
        response = await client.get(
            BASE_URL + path,
            params=params or {},
            headers=headers,
        )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"API-Football bağlantı hatası: {exc}",
        )

    try:
        data = response.json()
    except Exception:
        raise HTTPException(
            status_code=502,
            detail=(
                f"API-Football JSON döndürmedi. "
                f"HTTP {response.status_code}"
            ),
        )

    # API seviyesindeki hatalar
    errors = data.get("errors")

    if errors:
        if isinstance(errors, dict):
            message = " | ".join(
                f"{k}: {v}" for k, v in errors.items()
            )
        elif isinstance(errors, list):
            message = " | ".join(str(x) for x in errors)
        else:
            message = str(errors)

        raise HTTPException(
            status_code=400,
            detail=f"API-Football: {message}",
        )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail=f"API-Football HTTP {response.status_code}",
        )

    return data


# =========================================================
# ROOT / HEALTH
# =========================================================

@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "MACANALIZ PRO API",
        "bet365_only": True,
        "api": "API-Football",
    }


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "backend": True,
        "bet365_only": True,
        "api_key_configured": bool(
            os.getenv("API_FOOTBALL_KEY", "").strip()
        ),
        "time": datetime.utcnow().isoformat(),
    }


# =========================================================
# API STATUS
# =========================================================

@app.get("/api/status")
async def api_status():
    """
    API-Football hesap durumunu kontrol eder.
    """

    data = await api_get("/status")

    return {
        "connected": True,
        "status": data,
    }


# =========================================================
# BET365 BOOKMAKER
# =========================================================

async def find_bet365():
    global bookmaker_cache
    global bookmaker_cache_time

    now = datetime.utcnow()

    if (
        bookmaker_cache
        and bookmaker_cache_time
        and (now - bookmaker_cache_time).total_seconds() < 86400
    ):
        return bookmaker_cache

    data = await api_get("/odds/bookmakers")

    bookmakers = data.get("response") or []

    bet365 = None

    for bookmaker in bookmakers:
        name = str(bookmaker.get("name", "")).strip().lower()

        if name == "bet365":
            bet365 = bookmaker
            break

    if bet365 is None:
        for bookmaker in bookmakers:
            name = str(bookmaker.get("name", "")).strip().lower()

            if "bet365" in name:
                bet365 = bookmaker
                break

    if bet365 is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "Bet365 API-Football hesabında bulunamadı. "
                "Bu durumda başka bookmaker kullanılmayacaktır."
            ),
        )

    bookmaker_cache = {
        "id": bet365.get("id"),
        "name": bet365.get("name", BET365_NAME),
    }

    bookmaker_cache_time = now

    return bookmaker_cache


@app.get("/api/bet365")
async def bet365():
    bookmaker = await find_bet365()

    return {
        "connected": True,
        "bookmaker": bookmaker,
        "bet365_only": True,
    }


# =========================================================
# SEASON BULMA
# =========================================================

async def get_available_seasons(league_id: int):
    data = await api_get(
        "/leagues",
        {
            "id": league_id,
        },
    )

    response = data.get("response") or []

    if not response:
        return []

    seasons = response[0].get("seasons") or []

    result = []

    for season in seasons:
        year = season.get("year")

        if year is not None:
            try:
                result.append(int(year))
            except Exception:
                pass

    return sorted(set(result), reverse=True)


async def choose_season(league_id: int, requested: Optional[int]):
    seasons = await get_available_seasons(league_id)

    if not seasons:
        return requested

    if requested is not None and requested in seasons:
        return requested

    current_year = date.today().year

    # Önce mevcut yılı dene.
    if current_year in seasons:
        return current_year

    # Son erişilebilir sezonu kullan.
    return seasons[0]


# =========================================================
# FIXTURES
# =========================================================

@app.get("/api/fixtures")
async def fixtures(
    ids: str = Query(DEFAULT_LEAGUES),
    days: int = Query(7, ge=1, le=14),
    season: Optional[int] = Query(None),
    include_past: bool = Query(False),
):
    """
    Yaklaşan maçları getirir.

    include_past=false:
        Bugün -> sonraki N gün

    include_past=true:
        Son 7 gün -> sonraki N gün

    Böylece geçmiş maçlar da analiz ekranına girebilir.
    """

    try:
        league_ids = [
            int(x.strip())
            for x in ids.split(",")
            if x.strip()
        ]
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Lig ID listesi hatalı.",
        )

    if not league_ids:
        raise HTTPException(
            status_code=400,
            detail="En az bir lig ID girilmelidir.",
        )

    today = date.today()

    if include_past:
        start_date = today - timedelta(days=7)
    else:
        start_date = today

    end_date = today + timedelta(days=days)

    all_fixtures = []
    errors = []

    # API kotasını gereksiz tüketmemek için
    # aynı anda sınırlı sayıda istek.
    semaphore = asyncio.Semaphore(3)

    async def load_league(league_id):
        async with semaphore:
            try:
                selected_season = await choose_season(
                    league_id,
                    season,
                )

                params = {
                    "league": league_id,
                    "from": start_date.isoformat(),
                    "to": end_date.isoformat(),
                    "timezone": "Europe/Istanbul",
                }

                if selected_season is not None:
                    params["season"] = selected_season

                data = await api_get(
                    "/fixtures",
                    params,
                )

                return {
                    "league_id": league_id,
                    "season": selected_season,
                    "fixtures": data.get("response") or [],
                    "error": None,
                }

            except HTTPException as exc:
                return {
                    "league_id": league_id,
                    "season": None,
                    "fixtures": [],
                    "error": str(exc.detail),
                }

            except Exception as exc:
                return {
                    "league_id": league_id,
                    "season": None,
                    "fixtures": [],
                    "error": str(exc),
                }

    results = await asyncio.gather(
        *[
            load_league(league_id)
            for league_id in league_ids
        ]
    )

    for result in results:
        all_fixtures.extend(result["fixtures"])

        if result["error"]:
            errors.append(
                {
                    "league_id": result["league_id"],
                    "season": result["season"],
                    "error": result["error"],
                }
            )

    # Duplicate fixture temizliği
    unique = {}

    for fixture in all_fixtures:
        fixture_id = (
            fixture.get("fixture", {})
            .get("id")
        )

        if fixture_id:
            unique[fixture_id] = fixture

    fixtures_list = list(unique.values())

    # Tarihe göre sırala
    fixtures_list.sort(
        key=lambda x: (
            x.get("fixture", {})
            .get("timestamp", 0)
        )
    )

    return {
        "live": True,
        "bet365_only": True,
        "count": len(fixtures_list),
        "from": start_date.isoformat(),
        "to": end_date.isoformat(),
        "fixtures": fixtures_list,
        "errors": errors,
    }


# =========================================================
# MATCH WINNER / MS 1-X-2
# =========================================================

def extract_match_winner(bookmaker):
    bets = bookmaker.get("bets") or []

    for bet in bets:
        market_name = str(
            bet.get("name", "")
        ).strip().lower()

        if not (
            "match winner" in market_name
            or market_name in (
                "1x2",
                "fulltime result",
            )
        ):
            continue

        result = {
            "home": None,
            "draw": None,
            "away": None,
        }

        for value in bet.get("values") or []:
            odd_raw = value.get("odd")

            try:
                odd = float(odd_raw)
            except (TypeError, ValueError):
                continue

            label = str(
                value.get("value", "")
            ).strip().lower()

            if label in ("home", "1"):
                result["home"] = odd

            elif label in ("draw", "x"):
                result["draw"] = odd

            elif label in ("away", "2"):
                result["away"] = odd

        if all(
            result[x] is not None
            for x in ("home", "draw", "away")
        ):
            return result

    return None


# =========================================================
# TÜM BET365 MARKETLERİ
# =========================================================

def extract_markets(bookmaker):
    markets = {}

    for bet in bookmaker.get("bets") or []:
        name = str(
            bet.get("name", "")
        ).strip()

        if not name:
            continue

        values = []

        for value in bet.get("values") or []:
            odd = value.get("odd")

            try:
                odd = float(odd)
            except (TypeError, ValueError):
                continue

            values.append(
                {
                    "value": value.get("value"),
                    "odd": odd,
                }
            )

        if values:
            markets[name] = values

    return markets


# =========================================================
# ODDS
# =========================================================

@app.get("/api/odds")
async def odds(
    fixture: int = Query(..., ge=1)
):
    bookmaker = await find_bet365()

    data = await api_get(
        "/odds",
        {
            "fixture": fixture,
            "bookmaker": bookmaker["id"],
        },
    )

    response = data.get("response") or []

    if not response:
        return {
            "available": False,
            "fixture_id": fixture,
            "bookmaker": BET365_NAME,
            "bookmaker_id": bookmaker["id"],
            "odds": None,
            "markets": {},
            "update": None,
            "message": (
                "Bu maç için API-Football "
                "üzerinde Bet365 oranı bulunamadı."
            ),
        }

    for row in response:
        for bm in row.get("bookmakers") or []:

            if "bet365" not in str(
                bm.get("name", "")
            ).lower():
                continue

            match_winner = extract_match_winner(bm)
            markets = extract_markets(bm)

            # T-15 snapshot
            now = datetime.utcnow()

            snapshots = odds_snapshots.setdefault(
                fixture,
                [],
            )

            snapshot = {
                "time": now.isoformat(),
                "odds": match_winner,
            }

            if match_winner:
                if not snapshots or (
                    snapshots[-1].get("odds")
                    != match_winner
                ):
                    snapshots.append(snapshot)

            # Son 20 snapshot yeterli
            odds_snapshots[fixture] = snapshots[-20:]

            return {
                "available": match_winner is not None,
                "fixture_id": fixture,
                "bookmaker": bm.get(
                    "name",
                    BET365_NAME,
                ),
                "bookmaker_id": bm.get(
                    "id",
                    bookmaker["id"],
                ),
                "odds": match_winner,
                "markets": markets,
                "update": bm.get("update"),
                "snapshots": odds_snapshots.get(
                    fixture,
                    [],
                ),
            }

    return {
        "available": False,
        "fixture_id": fixture,
        "bookmaker": BET365_NAME,
        "bookmaker_id": bookmaker["id"],
        "odds": None,
        "markets": {},
        "update": None,
        "snapshots": odds_snapshots.get(
            fixture,
            [],
        ),
    }


# =========================================================
# PREDICTION
# =========================================================

@app.get("/api/prediction")
async def prediction(
    fixture: int = Query(..., ge=1)
):
    """
    API-Football'un kendi prediction endpointini kullanır.
    """

    data = await api_get(
        "/predictions",
        {
            "fixture": fixture,
        },
    )

    response = data.get("response") or []

    if not response:
        return {
            "available": False,
            "fixture_id": fixture,
            "prediction": None,
        }

    item = response[0]

    prediction_data = item.get(
        "predictions"
    ) or {}

    teams = item.get("teams") or {}

    return {
        "available": True,
        "fixture_id": fixture,
        "teams": teams,
        "prediction": prediction_data,
    }


# =========================================================
# MAÇ DETAY ANALİZİ
# =========================================================

@app.get("/api/match")
async def match_analysis(
    fixture: int = Query(..., ge=1)
):
    """
    Frontend tek çağrıyla:
    - maç
    - Bet365 oranı
    - prediction
    alabilsin.
    """

    fixture_data = await api_get(
        "/fixtures",
        {
            "id": fixture,
        },
    )

    fixtures_data = (
        fixture_data.get("response")
        or []
    )

    if not fixtures_data:
        raise HTTPException(
            status_code=404,
            detail="Maç bulunamadı.",
        )

    fixture_item = fixtures_data[0]

    # Odds
    odds_data = await odds(fixture)

    # Prediction
    try:
        prediction_data = await prediction(
            fixture
        )
    except HTTPException as exc:
        prediction_data = {
            "available": False,
            "fixture_id": fixture,
            "prediction": None,
            "error": str(exc.detail),
        }

    return {
        "fixture": fixture_item,
        "bet365": odds_data,
        "prediction": prediction_data,
        "bet365_only": True,
    }


# =========================================================
# TOP 5 ANALİZ
# =========================================================

def calculate_market_probability(
    home,
    draw,
    away,
):
    values = [
        x for x in (home, draw, away)
        if isinstance(x, (int, float))
        and x > 0
    ]

    if not values:
        return None

    inverse = [
        1 / x for x in values
    ]

    total = sum(inverse)

    if total <= 0:
        return None

    return [
        round((x / total) * 100, 2)
        for x in inverse
    ]


@app.get("/api/analyze")
async def analyze(
    ids: str = Query(DEFAULT_LEAGUES),
    days: int = Query(7, ge=1, le=14),
    season: Optional[int] = Query(None),
):
    """
    Ana ekran için:
    maçları getirir,
    Bet365 oranlarını kontrol eder,
    MS olasılıklarını hesaplar,
    en güçlü ilk 5'i döndürür.

    Bet365 oranı olmayan maçlar
    öneri sıralamasına alınmaz.
    """

    fixture_response = await fixtures(
        ids=ids,
        days=days,
        season=season,
        include_past=False,
    )

    fixture_list = (
        fixture_response.get("fixtures")
        or []
    )

    analyzed = []

    # API kotasını korumak için maksimum 15 maç
    # üzerinde odds çağrısı.
    for fixture in fixture_list[:15]:

        fixture_id = (
            fixture.get("fixture", {})
            .get("id")
        )

        if not fixture_id:
            continue

        try:
            odds_data = await odds(
                int(fixture_id)
            )
        except HTTPException:
            continue

        if not odds_data.get("available"):
            # Bet365 oranı yoksa öneriye sokma
            continue

        odd = odds_data.get("odds")

        if not odd:
            continue

        probabilities = calculate_market_probability(
            odd.get("home"),
            odd.get("draw"),
            odd.get("away"),
        )

        if not probabilities:
            continue

        # En yüksek MS olasılığı
        max_probability = max(probabilities)
        selection_index = probabilities.index(
            max_probability
        )

        selection = ["MS 1", "MS X", "MS 2"][
            selection_index
        ]

        analyzed.append(
            {
                "fixture": fixture,
                "bet365": {
                    "odds": odd,
                    "update": odds_data.get(
                        "update"
                    ),
                },
                "analysis": {
                    "selection": selection,
                    "probability": max_probability,
                    "probabilities": {
                        "MS1": probabilities[0],
                        "MSX": probabilities[1],
                        "MS2": probabilities[2],
                    },
                },
            }
        )

    analyzed.sort(
        key=lambda x: x["analysis"]["probability"],
        reverse=True,
    )

    return {
        "status": "ok",
        "bet365_only": True,
        "count": len(analyzed),
        "top5": analyzed[:5],
        "fixtures_total": len(fixture_list),
        "errors": fixture_response.get(
            "errors",
            [],
        ),
    }
