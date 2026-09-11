"""MACANALIZ PRO - merkezi yapilandirma.

Buradaki her deger environment variable ile gecersiz kilinabilir. Kodun
icine sabit deger gomulmez; lig ID'leri, model agirliklari, cache sureleri
ve kota sinirlari hep buradan okunur.

GUVENLIK: API anahtari yalnizca ortam degiskeninden okunur, asla loglanmaz
ve asla HTTP cevabina yazilmaz.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Sequence
from zoneinfo import ZoneInfo

APP_NAME = "MACANALIZ PRO"
APP_VERSION = "1.0.0"


# --------------------------------------------------------------------------
# Ortam degiskeni yardimcilari
# --------------------------------------------------------------------------
def env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        logging.getLogger(__name__).warning(
            "%s degeri sayiya cevrilemedi (%r), varsayilan %s kullaniliyor", name, raw, default
        )
        return default


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        logging.getLogger(__name__).warning(
            "%s degeri sayiya cevrilemedi (%r), varsayilan %s kullaniliyor", name, raw, default
        )
        return default


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "evet"}


def env_list(name: str, default: Sequence[str]) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return list(default)
    return [part.strip() for part in raw.split(",") if part.strip()]


# --------------------------------------------------------------------------
# Ligler
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class LeagueConfig:
    """Bir lig icin varsayilan yapilandirma.

    ``expected_name`` yalnizca DOGRULAMA icindir. Arayuzde her zaman API'den
    donen gercek lig adi gosterilir; buradaki ad tutmuyorsa uygulama uyari
    verir (yanlis lig ID'si sessizce yanlis mac listesi uretmesin diye).
    """

    id: int
    slug: str
    expected_name: str
    country: str


DEFAULT_LEAGUES: tuple[LeagueConfig, ...] = (
    LeagueConfig(39, "premier-league", "Premier League", "England"),
    LeagueConfig(140, "la-liga", "La Liga", "Spain"),
    LeagueConfig(135, "serie-a", "Serie A", "Italy"),
    LeagueConfig(78, "bundesliga", "Bundesliga", "Germany"),
    LeagueConfig(61, "ligue-1", "Ligue 1", "France"),
    LeagueConfig(203, "super-lig", "Super Lig", "Turkey"),
)


def _resolve_leagues() -> tuple[LeagueConfig, ...]:
    """LEAGUE_IDS ile lig listesi daraltilabilir/genisletilebilir."""
    raw = os.environ.get("LEAGUE_IDS")
    if not raw or not raw.strip():
        return DEFAULT_LEAGUES

    known = {league.id: league for league in DEFAULT_LEAGUES}
    resolved: list[LeagueConfig] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            league_id = int(token)
        except ValueError:
            logging.getLogger(__name__).warning("LEAGUE_IDS icinde gecersiz deger: %r", token)
            continue
        resolved.append(
            known.get(league_id, LeagueConfig(league_id, f"lig-{league_id}", "", ""))
        )
    return tuple(resolved) if resolved else DEFAULT_LEAGUES


# --------------------------------------------------------------------------
# Cache sureleri (saniye)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class CacheTTL:
    bookmakers: int = env_int("TTL_BOOKMAKERS", 86_400)
    bets: int = env_int("TTL_BETS", 86_400)
    leagues: int = env_int("TTL_LEAGUES", 86_400)
    fixtures: int = env_int("TTL_FIXTURES", 300)
    fixtures_finished: int = env_int("TTL_FIXTURES_FINISHED", 21_600)
    odds: int = env_int("TTL_ODDS", 180)
    predictions: int = env_int("TTL_PREDICTIONS", 2_700)
    team_statistics: int = env_int("TTL_TEAM_STATISTICS", 2_700)
    h2h: int = env_int("TTL_H2H", 3_600)
    injuries: int = env_int("TTL_INJURIES", 1_800)
    standings: int = env_int("TTL_STANDINGS", 3_600)
    #: Plan sezon erisimi. Kisa tutulur: plan yukseltilirse uygulama en gec
    #: bu sure icinde kendiliginden guncel sezona geciser (kod degisikligi yok).
    season_access: int = env_int("TTL_SEASON_ACCESS", 3_600)


# --------------------------------------------------------------------------
# Model parametreleri
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ModelConfig:
    """Dixon-Coles gol modeli parametreleri.

    Model PIYASADAN BAGIMSIZDIR: Bet365 orani modele girdi olarak verilmez.
    Aksi halde "model / piyasa farki" dongusel bir sayi olurdu.
    """

    max_goals: int = env_int("MODEL_MAX_GOALS", 10)
    # Dixon-Coles dusuk skor duzeltmesi. Negatif deger 0-0 ve 1-1'i yukseltir.
    rho: float = env_float("MODEL_RHO", -0.05)
    # Kucuk orneklemde takim gucunu lig ortalamasina cekme katsayisi.
    shrinkage_k: float = env_float("MODEL_SHRINKAGE_K", 5.0)
    # Form icin bakilacak son mac sayisi ve ussel sonumleme katsayisi.
    form_matches: int = env_int("MODEL_FORM_MATCHES", 6)
    form_decay: float = env_float("MODEL_FORM_DECAY", 0.85)
    # Sezonluk guc ile son-form gucunun harmanlanma orani (0 = sadece sezon).
    form_weight: float = env_float("MODEL_FORM_WEIGHT", 0.35)
    # Bu sayidan az mac verisi varsa model calistirilmaz, "yetersiz veri" denir.
    min_matches: int = env_int("MODEL_MIN_MATCHES", 4)
    # Ilk yarim analizi icin gereken, ilk yari skoru bilinen mac sayisi.
    min_halftime_matches: int = env_int("MODEL_MIN_HT_MATCHES", 5)
    # Lambda degerleri icin guvenlik sinirlari (sayisal patlamayi onler).
    min_lambda: float = env_float("MODEL_MIN_LAMBDA", 0.15)
    max_lambda: float = env_float("MODEL_MAX_LAMBDA", 6.0)


@dataclass(frozen=True)
class ConfidenceWeights:
    """Guven skoru bilesenlerinin maksimum puanlari (toplam 100).

    Her bilesen yalnizca dayandigi veri GERCEKTEN varsa puan uretir.
    Veri yoksa 0 puan alir ve aciklamada "veri yok" olarak gosterilir.
    """

    model_probability: float = env_float("CONF_W_MODEL", 25.0)
    market_agreement: float = env_float("CONF_W_MARKET", 15.0)
    form: float = env_float("CONF_W_FORM", 15.0)
    home_away: float = env_float("CONF_W_HOME_AWAY", 10.0)
    h2h: float = env_float("CONF_W_H2H", 10.0)
    injuries: float = env_float("CONF_W_INJURIES", 10.0)
    odds_movement: float = env_float("CONF_W_ODDS_MOVEMENT", 7.0)
    data_quality: float = env_float("CONF_W_DATA_QUALITY", 8.0)

    def total(self) -> float:
        return (
            self.model_probability
            + self.market_agreement
            + self.form
            + self.home_away
            + self.h2h
            + self.injuries
            + self.odds_movement
            + self.data_quality
        )


@dataclass(frozen=True)
class DataQualityWeights:
    """Veri kalitesi puanlari (toplam 100). Guven skorundan AYRI bir olcudur."""

    bet365_odds: float = env_float("DQ_W_BET365", 20.0)
    recent_form: float = env_float("DQ_W_FORM", 15.0)
    h2h: float = env_float("DQ_W_H2H", 10.0)
    home_away_split: float = env_float("DQ_W_HOME_AWAY", 15.0)
    predictions: float = env_float("DQ_W_PREDICTIONS", 15.0)
    injuries: float = env_float("DQ_W_INJURIES", 10.0)
    odds_snapshot: float = env_float("DQ_W_SNAPSHOT", 15.0)

    def total(self) -> float:
        return (
            self.bet365_odds
            + self.recent_form
            + self.h2h
            + self.home_away_split
            + self.predictions
            + self.injuries
            + self.odds_snapshot
        )


# --------------------------------------------------------------------------
# Ana ayarlar
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Settings:
    # --- API-Football ---
    api_key: str = field(default_factory=lambda: env_str("API_FOOTBALL_KEY"))
    api_base_url: str = field(
        default_factory=lambda: env_str("API_FOOTBALL_BASE_URL", "https://v3.football.api-sports.io")
    )
    api_timeout: float = field(default_factory=lambda: env_float("API_TIMEOUT_SECONDS", 20.0))
    api_max_retries: int = field(default_factory=lambda: env_int("API_MAX_RETRIES", 2))

    # --- Bookmaker ---
    # Bet365 ID'si HARD-CODE EDILMEZ. Bu yalnizca /odds/bookmakers icinde
    # aranacak isimdir; gercek ID calisma aninda kesfedilir.
    bookmaker_name: str = field(default_factory=lambda: env_str("BOOKMAKER_NAME", "Bet365"))

    # --- Veritabani ---
    database_url: str = field(default_factory=lambda: env_str("DATABASE_URL"))
    sqlite_path: str = field(default_factory=lambda: env_str("SQLITE_PATH", "macanaliz.db"))

    # --- Sunucu ---
    port: int = field(default_factory=lambda: env_int("PORT", 8000))
    allowed_origins: list[str] = field(
        default_factory=lambda: env_list("ALLOWED_ORIGINS", ["*"])
    )
    log_level: str = field(default_factory=lambda: env_str("LOG_LEVEL", "INFO").upper())

    # --- Zaman ---
    timezone_name: str = field(default_factory=lambda: env_str("TIMEZONE", "Europe/Istanbul"))

    # --- Ligler ---
    leagues: tuple[LeagueConfig, ...] = field(default_factory=_resolve_leagues)

    # --- Kota korumasi ---
    max_calls_per_refresh: int = field(default_factory=lambda: env_int("MAX_CALLS_PER_REFRESH", 400))
    quota_reserve: int = field(default_factory=lambda: env_int("QUOTA_RESERVE", 40))
    deep_analysis_limit: int = field(default_factory=lambda: env_int("DEEP_ANALYSIS_LIMIT", 40))

    # --- Snapshot ---
    snapshot_enabled: bool = field(default_factory=lambda: env_bool("SNAPSHOT_ENABLED", True))
    snapshot_interval_minutes: int = field(
        default_factory=lambda: env_int("SNAPSHOT_INTERVAL_MINUTES", 180)
    )
    snapshot_min_gap_minutes: int = field(
        default_factory=lambda: env_int("SNAPSHOT_MIN_GAP_MINUTES", 30)
    )
    # API-Football pre-match oranlari yaklasik 7 gunluk pencerede sunuyor.
    snapshot_horizon_days: int = field(default_factory=lambda: env_int("SNAPSHOT_HORIZON_DAYS", 7))

    # --- Top 5 ---
    top5_size: int = field(default_factory=lambda: env_int("TOP5_SIZE", 5))
    top5_min_data_quality: float = field(
        default_factory=lambda: env_float("TOP5_MIN_DATA_QUALITY", 50.0)
    )

    # --- Alt yapilandirmalar ---
    ttl: CacheTTL = field(default_factory=CacheTTL)
    model: ModelConfig = field(default_factory=ModelConfig)
    confidence_weights: ConfidenceWeights = field(default_factory=ConfidenceWeights)
    data_quality_weights: DataQualityWeights = field(default_factory=DataQualityWeights)

    @property
    def tzinfo(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone_name)
        except Exception:  # pragma: no cover - gecersiz timezone adi
            logging.getLogger(__name__).warning(
                "Gecersiz TIMEZONE %r, Europe/Istanbul kullaniliyor", self.timezone_name
            )
            return ZoneInfo("Europe/Istanbul")

    @property
    def api_key_configured(self) -> bool:
        return bool(self.api_key)

    @property
    def league_ids(self) -> list[int]:
        return [league.id for league in self.leagues]

    def league(self, league_id: int) -> LeagueConfig | None:
        for item in self.leagues:
            if item.id == league_id:
                return item
        return None

    def uses_postgres(self) -> bool:
        return self.database_url.startswith(("postgres://", "postgresql://"))


_settings: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    """Surec boyunca tek Settings ornegi dondurur."""
    global _settings
    if _settings is None or refresh:
        _settings = Settings()
    return _settings


# --------------------------------------------------------------------------
# Logging - sir sizdirmayan yapilandirma
# --------------------------------------------------------------------------
class SecretRedactingFilter(logging.Filter):
    """API anahtarinin loga dusmesini engeller.

    Anahtar bir sekilde mesaja veya argumanlara karisirsa ***** ile degistirilir.
    """

    _GENERIC = re.compile(r"(x-apisports-key|api[_-]?key|apikey)\s*[:=]\s*\S+", re.IGNORECASE)

    def __init__(self, secret: str = "") -> None:
        super().__init__()
        self.secret = secret

    def _clean(self, text: str) -> str:
        if self.secret and self.secret in text:
            text = text.replace(self.secret, "*****")
        return self._GENERIC.sub(r"\1=*****", text)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._clean(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: self._clean(value) if isinstance(value, str) else value
                    for key, value in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self._clean(value) if isinstance(value, str) else value for value in record.args
                )
        return True


def configure_logging(settings: Settings | None = None) -> None:
    """Production logging: seviye ayari + anahtar redaksiyonu."""
    settings = settings or get_settings()
    level = getattr(logging, settings.log_level, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )
        root.addHandler(handler)

    redactor = SecretRedactingFilter(settings.api_key)
    for handler in root.handlers:
        # Ayni filtreyi iki kez eklemeyelim.
        handler.filters = [f for f in handler.filters if not isinstance(f, SecretRedactingFilter)]
        handler.addFilter(redactor)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
