"""Deploy oncesi otomatik kontrol listesi.

Kullanim:
    python tools/preflight.py

API anahtari tanimliysa canli kontroller de yapilir; tanimli degilse o
maddeler "atlandi" olarak isaretlenir (basarisiz sayilmaz).

Bir madde bile FAIL ise cikis kodu 1'dir - deploy etmeyin.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Kontrol sirasinda arka plan snapshot dongusu bosuna kota harcamasin.
os.environ["SNAPSHOT_ENABLED"] = "false"

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    symbol = {"PASS": "[ x ]", "FAIL": "[!!!]", "SKIP": "[ - ]"}[status]
    print(f"{symbol} {name}" + (f"  -> {detail}" if detail else ""))


def check(name: str, fn) -> None:
    try:
        detail = fn()
        record(name, PASS, detail or "")
    except AssertionError as exc:
        record(name, FAIL, str(exc))
    except Exception as exc:  # pragma: no cover
        record(name, FAIL, f"{type(exc).__name__}: {exc}")


# ==========================================================================
# 1-2  Import ve uygulama
# ==========================================================================
def check_backend_imports() -> str:
    import backend  # noqa: F401

    return "backend.py import edildi"


def check_app_starts() -> str:
    import backend

    routes = [route.path for route in backend.app.routes if hasattr(route, "path")]
    required = [
        "/api/health", "/api/status", "/api/config", "/api/matches", "/api/top5",
        "/api/match/{fixture_id}", "/api/odds/{fixture_id}", "/api/predictions/{fixture_id}",
        "/api/form/{fixture_id}", "/api/h2h/{fixture_id}", "/api/analysis/{fixture_id}",
        "/api/snapshots/{fixture_id}", "/api/snapshots/capture", "/api/leagues",
        "/api/bookmakers", "/api/fixtures", "/api/quota", "/api/capabilities",
        "/api/highlights", "/api/calibration",
    ]
    missing = [path for path in required if path not in routes]
    assert not missing, f"eksik endpoint: {missing}"
    return f"{len(required)} endpoint tanimli"


# ==========================================================================
# 3-5  HTTP
# ==========================================================================
def _client():
    """TestClient'i CONTEXT MANAGER olarak dondurur.

    Girilmezse FastAPI lifespan'i calismaz ve app.state bos kalir.
    """
    from fastapi.testclient import TestClient

    import backend

    return TestClient(backend.app)


def check_health() -> str:
    with _client() as client:
        response = client.get("/api/health")
    assert response.status_code == 200, f"HTTP {response.status_code}"
    assert response.json().get("status") == "ok"
    return "200 OK"


def check_status() -> str:
    with _client() as client:
        response = client.get("/api/status")
    assert response.status_code == 200, f"HTTP {response.status_code}"
    payload = response.json()
    for key in ("api_key_configured", "api_connected", "bet365_available", "database"):
        assert key in payload, f"{key} alani yok"
    # Kimlik bilgisi tasiyan bir alan var mi? (Duz "key" aramasi yanlis alarm
    # uretir: checks listesinde zararsiz bir "key" alani var.)
    leak = re.search(
        r'"(api[_-]?key|apikey|x-apisports-key|token|secret|password)"\s*:\s*"[^"]{4,}"',
        response.text, re.IGNORECASE,
    )
    assert leak is None, f"sir sizintisi: {leak.group(0) if leak else ''}"
    assert payload.get("checks"), "checks alani bos - frontend durum paneli bos kalir"
    return (
        f"db={payload['database']}, bet365={payload['bet365_available']}, "
        f"{len(payload['checks'])} kontrol"
    )


def check_missing_key_error() -> str:
    saved = os.environ.get("API_FOOTBALL_KEY", "")
    os.environ["API_FOOTBALL_KEY"] = ""
    try:
        import importlib

        import backend
        import config as config_module
        import database as database_module

        config_module.get_settings(refresh=True)
        database_module.get_database(refresh=True)
        importlib.reload(backend)
        from fastapi.testclient import TestClient

        with TestClient(backend.app) as client:
            response = client.get("/api/matches", params={"days": 1})
        assert response.status_code == 503, f"beklenen 503, gelen {response.status_code}"
        assert response.json()["error"]["code"] == "api_key_missing"
        return "anahtar yokken net hata donuyor"
    finally:
        os.environ["API_FOOTBALL_KEY"] = saved
        import importlib

        import backend
        import config as config_module
        import database as database_module

        config_module.get_settings(refresh=True)
        database_module.get_database(refresh=True)
        importlib.reload(backend)


# ==========================================================================
# 6-8  Canli API (anahtar varsa)
# ==========================================================================
def check_live_api() -> str:
    from config import get_settings

    settings = get_settings(refresh=True)
    if not settings.api_key_configured:
        raise SkipCheck("API_FOOTBALL_KEY tanimli degil")

    async def run():
        from api_client import ApiFootballClient, CallBudget
        from bookmakers import BookmakerResolver
        from database import Database

        database = Database(settings)
        database.init_schema()
        client = ApiFootballClient(settings, database)
        resolver = BookmakerResolver(client, database, settings)
        budget = CallBudget(limit=20)
        try:
            bookmaker = await resolver.resolve(budget=budget)
            season, info = await client.current_season(settings.league_ids[0], budget=budget)
            return bookmaker, season, info, client.quota
        finally:
            await client.aclose()

    bookmaker, season, info, quota = asyncio.run(run())
    assert bookmaker is not None, "Bet365 bulunamadi"
    assert season is not None, "guncel sezon tespit edilemedi"
    return (
        f"{bookmaker.name} id={bookmaker.id}, {info.get('name')} sezon {season}, "
        f"kalan kota {quota.daily_remaining}"
    )


def check_deployed_backend() -> str:
    """CANLI Render backend'i GERCEKTEN bizim kodumuz mu?

    Bu kontrol tam olarak su olayi onlemek icin var: Render'da baska bir
    uygulama ("MACANALIZ PRO API 2.0") calisiyordu, frontend ona baglaniyordu
    ve hata "Sunucu 400 dondurdu" gibi anlamsiz gorunuyordu.

    BACKEND_URL tanimli degilse atlanir.
    """
    import httpx

    base = os.environ.get("BACKEND_URL", "").strip().rstrip("/")
    if not base:
        raise SkipCheck("BACKEND_URL tanimli degil (canli deploy dogrulanmadi)")

    import backend

    expected = backend.API_CONTRACT

    try:
        with httpx.Client(timeout=90) as client:
            config_response = client.get(f"{base}/api/config")
            health_response = client.get(f"{base}/api/health")
    except httpx.HTTPError as exc:
        raise AssertionError(f"{base} adresine ulasilamadi: {exc}") from exc

    assert config_response.status_code == 200, (
        f"{base}/api/config -> HTTP {config_response.status_code}. "
        "Bu adreste bizim backend'imiz calismiyor olabilir."
    )
    try:
        config_payload = config_response.json()
    except ValueError as exc:
        raise AssertionError(f"/api/config JSON degil: {config_response.text[:200]}") from exc

    found = config_payload.get("api_contract")
    assert found == expected, (
        f"sozlesme uyusmuyor: beklenen '{expected}', bulunan '{found}'. "
        f"Calisan servis: {config_payload.get('app')} {config_payload.get('version')}. "
        "Render'a GUNCEL kod deploy edilmemis olabilir."
    )

    assert health_response.status_code == 200, (
        f"/api/health -> HTTP {health_response.status_code}"
    )
    request_id = health_response.headers.get("X-Request-ID")
    assert request_id, "X-Request-ID header'i yok (guncel surum degil)"

    return (
        f"{base} -> {config_payload.get('app')} v{config_payload.get('version')}, "
        f"sozlesme {found}, rid={request_id}"
    )


class SkipCheck(Exception):
    pass


def check_with_skip(name: str, fn) -> None:
    try:
        detail = fn()
        record(name, PASS, detail or "")
    except SkipCheck as exc:
        record(name, SKIP, str(exc))
    except AssertionError as exc:
        record(name, FAIL, str(exc))
    except Exception as exc:
        record(name, FAIL, f"{type(exc).__name__}: {exc}")


# ==========================================================================
# 9-13  Saf mantik
# ==========================================================================
def check_odds_parsing() -> str:
    from odds_parser import market_1x2, parse_odds_rows

    row = {
        "fixture": {"id": 1, "date": "2026-09-07T18:00:00+00:00"},
        "bookmakers": [
            {"id": 6, "name": "Bwin", "bets": [
                {"name": "Match Winner", "values": [
                    {"value": "Home", "odd": "9.99"}, {"value": "Draw", "odd": "9.99"},
                    {"value": "Away", "odd": "9.99"}]}]},
            {"id": 8, "name": "Bet365", "bets": [
                {"name": "Match Winner", "values": [
                    {"value": "Home", "odd": "2.10"}, {"value": "Draw", "odd": "3.40"},
                    {"value": "Away", "odd": "3.20"}]}]},
        ],
    }
    parsed = parse_odds_rows([row], 8, "Bet365")
    odds = market_1x2(parsed.get(1))
    assert odds == {"1": 2.10, "X": 3.40, "2": 3.20}, f"beklenmeyen oran: {odds}"

    only_other = dict(row)
    only_other["bookmakers"] = [row["bookmakers"][0]]
    assert parse_odds_rows([only_other], 8, "Bet365") == {}, "baska bookmaker sizdi!"
    return "Bet365 dogru okundu, baska bookmaker sizmadi"


def check_analysis_engine() -> str:
    from analysis_engine import (
        calculate_implied_probability,
        calculate_overround,
        markets_from_matrix,
        normalize_probabilities,
        score_matrix,
    )

    implied = calculate_implied_probability({"1": 1.80, "X": 3.50, "2": 4.20})
    assert abs(implied["1"] - 0.5556) < 0.0001, "implied probability yanlis"
    assert abs(calculate_overround(implied) - 1.0794) < 0.0001, "overround yanlis"
    assert abs(sum(normalize_probabilities(implied).values()) - 1.0) < 1e-9

    matrix = score_matrix(1.7, 1.2, -0.05, 10)
    assert abs(sum(sum(row) for row in matrix) - 1.0) < 1e-9, "matris normalize degil"
    markets = markets_from_matrix(matrix)
    assert abs(sum(markets["match_result"].values()) - 1.0) < 1e-9
    assert abs(sum(markets["over_under_25"].values()) - 1.0) < 1e-9
    return "olasilik, overround ve marketler tutarli"


def check_prediction_parsing() -> str:
    from services import MatchService

    block = MatchService._prediction_block(
        None,  # type: ignore[arg-type]
        [{"predictions": {"winner": {"name": "X"}, "advice": "a",
                          "percent": {"home": "55%", "draw": "25%", "away": "20%"}}}],
    )
    assert block.available and abs(block.percent["1"] - 0.55) < 1e-9
    empty = MatchService._prediction_block(None, [])  # type: ignore[arg-type]
    assert not empty.available and empty.percent == {}
    return "predictions ayristirildi, bos veri uydurulmadi"


def check_database_and_snapshot() -> str:
    import config as config_module
    from database import Database, to_iso, utcnow
    from snapshots import capture_snapshot, movement_for

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    saved_path = os.environ.get("SQLITE_PATH")
    saved_url = os.environ.get("DATABASE_URL")
    os.environ["SQLITE_PATH"] = tmp.name
    os.environ.pop("DATABASE_URL", None)
    try:
        settings = config_module.get_settings(refresh=True)
        database = Database(settings)
        database.init_schema()

        entry = {
            "fixture_id": 1, "bookmaker_id": 8, "bookmaker_name": "Bet365",
            "kickoff": to_iso(utcnow() + timedelta(hours=3)),
            "markets": {"1x2": {"1": 2.40, "X": 3.30, "2": 3.00}},
        }
        assert capture_snapshot(database, entry, settings=settings), "ilk snapshot yazilamadi"
        entry["markets"]["1x2"]["1"] = 2.15
        assert capture_snapshot(database, entry, settings=settings), "degisen oran yazilmadi"

        movement = movement_for(database, 1, utcnow() + timedelta(hours=3))
        assert movement["available"], "hareket hesaplanamadi"
        assert abs(movement["change_pct"]["1"] + 10.42) < 0.01, "degisim yuzdesi yanlis"

        empty = movement_for(database, 999, None)
        assert not empty["available"] and empty["change_pct"] == {}, "snapshot yokken veri uretildi!"
        return "sema, snapshot yazimi ve hareket dogru"
    finally:
        # Basta tanimsizsa GERI EKLEME - eski surumde silinmis bir gecici
        # dosyanin yolu ortamda kaliyordu.
        for key, value in (("SQLITE_PATH", saved_path), ("DATABASE_URL", saved_url)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config_module.get_settings(refresh=True)
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# ==========================================================================
# 14-17  Frontend, CORS, sahte veri, sir sizintisi
# ==========================================================================
def check_frontend_files() -> str:
    required = ["index.html", "css/styles.css", "js/config.js", "js/format.js",
                "js/api.js", "js/ui.js", "js/app.js"]
    missing = [name for name in required if not (ROOT / name).exists()]
    assert not missing, f"eksik dosya: {missing}"

    unsafe = []
    for name in required:
        text = (ROOT / name).read_text(encoding="utf-8")
        for pattern in ("innerHTML", "outerHTML", "document.write", "eval("):
            for line in text.splitlines():
                if pattern in line and not line.strip().startswith(("*", "//", "/*")):
                    unsafe.append(f"{name}: {pattern}")
    assert not unsafe, f"guvensiz DOM kullanimi: {unsafe}"
    return f"{len(required)} dosya var, innerHTML kullanilmiyor"


def check_frontend_has_no_demo_fallback() -> str:
    text = (ROOT / "js" / "app.js").read_text(encoding="utf-8")
    banned = ["DEMO_DATA", "demoData", "fakeMatches", "Math.random"]
    found = [word for word in banned if word in text]
    assert not found, f"demo/sahte veri izi: {found}"
    return "API hatasinda demo veriye dusulmuyor"


def check_cors_configured() -> str:
    import backend

    names = [middleware.cls.__name__ for middleware in backend.app.user_middleware]
    assert "CORSMiddleware" in names, "CORS middleware yok"
    return "CORSMiddleware ALLOWED_ORIGINS ile yapilandirilmis"


def check_no_fake_data_generators() -> str:
    suspicious = re.compile(r"\b(random\.(uniform|random|randint|choice)|faker|Faker)\b")
    offenders = []
    for path in ROOT.glob("*.py"):
        if path.name.startswith("test"):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if suspicious.search(line):
                offenders.append(f"{path.name}:{number}")
    assert not offenders, f"rastgele veri uretimi: {offenders}"
    return "kaynak kodda rastgele veri uretimi yok"


def check_no_hardcoded_secrets() -> str:
    patterns = [
        re.compile(r"API_FOOTBALL_KEY\s*=\s*[\"'][^\"'\s]{8,}[\"']"),
        re.compile(r"x-apisports-key\s*[:=]\s*[\"'][A-Za-z0-9]{16,}[\"']"),
    ]
    offenders = []
    for path in list(ROOT.glob("*.py")) + list((ROOT / "js").glob("*.js")):
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            if pattern.search(text):
                offenders.append(path.name)
    assert not offenders, f"kodda gomulu anahtar suphesi: {offenders}"

    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in gitignore, ".gitignore icinde .env yok"
    return "kodda gomulu anahtar yok, .env gitignore'da"


def check_bookmaker_id_not_hardcoded() -> str:
    text = (ROOT / "bookmakers.py").read_text(encoding="utf-8")
    assert "find_bookmaker" in text, "bookmaker kesif fonksiyonu yok"
    for path in ["services.py", "api_client.py", "odds_parser.py"]:
        source = (ROOT / path).read_text(encoding="utf-8")
        assert not re.search(r"bookmaker_id\s*=\s*\d+", source), f"{path} icinde sabit ID"
    return "Bet365 ID'si calisma aninda kesfediliyor"


def check_fatal_errors_not_swallowed() -> str:
    """Anahtar yok/gecersizken servis bos liste degil, HATA dondurmeli.

    Regresyon korumasi: eski surumde ``list_matches`` bu hatalari uyariya
    cevirip 0 mac donduruyordu; kullanici bunu "bugun mac yok" saniyordu.
    """
    import asyncio

    from errors import (
        ApiForbiddenError,
        ApiKeyInvalidError,
        ApiKeyMissingError,
        ApiQuotaExceededError,
        CallBudgetExhaustedError,
        SeasonNotAccessibleError,
        error_from_payload,
    )

    for cls in (ApiKeyMissingError, ApiKeyInvalidError, ApiForbiddenError):
        assert cls.fatal, f"{cls.__name__} fatal olmali"
    for cls in (ApiQuotaExceededError, CallBudgetExhaustedError, SeasonNotAccessibleError):
        assert not cls.fatal, f"{cls.__name__} fatal OLMAMALI (kismi veri gosterilir)"

    # Plan sezon kisiti kendi tipine dusmeli ve saglayicinin mesajini tasimali.
    plan_error = error_from_payload(
        {"plan": "Free plans do not have access to this season, from 2022"}
    )
    assert isinstance(plan_error, SeasonNotAccessibleError), (
        "plan sezon kisiti SeasonNotAccessibleError'a donusmuyor"
    )
    assert "this season" in (plan_error.provider_message or ""), (
        "saglayicinin mesaji tasinmiyor"
    )
    payload = plan_error.to_payload()["error"]
    for key in ("provider_message", "hint", "season"):
        assert key in payload, f"hata govdesinde {key} yok"
    # Endpoint kapsami kisiti ile karistirilmamali
    assert not isinstance(
        error_from_payload({"plan": "Your plan does not allow this endpoint"}),
        SeasonNotAccessibleError,
    ), "endpoint kapsami kisiti sezon kisiti sanildi"

    saved = os.environ.get("API_FOOTBALL_KEY", "")
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    saved_path = os.environ.get("SQLITE_PATH")
    os.environ["API_FOOTBALL_KEY"] = ""
    os.environ["SQLITE_PATH"] = tmp.name
    try:
        import config as config_module
        from api_client import ApiFootballClient
        from bookmakers import BookmakerResolver
        from database import Database
        from services import MatchService

        settings = config_module.get_settings(refresh=True)
        database = Database(settings)
        database.init_schema()
        client = ApiFootballClient(settings, database)
        resolver = BookmakerResolver(client, database, settings)
        service = MatchService(client, database, resolver, settings)

        async def run():
            from datetime import date

            try:
                await service.list_matches(settings.league_ids[:1], date.today(), date.today())
            except ApiKeyMissingError:
                return True
            finally:
                await client.aclose()
            return False

        raised = asyncio.run(run())
        assert raised, "anahtar yokken list_matches sessizce bos liste dondurdu"
        return "anahtar/kimlik hatalari ve plan sezon kisiti dogru siniflandiriliyor"
    finally:
        os.environ["API_FOOTBALL_KEY"] = saved
        if saved_path is None:
            os.environ.pop("SQLITE_PATH", None)
        else:
            os.environ["SQLITE_PATH"] = saved_path
        import config as config_module

        config_module.get_settings(refresh=True)
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def check_no_todo_left() -> str:
    offenders = []
    for path in list(ROOT.glob("*.py")) + list((ROOT / "js").glob("*.js")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\b(TODO|FIXME|XXX|sonra yaparim)\b", line):
                offenders.append(f"{path.name}:{number}")
    assert not offenders, f"TODO birakilmis: {offenders}"
    return "TODO/FIXME yok"


def check_tests_pass() -> str:
    """Test paketini TEMIZ bir ortamda calistirir.

    Preflight kendi surecinde SNAPSHOT_ENABLED=false gibi ayarlar yapiyor;
    bunlar alt surece sizarsa testler yanlislikla duser. Bu yuzden testlerin
    kendi belirledigi degiskenler ortamdan cikarilir.

    Basarisizlik halinde HANGI testlerin dustugu raporlanir - "failures=3"
    demek yeterli degil.
    """
    import subprocess

    env = os.environ.copy()
    for key in (
        "SNAPSHOT_ENABLED", "SNAPSHOT_MIN_GAP_MINUTES", "SQLITE_PATH", "DATABASE_URL",
        "LEAGUE_IDS", "ALLOWED_ORIGINS", "API_TIMEOUT_SECONDS", "API_MAX_RETRIES",
    ):
        env.pop(key, None)

    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
        cwd=str(ROOT), capture_output=True, text=True, env=env,
    )
    output = (completed.stdout or "") + (completed.stderr or "")

    if completed.returncode != 0:
        failing = [
            line.strip()
            for line in output.splitlines()
            if line.startswith(("FAIL:", "ERROR:"))
        ]
        if failing:
            print("\n     Dusen testler:")
            for line in failing:
                print("       - " + line)
        summary = [line.strip() for line in output.splitlines() if line.startswith("FAILED")]
        raise AssertionError(
            (summary[0] if summary else "test hatasi")
            + (f"  |  {len(failing)} test: " + "; ".join(failing[:3]) if failing else "")
        )

    summary = [
        line.strip()
        for line in output.splitlines()
        if line.strip().startswith(("Ran ", "OK"))
    ]
    return " / ".join(summary)


# ==========================================================================
def main() -> int:
    print("=" * 68)
    print("MACANALIZ PRO - deploy oncesi kontrol listesi")
    print("=" * 68)

    check("1.  backend import ediliyor", check_backend_imports)
    check("2.  FastAPI app ve endpointler tanimli", check_app_starts)
    check("3.  /api/health 200", check_health)
    check("4.  /api/status calisiyor (checks dolu)", check_status)
    check("5.  API key yokken /api/matches 503 doner", check_missing_key_error)
    check_with_skip("6.  Canli API + Bet365 kesfi + sezon", check_live_api)
    check("7.  Oran ayristirma (Bet365 filtresi)", check_odds_parsing)
    check("8.  Analiz motoru matematigi", check_analysis_engine)
    check("9.  Predictions ayristirma", check_prediction_parsing)
    check("10. Veritabani ve snapshot", check_database_and_snapshot)
    check("11. Yapilandirma + plan hatalari yutulmuyor", check_fatal_errors_not_swallowed)
    check("12. Bookmaker ID'si hard-code degil", check_bookmaker_id_not_hardcoded)
    check("13. Frontend dosyalari + guvenli DOM", check_frontend_files)
    check("14. Frontend'de demo fallback yok", check_frontend_has_no_demo_fallback)
    check("15. CORS yapilandirilmis", check_cors_configured)
    check("16. Sahte veri ureteci yok", check_no_fake_data_generators)
    check("17. Sir sizintisi yok", check_no_hardcoded_secrets)
    check("18. TODO birakilmamis", check_no_todo_left)
    check_with_skip("19. Canli deploy bizim kodumuz mu", check_deployed_backend)
    check("20. Test paketi geciyor", check_tests_pass)

    failed = [name for name, status, _ in results if status == FAIL]
    skipped = [name for name, status, _ in results if status == SKIP]

    print("\n" + "=" * 68)
    print(f"PASS: {len(results) - len(failed) - len(skipped)}   "
          f"FAIL: {len(failed)}   SKIP: {len(skipped)}")
    if failed:
        print("\nBASARISIZ MADDELER - DEPLOY ETMEYIN:")
        for name in failed:
            print("  - " + name)
        return 1
    if skipped:
        print("\nAtlanan maddeler (anahtar tanimlayip tekrar calistirin):")
        for name in skipped:
            print("  - " + name)
    print("\nTum zorunlu kontroller gecti.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
