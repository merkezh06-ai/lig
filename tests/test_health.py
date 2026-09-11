"""Backend HTTP katmani testleri.

FastAPI kurulu degilse bu dosya atlanir (kalan testler yine calisir).
Kurulumdan sonra: ``python -m unittest tests.test_health``
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from fastapi.testclient import TestClient  # type: ignore

    FASTAPI_AVAILABLE = True
except Exception:  # pragma: no cover
    FASTAPI_AVAILABLE = False

SECRET = "super-secret-key-should-never-leak"


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI kurulu degil (pip install -r requirements.txt)")
class TestBackendHttp(unittest.TestCase):
    ENV_KEYS = ("SQLITE_PATH", "API_FOOTBALL_KEY", "SNAPSHOT_ENABLED",
                "ALLOWED_ORIGINS", "DATABASE_URL", "LEAGUE_IDS",
                "API_TIMEOUT_SECONDS", "API_MAX_RETRIES")

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls.tmp.close()
        # Ortami kaydet: bu modul SNAPSHOT_ENABLED=false yaziyor ve eskiden
        # bunu geri almayip sonraki test modullerini dusuruyordu.
        cls._saved_env = {key: os.environ.get(key) for key in cls.ENV_KEYS}
        os.environ["SQLITE_PATH"] = cls.tmp.name
        os.environ["API_FOOTBALL_KEY"] = SECRET
        os.environ["SNAPSHOT_ENABLED"] = "false"
        os.environ["ALLOWED_ORIGINS"] = "https://example.github.io"
        os.environ.pop("DATABASE_URL", None)
        # Sahte anahtarla gercek API'ye gidilirse hizlica pes etsin; bu testler
        # ag erisimine BAGIMLI DEGILDIR, endpointlerin yine de cevap vermesini
        # ve sir sizdirmamasini olcerler.
        os.environ["API_TIMEOUT_SECONDS"] = "3"
        os.environ["API_MAX_RETRIES"] = "0"

        import config as config_module
        import database as database_module

        config_module.get_settings(refresh=True)
        database_module.get_database(refresh=True)

        import backend

        cls.backend = backend
        # DIKKAT: TestClient lifespan'i YALNIZCA context manager olarak
        # calistirir. Girilmezse app.state.service tanimsiz kalir.
        cls._ctx = TestClient(backend.app)
        cls.client = cls._ctx.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls._ctx.__exit__(None, None, None)
        except Exception:
            pass
        for key, value in cls._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import config as config_module

        config_module.get_settings(refresh=True)
        try:
            os.unlink(cls.tmp.name)
        except OSError:
            pass

    # ---------------------------------------------------------------- health
    def test_health_returns_200(self) -> None:
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertIn("version", payload)

    def test_config_has_no_secret(self) -> None:
        response = self.client.get("/api/config")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(SECRET, response.text)
        self.assertIn("disclaimer", response.json())

    def test_config_returns_the_api_contract(self) -> None:
        payload = self.client.get("/api/config").json()
        self.assertIn("api_contract", payload)
        self.assertTrue(payload["api_contract"].startswith("macanaliz-pro/"))

    def test_unknown_path_returns_our_error_envelope(self) -> None:
        """404 de {"error": {...}} donmeli.

        Aksi halde frontend mesaj bulamayip "Sunucu 404 dondurdu" gibi bos bir
        metin gosteriyordu ve gercek sebep kayboluyordu.
        """
        response = self.client.get("/api/bu-adres-yok")
        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertIn("error", payload)
        self.assertEqual(payload["error"]["code"], "http_404")
        self.assertIn("tanimiyor", payload["error"]["message"])
        self.assertIn("expected_contract", payload["error"])

    def test_wrong_method_returns_envelope(self) -> None:
        response = self.client.post("/api/health")
        self.assertEqual(response.status_code, 405)
        self.assertIn("error", response.json())

    def test_validation_error_uses_our_envelope(self) -> None:
        response = self.client.get("/api/matches", params={"days": 999})
        self.assertEqual(response.status_code, 422)
        payload = response.json()
        self.assertIn("error", payload)
        self.assertEqual(payload["error"]["code"], "validation_error")
        self.assertTrue(payload["error"]["problems"])

    def test_every_response_carries_a_request_id(self) -> None:
        """Render logundaki satiri tarayicidaki hatayla eslestirebilmek icin."""
        for path in ("/api/health", "/api/config", "/api/bu-adres-yok"):
            response = self.client.get(path)
            self.assertTrue(
                response.headers.get("X-Request-ID"),
                f"{path} icin X-Request-ID yok",
            )

    def test_config_lists_leagues_and_bookmaker(self) -> None:
        payload = self.client.get("/api/config").json()
        self.assertTrue(payload["leagues"])
        self.assertEqual(payload["bookmaker_name"], "Bet365")

    # ---------------------------------------------------------------- gizlilik
    def test_no_endpoint_ever_returns_the_api_key(self) -> None:
        """Hicbir endpoint anahtari (veya anahtar tasiyan bir alani) dondurmemeli.

        NOT: duz metin araması ('"key"' gibi) burada yanlis alarm uretir -
        /api/status'un ``checks`` listesinde ``"key": "backend"`` gibi zararsiz
        alanlar var. Bu yuzden gercek sizinti kaliplari aranir: kimlik bilgisi
        adi tasiyan bir alanin DOLU bir string degeri.
        """
        leak_pattern = re.compile(
            r'"(api[_-]?key|apikey|x-apisports-key|token|secret|password|authorization)"'
            r'\s*:\s*"[^"]{4,}"',
            re.IGNORECASE,
        )
        for path in ["/api/health", "/api/config", "/api/status", "/api/quota",
                     "/api/capabilities", "/api/bookmakers", "/api/leagues",
                     "/api/calibration"]:
            response = self.client.get(path)
            self.assertNotIn(SECRET, response.text, f"{path} anahtari sizdirdi!")
            self.assertNotIn(SECRET.lower(), response.text.lower(), f"{path} sizdirdi!")
            match = leak_pattern.search(response.text)
            self.assertIsNone(
                match, f"{path} kimlik bilgisi gibi bir alan dondurdu: {match}"
            )

    def test_leak_pattern_would_actually_catch_a_leak(self) -> None:
        """Yukaridaki kontrolun ise yaradigini dogrular (test'in testi)."""
        leak_pattern = re.compile(
            r'"(api[_-]?key|apikey|x-apisports-key|token|secret|password|authorization)"'
            r'\s*:\s*"[^"]{4,}"',
            re.IGNORECASE,
        )
        self.assertIsNotNone(leak_pattern.search('{"api_key": "abc12345"}'))
        self.assertIsNotNone(leak_pattern.search('{"x-apisports-key":"abc12345"}'))
        # Zararsiz alanlar tetiklememeli
        self.assertIsNone(leak_pattern.search('{"key": "backend"}'))
        self.assertIsNone(leak_pattern.search('{"api_key_configured": true}'))

    def test_status_reports_key_as_boolean_only(self) -> None:
        payload = self.client.get("/api/status").json()
        self.assertIs(payload["api_key_configured"], True)
        self.assertIsInstance(payload["bet365_available"], bool)
        self.assertIn("database", payload)
        self.assertNotIn(SECRET, json.dumps(payload))

    # ---------------------------------------------------------------- dogrulama
    def test_invalid_league_id_returns_400(self) -> None:
        response = self.client.get("/api/matches", params={"leagues": "abc"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "validation_error")

    def test_days_out_of_range_returns_422_with_message(self) -> None:
        response = self.client.get("/api/matches", params={"days": 999})
        self.assertEqual(response.status_code, 422)
        self.assertIn("error", response.json())

    def test_bad_date_format_returns_400(self) -> None:
        response = self.client.get("/api/matches", params={"from": "07-09-2026"})
        self.assertEqual(response.status_code, 400)

    def test_reversed_date_range_returns_400(self) -> None:
        response = self.client.get(
            "/api/matches", params={"from": "2026-09-10", "to": "2026-09-01"}
        )
        self.assertEqual(response.status_code, 400)

    def test_negative_fixture_id_is_rejected(self) -> None:
        response = self.client.get("/api/match/-5")
        self.assertIn(response.status_code, (400, 404, 422))

    def test_errors_never_leak_a_traceback(self) -> None:
        response = self.client.get("/api/matches", params={"leagues": "abc"})
        self.assertNotIn("Traceback", response.text)
        self.assertNotIn("File \"", response.text)

    # ---------------------------------------------------------------- CORS
    def test_cors_header_is_present_for_allowed_origin(self) -> None:
        response = self.client.get(
            "/api/health", headers={"Origin": "https://example.github.io"}
        )
        self.assertEqual(
            response.headers.get("access-control-allow-origin"), "https://example.github.io"
        )

    def test_request_id_is_exposed_to_the_browser(self) -> None:
        """Cross-origin'de okunabilmesi icin expose edilmeli."""
        response = self.client.get(
            "/api/health", headers={"Origin": "https://example.github.io"}
        )
        exposed = (response.headers.get("access-control-expose-headers") or "").lower()
        self.assertIn("x-request-id", exposed)

    def test_preflight_is_answered(self) -> None:
        response = self.client.options(
            "/api/matches",
            headers={
                "Origin": "https://example.github.io",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertIn(response.status_code, (200, 204))


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI kurulu degil")
class TestMissingApiKey(unittest.TestCase):
    """Anahtar yokken uygulama ayakta kalmali ve bunu acikca soylemeli."""

    ENV_KEYS = ("SQLITE_PATH", "API_FOOTBALL_KEY", "SNAPSHOT_ENABLED", "DATABASE_URL",
                "API_TIMEOUT_SECONDS", "API_MAX_RETRIES")

    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self._saved_env = {key: os.environ.get(key) for key in self.ENV_KEYS}
        os.environ["SQLITE_PATH"] = self.tmp.name
        os.environ["API_FOOTBALL_KEY"] = ""
        os.environ["SNAPSHOT_ENABLED"] = "false"
        os.environ.pop("DATABASE_URL", None)
        os.environ["API_TIMEOUT_SECONDS"] = "3"
        os.environ["API_MAX_RETRIES"] = "0"

        import config as config_module
        import database as database_module

        config_module.get_settings(refresh=True)
        database_module.get_database(refresh=True)

        import importlib

        import backend

        importlib.reload(backend)
        self._ctx = TestClient(backend.app)
        self.client = self._ctx.__enter__()

    def tearDown(self) -> None:
        try:
            self._ctx.__exit__(None, None, None)
        except Exception:
            pass
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import config as config_module

        config_module.get_settings(refresh=True)
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def test_health_still_works(self) -> None:
        self.assertEqual(self.client.get("/api/health").status_code, 200)

    def test_status_says_key_missing(self) -> None:
        payload = self.client.get("/api/status").json()
        self.assertFalse(payload["api_key_configured"])
        self.assertFalse(payload["api_connected"])
        self.assertFalse(payload["bet365_available"])
        self.assertTrue(any("API_FOOTBALL_KEY" in w for w in payload["warnings"]))

    def test_matches_returns_a_clear_error(self) -> None:
        response = self.client.get("/api/matches", params={"days": 1})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "api_key_missing")
        self.assertIn("API anahtari bulunamadi", response.json()["error"]["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
