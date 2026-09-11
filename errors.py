"""Hata tipleri ve kullaniciya gosterilecek Turkce mesajlar.

Kural: kullaniciya asla ham Python traceback'i gitmez. Her hata bir
``AppError``a cevrilir; ayrintili teknik bilgi yalnizca loga yazilir.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Uygulamanin kullaniciya gosterebilecegi hata.

    ``fatal`` alani cok onemlidir:

    * ``fatal=True``  -> yapilandirma/kimlik sorunu. HICBIR SEY calismaz, bu
      yuzden yutulmaz; istek hemen basarisiz olur ve kullanici nedeni gorur.
      (Aksi halde "API anahtari yok" durumu, ekranda "bu filtrelerde mac
      bulunamadi" gibi gorunur - tam olarak kacinmak istedigimiz sey.)
    * ``fatal=False`` -> kismi/gecici sorun. Elde edilen gercek veri gosterilir,
      sorun uyari olarak bildirilir.
    """

    code: str = "internal_error"
    http_status: int = 500
    user_message: str = "Beklenmeyen bir hata olustu."
    fatal: bool = False
    #: API-Football'in dondurdugu HAM HTTP kodu (teshis icin; 0 = bilinmiyor).
    upstream_status: int = 0

    def __init__(
        self,
        user_message: str | None = None,
        *,
        code: str | None = None,
        http_status: int | None = None,
        detail: str | None = None,
        fatal: bool | None = None,
    ) -> None:
        self.user_message = user_message or self.user_message
        self.code = code or self.code
        self.http_status = http_status or self.http_status
        self.detail = detail
        if fatal is not None:
            self.fatal = fatal
        super().__init__(self.user_message)

    def to_payload(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.user_message,
            }
        }


class ConfigurationError(AppError):
    code = "configuration_error"
    http_status = 500
    user_message = "Uygulama yapilandirmasi eksik."
    fatal = True


class ApiKeyMissingError(ConfigurationError):
    code = "api_key_missing"
    http_status = 503
    fatal = True
    user_message = (
        "API anahtari bulunamadi. Sunucuda API_FOOTBALL_KEY environment "
        "variable'i tanimli degil."
    )


class ApiKeyInvalidError(AppError):
    code = "api_key_invalid"
    http_status = 502
    fatal = True
    user_message = "API anahtari gecersiz. API-Football kimlik dogrulamasi basarisiz."


class ApiForbiddenError(AppError):
    code = "api_forbidden"
    http_status = 502
    fatal = True
    user_message = (
        "API-Football bu istegi reddetti. Planiniz bu endpoint'i kapsamiyor olabilir."
    )


class ApiQuotaExceededError(AppError):
    """Kota bitti.

    FATAL DEGILDIR: o ana kadar cekilmis GERCEK veri gosterilir, eksik kalan
    kisim uyari olarak bildirilir. Hicbir sey cekilemediyse ``services`` bu
    hatayi yine de yukari firlatir (bkz. list_matches).
    """

    code = "api_quota_exceeded"
    http_status = 429
    fatal = False
    user_message = "API-Football kota sinirina ulasildi. Kota yenilenene kadar bekleyin."


class SeasonNotAccessibleError(AppError):
    """API plani ISTENEN SEZONA erisim vermiyor.

    Bu bir kod hatasi degil, abonelik kisitidir. Ornek govde:
        {"plan": "Free plans do not have access to this season, from 2022"}

    FATAL DEGILDIR: bazi ligler/sezonlar erisilebilir olabilir. Hicbiri
    erisilemiyorsa ``services.list_matches`` bu hatayi yukari firlatir; boylece
    kullanici BOS LISTE degil, gercek sebebi gorur.

    ``provider_message`` API'nin kendi cumlesidir - kullaniciya aynen gosterilir.
    """

    code = "season_not_accessible"
    http_status = 502
    fatal = False
    user_message = (
        "API-Football planiniz bu sezonun verisine erisim vermiyor. "
        "Guncel sezon maclari bu planla cekilemez."
    )

    def __init__(
        self,
        user_message: str | None = None,
        *,
        provider_message: str | None = None,
        league_id: int | None = None,
        season: int | None = None,
        **kwargs: Any,
    ) -> None:
        self.provider_message = provider_message
        self.league_id = league_id
        self.season = season
        if user_message is None and season is not None:
            user_message = (
                f"API-Football planiniz {season} sezonuna erisim vermiyor. "
                "Guncel sezon maclari bu planla cekilemez."
            )
        super().__init__(user_message, **kwargs)

    def to_payload(self) -> dict[str, Any]:
        payload = super().to_payload()
        payload["error"]["provider_message"] = self.provider_message
        payload["error"]["season"] = self.season
        payload["error"]["hint"] = (
            "Plan yukseltilirse uygulama kod degisikligi olmadan guncel sezonu "
            "kullanir; sezon her zaman API'den tespit edilir."
        )
        return payload


class CallBudgetExhaustedError(AppError):
    """Tek bir yenileme icin ayrilan cagri butcesi doldu.

    Bu bir API hatasi degil, bizim koydugumuz guvenlik sinirimizdir; kotayi
    tek istekte tuketmeyi onler. Fatal degildir.
    """

    code = "budget_exhausted"
    http_status = 429
    fatal = False
    user_message = "Bu yenileme icin ayrilan API cagri butcesi doldu."


class ApiUnavailableError(AppError):
    code = "api_unavailable"
    http_status = 502
    user_message = "API-Football'a su anda ulasilamiyor."


class ApiTimeoutError(ApiUnavailableError):
    code = "api_timeout"
    user_message = "API-Football yanit vermedi (zaman asimi)."


class NotFoundError(AppError):
    code = "not_found"
    http_status = 404
    user_message = "Istenen kayit bulunamadi."


class FixtureNotFoundError(NotFoundError):
    code = "fixture_not_found"
    user_message = "Bu mac bulunamadi."


class BookmakerNotFoundError(AppError):
    code = "bookmaker_not_found"
    http_status = 503
    user_message = (
        "Bet365 bookmaker listesinde bulunamadi. Bu API anahtari/plani Bet365 "
        "oranlarini saglamiyor olabilir. Baska bookmaker Bet365 yerine kullanilmaz."
    )


class OddsUnavailableError(AppError):
    code = "odds_unavailable"
    http_status = 404
    user_message = "Bu mac icin Bet365 orani mevcut degil."


class InsufficientDataError(AppError):
    code = "insufficient_data"
    http_status = 200  # veri yoklugu bir hata degil, bir durumdur
    user_message = "Bu analiz icin yeterli veri bulunamadi."


class ValidationError(AppError):
    code = "validation_error"
    http_status = 400
    user_message = "Gonderilen parametreler gecersiz."


class DatabaseError(AppError):
    code = "database_error"
    http_status = 500
    user_message = "Veritabani islemi basarisiz oldu."


# --------------------------------------------------------------------------
# API-Football cevaplarindan hata uretimi
# --------------------------------------------------------------------------
def error_from_status(status_code: int, detail: str | None = None) -> AppError:
    """HTTP durum kodunu anlasilir bir hataya cevirir."""
    mapping: dict[int, type[AppError]] = {
        401: ApiKeyInvalidError,
        403: ApiForbiddenError,
        429: ApiQuotaExceededError,
        499: ApiQuotaExceededError,
        404: NotFoundError,
    }
    if status_code in mapping:
        return mapping[status_code](detail=detail)
    if 500 <= status_code < 600:
        return ApiUnavailableError(
            f"API-Football sunucu hatasi dondurdu (HTTP {status_code}).", detail=detail
        )
    return ApiUnavailableError(
        f"API-Football beklenmeyen bir cevap dondurdu (HTTP {status_code}).", detail=detail
    )


def error_from_payload(errors: Any) -> AppError | None:
    """API-Football HTTP 200 icinde de hata dondurebilir.

    ``errors`` bos liste ise sorun yok. Dolu bir dict/liste ise icerigine gore
    dogru hata tipi secilir.
    """
    if not errors:
        return None

    if isinstance(errors, dict):
        text = " ".join(f"{key}: {value}" for key, value in errors.items())
        keys = {key.lower() for key in errors}
    elif isinstance(errors, (list, tuple)):
        text = " ".join(str(item) for item in errors)
        keys = set()
    else:
        text = str(errors)
        keys = set()

    lowered = text.lower()

    if "token" in keys or "invalid api key" in lowered or "not authorized" in lowered:
        return ApiKeyInvalidError(detail=text)
    if "requests" in keys or "rate limit" in lowered or "reached the request limit" in lowered:
        return ApiQuotaExceededError(detail=text)
    if "plan" in keys or "subscription" in lowered or "not allowed" in lowered:
        # "Free plans do not have access to this season, from 2022" gibi
        # SEZON kisitini ayri bir tip olarak isaretle: bu, endpoint kapsami
        # sorunu degil, abonelik sezon araligi sorunudur ve kullaniciya
        # bambaska bir sey soylenmesi gerekir.
        if "season" in lowered:
            return SeasonNotAccessibleError(provider_message=text, detail=text)
        return ApiForbiddenError(detail=text)
    if "required" in lowered or "parameter" in lowered or "bug" in keys:
        return ValidationError(
            "API-Football istegi reddetti: gonderilen parametreler gecersiz.", detail=text
        )
    return ApiUnavailableError("API-Football bir hata dondurdu.", detail=text)
