
from datetime import datetime, timedelta
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

# ============================================================
# MACANALİZ PRO - API GATEWAY
# ============================================================
# Bu dosya API anahtarını frontend/GitHub içine koymadan
# API-Football ile konuşmak için kullanılır.
#
# Render/Railway üzerinde Environment Variable:
#   API_FOOTBALL_KEY = SENİN_API_FOOTBALL_ANAHTARIN
#
# Bet365 ana bookmaker'dır. Başka bookmaker'a fallback YOKTUR.
# ============================================================

APP_NAME = "MACANALİZ PRO API"
API_BASE = "https://v3.football.api-sports.io"
BET365_NAME = "Bet365"
DEFAULT_LEAGUES = [39, 140, 135, 78, 61, 203]
DEFAULT_DAYS = 7
MAX_DAYS = 14

app = FastAPI(title=APP_NAME, version="1.0.0")

# GitHub Pages frontend'inin backend'e erişebilmesi için.
# Üretimde kendi GitHub Pages domain'in ile sınırlandırılabilir.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)

_client: Optional[httpx.AsyncClient] = None
_bookmaker_cache = {"id": None, "name": None, "loaded_at": None}


def api_key() -> str:
    key = os.getenv("API_FOOTBALL_KEY", "").strip()
    if not key:
        raise HTTPException(
            status_code=503,
            detail="API_FOOTBALL_KEY backend Environment Variable olarak tanımlanmamış."
        )
    return key


async def client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=20.0)
    return _client


async def af_get(path: str, params: Optional[dict] = None) -> dict:
    key = api_key()
    c = await client()

    try:
        response = await c.get(
            f"{API_BASE}{path}",
            params=params or {},
            headers={
                "x-apisports-key": key,
                "Accept": "application/json",
            },
        )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"API-Football'a ulaşılamadı: {exc}"
        )

    try:
        data = response.json()
    except Exception:
        raise HTTPException(
            status_code=502,
            detail=f"API-Football JSON döndürmedi. HTTP {response.status_code}"
        )

    # API-Football hata mesajını kaybetmeden frontend'e gönder.
    errors = data.get("errors") or {}
    if errors:
        if isinstance(errors, dict):
            msg = " | ".join(str(v) for v in errors.values())
        else:
            msg = str(errors)

        raise HTTPException(
            status_code=400 if response.status_code < 500 else response.status_code,
            detail=msg,
        )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail=f"API-Football HTTP {response.status_code}"
        )

    return data


def parse_leagues(value: str) -> list[int]:
    result = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            n = int(item)
            if n not in result:
                result.append(n)
        except ValueError:
            continue

    if not result:
        raise HTTPException(status_code=400, detail="Geçerli lig ID'si verilmedi.")

    return result


async def get_bet365() -> dict:
    # Önce cache.
    if _bookmaker_cache["id"]:
        return {
            "id": _bookmaker_cache["id"],
            "name": _bookmaker_cache["name"] or BET365_NAME,
        }

    data = await af_get("/odds/bookmakers")
    bookmakers = data.get("response") or []

    # Önce tam isim, sonra case-insensitive contains.
    exact = next(
        (
            b for b in bookmakers
            if str(b.get("name", "")).strip().lower() == BET365_NAME.lower()
        ),
        None,
    )
    if exact is None:
        exact = next(
            (
                b for b in bookmakers
                if "bet365" in str(b.get("name", "")).lower()
            ),
            None,
        )

    if exact is None:
        raise HTTPException(
            status_code=404,
            detail="API-Football bookmaker listesinde Bet365 bulunamadı. "
                   "Bu hesabın odds/bookmaker kapsamı kontrol edilmeli."
        )

    bookmaker_id = exact.get("id")
    if bookmaker_id is None:
        raise HTTPException(
            status_code=502,
            detail="Bet365 bulundu fakat bookmaker ID döndürülmedi."
        )

    _bookmaker_cache["id"] = int(bookmaker_id)
    _bookmaker_cache["name"] = exact.get("name") or BET365_NAME
    _bookmaker_cache["loaded_at"] = datetime.utcnow().isoformat()

    return {"id": int(bookmaker_id), "name": _bookmaker_cache["name"]}


def normalize_fixture(item: dict) -> dict:
    fixture = item.get("fixture") or {}
    league = item.get("league") or {}
    teams = item.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    goals = item.get("goals") or {}

    return {
        "id": fixture.get("id"),
        "date": fixture.get("date"),
        "timestamp": fixture.get("timestamp"),
        "status": (fixture.get("status") or {}).get("short"),
        "status_long": (fixture.get("status") or {}).get("long"),
        "venue": (fixture.get("venue") or {}).get("name"),
        "referee": fixture.get("referee"),
        "league": {
            "id": league.get("id"),
            "name": league.get("name"),
            "country": league.get("country"),
            "season": league.get("season"),
            "round": league.get("round"),
        },
        "home": {
            "id": home.get("id"),
            "name": home.get("name"),
            "logo": home.get("logo"),
        },
        "away": {
            "id": away.get("id"),
            "name": away.get("name"),
            "logo": away.get("logo"),
        },
        "goals": {
            "home": goals.get("home"),
            "away": goals.get("away"),
        },
    }


def extract_1x2(bookmaker: dict) -> Optional[dict]:
    # Yalnızca Match Winner / Fulltime Result / 1X2.
    bets = bookmaker.get("bets") or []
    market = next(
        (
            b for b in bets
            if str(b.get("name", "")).lower()
            in {"match winner", "fulltime result", "1x2"}
        ),
        None,
    )
    if market is None:
        market = next(
            (
                b for b in bets
                if any(
                    x in str(b.get("name", "")).lower()
                    for x in ("match winner", "fulltime", "1x2")
                )
            ),
            None,
        )

    if not market:
        return None

    out = {}
    for value in market.get("values") or []:
        label = str(value.get("value", "")).strip().lower()
        odd = value.get("odd")
        try:
            odd = float(odd)
        except (TypeError, ValueError):
            continue

        if label in {"home", "1"}:
            out["home"] = odd
        elif label in {"draw", "x"}:
            out["draw"] = odd
        elif label in {"away", "2"}:
            out["away"] = odd

    if not all(k in out for k in ("home", "draw", "away")):
        return None

    return {
        "market": market.get("name"),
        "home": out["home"],
        "draw": out["draw"],
        "away": out["away"],
    }


def normalize_odds(data: dict, fixture_id: int) -> dict:
    response = data.get("response") or []
    bet365 = None

    # SADECE BET365.
    for row in response:
        for bookmaker in row.get("bookmakers") or []:
            name = str(bookmaker.get("name", ""))
            if name.strip().lower() == BET365_NAME.lower() or "bet365" in name.lower():
                bet365 = bookmaker
                break
        if bet365:
            break

    if not bet365:
        return {
            "fixture_id": fixture_id,
            "available": False,
            "bookmaker": BET365_NAME,
            "bookmaker_id": _bookmaker_cache.get("id"),
            "odds": None,
            "update": None,
        }

    return {
        "fixture_id": fixture_id,
        "available": True,
        "bookmaker": bet365.get("name") or BET365_NAME,
        "bookmaker_id": bet365.get("id") or _bookmaker_cache.get("id"),
        "update": bet365.get("update") or (response[0].get("update") if response else None),
        "odds": extract_1x2(bet365),
    }


@app.get("/")
async def root():
    return {
        "name": APP_NAME,
        "status": "ok",
        "bookmaker_policy": "BET365_ONLY",
        "message": "MACANALİZ PRO backend çalışıyor.",
    }


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "service": APP_NAME,
        "bet365_only": True,
        "api_base": API_BASE,
    }


@app.get("/api/account")
async def account():
    # API anahtarını frontend'e geri döndürmez.
    data = await af_get("/status")
    response = data.get("response") or {}
    return {
        "connected": True,
        "account": response,
        "errors": data.get("errors") or {},
    }


@app.get("/api/bet365")
async def bet365():
    bookmaker = await get_bet365()
    return {
        "connected": True,
        "bookmaker": bookmaker,
        "policy": "BET365_ONLY",
    }


@app.get("/api/leagues")
async def leagues(
    ids: str = Query(",".join(map(str, DEFAULT_LEAGUES))),
):
    league_ids = parse_leagues(ids)
    result = []

    for league_id in league_ids:
        try:
            data = await af_get(
                "/leagues",
                {"id": league_id, "current": "true"},
            )
            rows = data.get("response") or []

            if rows:
                row = rows[0]
                seasons = row.get("seasons") or []
                current = next(
                    (s for s in seasons if s.get("current") is True),
                    None,
                )

                result.append({
                    "id": league_id,
                    "name": (row.get("league") or {}).get("name"),
                    "country": (row.get("country") or {}).get("name"),
                    "current_season": current.get("year") if current else None,
                    "coverage": (current or {}).get("coverage") if current else None,
                    "ok": True,
                })
            else:
                result.append({
                    "id": league_id,
                    "ok": False,
                    "error": "Lig bulunamadı veya mevcut sezon kapsamı yok.",
                })

        except HTTPException as exc:
            result.append({
                "id": league_id,
                "ok": False,
                "error": str(exc.detail),
            })

    return {
        "bet365_only": True,
        "leagues": result,
    }


@app.get("/api/fixtures")
async def fixtures(
    ids: str = Query(",".join(map(str, DEFAULT_LEAGUES))),
    days: int = Query(DEFAULT_DAYS, ge=1, le=MAX_DAYS),
    season: Optional[int] = Query(None),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    timezone: str = Query("Europe/Istanbul"),
):
    league_ids = parse_leagues(ids)

    # Tarihleri backend üretir; böylece frontend UTC/gün kayması yapmaz.
    if from_date and to_date:
        start = from_date
        end = to_date
    else:
        today = datetime.utcnow().date()
        start = today.isoformat()
        end = (today + timedelta(days=days)).isoformat()

    all_fixtures = []
    errors = []

    for league_id in league_ids:
        params = {
            "league": league_id,
            "from": start,
            "to": end,
            "timezone": timezone,
        }
        if season is not None:
            params["season"] = season

        try:
            data = await af_get("/fixtures", params)
            rows = data.get("response") or []
            all_fixtures.extend(normalize_fixture(x) for x in rows)
        except HTTPException as exc:
            errors.append({
                "league_id": league_id,
                "error": str(exc.detail),
            })

    # Tekilleştir + tarihe göre sırala.
    unique = {}
    for f in all_fixtures:
        if f.get("id") is not None:
            unique[f["id"]] = f

    items = sorted(
        unique.values(),
        key=lambda x: x.get("timestamp") or 0,
    )

    return {
        "live": True,
        "bet365_only": True,
        "from": start,
        "to": end,
        "timezone": timezone,
        "count": len(items),
        "fixtures": items,
        "errors": errors,
    }


@app.get("/api/odds")
async def odds(
    fixture: int = Query(..., ge=1),
):
    bookmaker = await get_bet365()

    data = await af_get(
        "/odds",
        {
            "fixture": fixture,
            "bookmaker": bookmaker["id"],
        },
    )

    return normalize_odds(data, fixture)


@app.get("/api/match")
async def match_data(
    fixture: int = Query(..., ge=1),
):
    # Tek maç için temel veri + Bet365 1X2.
    fixture_data = await af_get("/fixtures", {"id": fixture})
    rows = fixture_data.get("response") or []

    if not rows:
        raise HTTPException(status_code=404, detail="Maç bulunamadı.")

    f = normalize_fixture(rows[0])
    o = await odds(fixture)

    return {
        "fixture": f,
        "bet365": o,
    }


@app.get("/api/h2h")
async def h2h(
    home: int = Query(..., ge=1),
    away: int = Query(..., ge=1),
    last: int = Query(10, ge=1, le=20),
):
    data = await af_get(
        "/fixtures/headtohead",
        {"h2h": f"{home}-{away}", "last": last},
    )
    return {
        "home_team_id": home,
        "away_team_id": away,
        "response": data.get("response") or [],
    }


@app.get("/api/standings")
async def standings(
    league: int = Query(..., ge=1),
    season: int = Query(..., ge=2000),
):
    data = await af_get(
        "/standings",
        {"league": league, "season": season},
    )
    return {
        "league": league,
        "season": season,
        "response": data.get("response") or [],
    }


@app.get("/api/prediction")
async def prediction(
    fixture: int = Query(..., ge=1),
):
    data = await af_get(
        "/predictions",
        {"fixture": fixture},
    )
    return {
        "fixture": fixture,
        "response": data.get("response") or [],
    }


@app.on_event("shutdown")
async def shutdown():
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
