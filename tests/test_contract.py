"""Sozlesme (contract) statik kontrolleri.

Burada FastAPI gerekmez: kaynak kod AST olarak okunur.

Yakalanan hata sinifi
---------------------
Bir endpoint bir alani HESAPLAR ama cevap modeline GECIRMEYI unutur. Model
alaninin varsayilani oldugu icin ne Python ne de Pydantic sikayet eder;
frontend sessizce bos veri gorur. Tam olarak ``/api/status`` icindeki
``checks`` alaninda yasanan seydi: "Bağlantı Durumu" paneli bos kaliyordu.
"""

from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import models as models_module  # noqa: E402


def _response_models() -> dict[str, set[str]]:
    """models.py icindeki Pydantic modellerini ve alan adlarini toplar."""
    result: dict[str, set[str]] = {}
    for name in dir(models_module):
        obj = getattr(models_module, name)
        fields = getattr(obj, "model_fields", None)
        if isinstance(fields, dict) and fields:
            result[name] = set(fields.keys())
    return result


def _assigned_names(node: ast.AST, before_line: int | None = None) -> set[str]:
    """Fonksiyonda atanan yerel degisken adlari.

    ``before_line`` verilirse yalnizca O SATIRDAN ONCE atananlar dondurulur.
    Bu sart olmazsa erken ``return`` dallari yanlis alarm uretir: fonksiyonun
    ilerisinde hesaplanan bir degisken, ondan once donen bir dal icin
    "unutulmus" gibi gorunur.
    """
    names: set[str] = set()

    def add(target: ast.AST, lineno: int) -> None:
        if before_line is not None and lineno >= before_line:
            return
        if isinstance(target, ast.Name):
            names.add(target.id)

    for child in ast.walk(node):
        if isinstance(child, ast.Assign):
            for target in child.targets:
                add(target, child.lineno)
        elif isinstance(child, ast.AnnAssign):
            add(child.target, child.lineno)
        elif isinstance(child, (ast.For, ast.AsyncFor)):
            add(child.target, child.lineno)
    return names


class TestResponseModelsAreFullyPopulated(unittest.TestCase):
    """Hesaplanan her alan cevap modeline gecirilmeli."""

    MODELS = _response_models()
    SOURCES = ("backend.py", "services.py")

    def test_no_computed_field_is_silently_dropped(self) -> None:
        problems: list[str] = []

        for filename in self.SOURCES:
            tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
            for function in ast.walk(tree):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for call in ast.walk(function):
                    if not isinstance(call, ast.Call):
                        continue
                    if not isinstance(call.func, ast.Name):
                        continue
                    fields = self.MODELS.get(call.func.id)
                    if not fields:
                        continue
                    # ** ile aciliyorsa (ör. MatchDetail(**summary.model_dump()))
                    # alanlar dinamik gelir, atlanir.
                    if any(keyword.arg is None for keyword in call.keywords):
                        continue

                    # Yalnizca CAGRIDAN ONCE hesaplanmis degiskenler sayilir.
                    locals_here = _assigned_names(function, before_line=call.lineno)
                    passed = {keyword.arg for keyword in call.keywords if keyword.arg}
                    forgotten = (fields - passed) & locals_here
                    for field in sorted(forgotten):
                        problems.append(
                            f"{filename}:{call.lineno} {call.func.id}(...) -> "
                            f"'{field}' fonksiyonda hesaplaniyor ama modele gecirilmiyor"
                        )

        self.assertEqual(
            problems,
            [],
            "Hesaplanip cevaba konmayan alan(lar):\n  " + "\n  ".join(problems),
        )

    def test_the_check_itself_detects_a_planted_bug(self) -> None:
        """Kontrolun ise yaradigini kanitlar (test'in testi)."""
        source = (
            "def handler():\n"
            "    checks = [1, 2, 3]\n"
            "    warnings = []\n"
            "    return StatusResponse(backend='ok', warnings=warnings)\n"
        )
        tree = ast.parse(source)
        function = tree.body[0]
        call = next(node for node in ast.walk(function) if isinstance(node, ast.Call))
        locals_here = _assigned_names(function, before_line=call.lineno)
        passed = {keyword.arg for keyword in call.keywords if keyword.arg}
        forgotten = (self.MODELS["StatusResponse"] - passed) & locals_here
        self.assertIn("checks", forgotten)
        self.assertNotIn("warnings", forgotten)


class TestStatusContract(unittest.TestCase):
    """/api/status'un frontend'in kullandigi alanlari tasidigini garanti eder."""

    def test_status_model_has_the_fields_the_frontend_reads(self) -> None:
        fields = set(models_module.StatusResponse.model_fields.keys())
        for name in ("api_key_configured", "api_connected", "bet365_available",
                     "database", "database_persistent", "quota", "warnings", "checks",
                     "leagues", "version"):
            self.assertIn(name, fields)

    def test_frontend_only_reads_fields_that_exist(self) -> None:
        """app.js'in status'tan okudugu her alan modelde bulunmali."""
        import re

        source = (ROOT / "js" / "app.js").read_text(encoding="utf-8")
        used = set(re.findall(r"\bstatus\.([a-z_]+)\b", source))
        fields = set(models_module.StatusResponse.model_fields.keys())
        missing = sorted(used - fields)
        self.assertEqual(missing, [], f"app.js olmayan alan(lar)i okuyor: {missing}")

    def test_status_carries_plan_access_block(self) -> None:
        """Plan/sezon erisim durumu sozlesmenin parcasi olmali."""
        fields = set(models_module.StatusResponse.model_fields.keys())
        self.assertIn("plan", fields)
        plan_fields = set(models_module.PlanAccess.model_fields.keys())
        for name in ("checked", "season_access_ok", "requested_season",
                     "provider_message", "newest_accessible_season", "tried", "note"):
            self.assertIn(name, plan_fields)

    def test_frontend_reads_plan_fields_that_exist(self) -> None:
        import re

        source = (ROOT / "js" / "ui.js").read_text(encoding="utf-8")
        used = set(re.findall(r"\bplan\.([a-z_]+)\b", source))
        plan_fields = set(models_module.PlanAccess.model_fields.keys())
        missing = sorted(used - plan_fields)
        self.assertEqual(missing, [], f"ui.js olmayan plan alanini okuyor: {missing}")

    def test_match_summary_carries_provenance_on_every_block(self) -> None:
        """Her veri blogu nereden geldigini soylemeli."""
        for name in ("Bet365Block", "ImpliedProbabilities", "ModelBlock", "ValueEdge",
                     "OddsMovement", "MarketProbabilities", "TeamFormBlock", "H2HBlock",
                     "InjuriesBlock", "ApiPredictionBlock"):
            model = getattr(models_module, name)
            self.assertIn("source", model.model_fields, f"{name} 'source' tasimiyor")
            self.assertIn("available", model.model_fields, f"{name} 'available' tasimiyor")


class TestBackendFrontendHandshake(unittest.TestCase):
    """Frontend ile backend ayni sozlesme surumunde olmali.

    Gercek olay: Render'da BASKA bir uygulama ("MACANALIZ PRO API 2.0")
    calisiyordu; frontend bunu "Sunucu 400 dondurdu" diye gosteriyordu.
    /api/config artik bir sozlesme isareti donduruyor ve frontend onu
    dogruluyor. Bu test iki tarafin ayni degeri kullandigini garanti eder.
    """

    def _backend_contract(self) -> str:
        import re

        source = (ROOT / "backend.py").read_text(encoding="utf-8")
        match = re.search(r'API_CONTRACT\s*=\s*"([^"]+)"', source)
        self.assertIsNotNone(match, "backend.py icinde API_CONTRACT yok")
        return match.group(1)  # type: ignore[union-attr]

    def _frontend_contract(self) -> str:
        import re

        source = (ROOT / "js" / "config.js").read_text(encoding="utf-8")
        match = re.search(r'apiContract\s*:\s*"([^"]+)"', source)
        self.assertIsNotNone(match, "js/config.js icinde apiContract yok")
        return match.group(1)  # type: ignore[union-attr]

    def test_contract_versions_match(self) -> None:
        self.assertEqual(
            self._backend_contract(),
            self._frontend_contract(),
            "backend ve frontend sozlesme surumleri farkli",
        )

    def test_config_endpoint_returns_the_contract(self) -> None:
        source = (ROOT / "backend.py").read_text(encoding="utf-8")
        self.assertIn('"api_contract": API_CONTRACT', source,
                      "/api/config sozlesme isaretini dondurmuyor")

    def test_frontend_verifies_the_contract(self) -> None:
        source = (ROOT / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn("api_contract", source, "frontend sozlesmeyi dogrulamiyor")
        self.assertIn("contract_mismatch", source)


class TestErrorReportingRules(unittest.TestCase):
    """Backend anlamli bir HTTP cevabi dondurduyse 'ulasilamadi' denmez."""

    def test_frontend_separates_transport_failure_from_http_response(self) -> None:
        api_source = (ROOT / "js" / "api.js").read_text(encoding="utf-8")
        app_source = (ROOT / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn("transport", api_source, "ApiError transport ayrimi tasimiyor")
        self.assertIn("error.transport", app_source,
                      "app.js transport ayrimini kullanmiyor")

    def test_unreachable_title_is_only_used_for_transport_failures(self) -> None:
        """'Backend'e ulasilamadi' basligi yalnizca transport dalinda olmali."""
        source = (ROOT / "js" / "app.js").read_text(encoding="utf-8")
        occurrences = source.count("Backend'e ulaşılamadı")
        self.assertEqual(
            occurrences, 1,
            "bu baslik birden fazla yerde kullanilmis; HTTP cevabi olan "
            "durumlarda gercek sebebi gizleyebilir",
        )
        index = source.index("Backend'e ulaşılamadı")
        window = source[max(0, index - 200):index]
        self.assertIn("error.transport", window,
                      "baslik transport kontrolunun icinde degil")

    def test_frontend_surfaces_diagnostics(self) -> None:
        source = (ROOT / "js" / "app.js").read_text(encoding="utf-8")
        for needle in ("error.url", "error.status", "error.requestId", "bodyExcerpt"):
            self.assertIn(needle, source, f"teshis alani gosterilmiyor: {needle}")

    def test_backend_wraps_404_and_422_in_the_same_envelope(self) -> None:
        source = (ROOT / "backend.py").read_text(encoding="utf-8")
        self.assertIn("StarletteHTTPException", source, "404 zarfi yok")
        self.assertIn("RequestValidationError", source, "422 zarfi yok")
        self.assertIn("X-Request-ID", source, "request-id yok")
        self.assertIn("expose_headers", source,
                      "X-Request-ID CORS'ta expose edilmiyor - tarayici okuyamaz")


if __name__ == "__main__":
    unittest.main(verbosity=2)
