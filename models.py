"""Backend -> frontend sozlesmesi (contract).

Temel kural: FRONTEND HICBIR DEGERI KENDISI URETMEZ. Her sayisal alan ya
gercek bir degerdir ya da ``None``dur, ve yaninda mutlaka nereden geldigini
soyleyen bir ``source`` alani tasir. Boylece "ekranda var ama API'de yok"
durumu yapisal olarak imkansiz hale gelir.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Bir degerin nereden geldigi. "unavailable" = veri yok, uydurulmadi.
Source = Literal["api", "snapshot", "model", "derived", "unavailable"]


class Base(BaseModel):
    model_config = ConfigDict(populate_by_name=True, ser_json_timedelta="iso8601")


# --------------------------------------------------------------------------
# Ortak parcalar
# --------------------------------------------------------------------------
class TeamRef(Base):
    id: int
    name: str
    logo: str | None = None


class LeagueRef(Base):
    id: int
    name: str
    country: str | None = None
    season: int | None = None
    logo: str | None = None
    round: str | None = None


class Kickoff(Base):
    """Mac baslama zamani. ``display`` her zaman Turkiye saatine gore uretilir."""

    iso: str
    display: str
    date: str
    time: str
    timezone: str
    minutes_until: int | None = None


# --------------------------------------------------------------------------
# Oranlar
# --------------------------------------------------------------------------
class MarketOdds(Base):
    """Tek bir market icin bookmaker oranlari."""

    available: bool = False
    source: Source = "unavailable"
    values: dict[str, float] = Field(default_factory=dict)
    note: str | None = None


class Bet365Block(Base):
    """SADECE Bet365. Baska bookmaker bu blogu asla dolduramaz."""

    available: bool = False
    reason: str | None = None
    bookmaker_id: int | None = None
    bookmaker_name: str | None = None
    updated_at: datetime | None = None
    source: Source = "unavailable"
    # Kolay erisim icin 1X2 duz alanlar (yoksa None kalir).
    home: float | None = None
    draw: float | None = None
    away: float | None = None
    markets: dict[str, MarketOdds] = Field(default_factory=dict)


class ImpliedProbabilities(Base):
    """Orandan turetilen piyasa olasiligi.

    ``raw`` = 1/oran (marj dahil), ``normalized`` = marj arindirilmis.
    """

    available: bool = False
    source: Source = "unavailable"
    raw: dict[str, float] = Field(default_factory=dict)
    normalized: dict[str, float] = Field(default_factory=dict)
    overround: float | None = None
    margin_pct: float | None = None
    method: str | None = None


# --------------------------------------------------------------------------
# Model ciktilari
# --------------------------------------------------------------------------
class MarketProbabilities(Base):
    """Modelin bir market icin urettigi olasiliklar."""

    available: bool = False
    source: Source = "unavailable"
    probabilities: dict[str, float] = Field(default_factory=dict)
    note: str | None = None


class ModelBlock(Base):
    """Dixon-Coles gol modelinin ciktisi. Piyasa orani girdi olarak KULLANILMAZ."""

    available: bool = False
    source: Source = "unavailable"
    note: str | None = None
    expected_goals_home: float | None = None
    expected_goals_away: float | None = None
    match_result: MarketProbabilities = Field(default_factory=MarketProbabilities)
    first_half: MarketProbabilities = Field(default_factory=MarketProbabilities)
    both_teams_to_score: MarketProbabilities = Field(default_factory=MarketProbabilities)
    over_under_25: MarketProbabilities = Field(default_factory=MarketProbabilities)
    double_chance: MarketProbabilities = Field(default_factory=MarketProbabilities)
    top_scoreline: dict[str, Any] | None = None
    matches_used_home: int = 0
    matches_used_away: int = 0


class ValueEdge(Base):
    """Model olasiligi ile marj arindirilmis piyasa olasiligi arasindaki fark."""

    available: bool = False
    source: Source = "unavailable"
    # Yuzde PUANI cinsinden fark (model - piyasa).
    edges: dict[str, float] = Field(default_factory=dict)
    best_outcome: str | None = None
    best_edge: float | None = None
    note: str | None = None


# --------------------------------------------------------------------------
# Skorlar
# --------------------------------------------------------------------------
class ScoreComponent(Base):
    key: str
    label: str
    points: float
    max_points: float
    available: bool
    source: Source
    note: str | None = None


class ConfidenceScore(Base):
    """Guven skoru. Veri kalitesinden AYRI bir olcudur."""

    score: float = 0.0
    max_score: float = 100.0
    components: list[ScoreComponent] = Field(default_factory=list)


class DataQuality(Base):
    """Hangi verinin gercekten elimizde oldugunu olcer."""

    score: float = 0.0
    max_score: float = 100.0
    components: list[ScoreComponent] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Oran hareketi (kendi snapshot sistemimizden)
# --------------------------------------------------------------------------
class SnapshotPoint(Base):
    label: str
    captured_at: datetime
    values: dict[str, float]
    minutes_to_kickoff: int | None = None


class OddsMovement(Base):
    """Oran hareketi.

    DIKKAT: "acilis orani" ve "T-15" ETIKETLERI KULLANILMAZ. API 7 gunden
    geriye gitmiyor ve pre-match oranlari ~3 saatte bir guncelliyor; bizim
    gecmisimiz de ilk kaydettigimiz anda basliyor. Bu yuzden yalnizca
    gercekten kaydedilmis snapshot'lar, gercek zaman damgalariyla gosterilir.
    """

    available: bool = False
    source: Source = "unavailable"
    note: str | None = None
    snapshot_count: int = 0
    first_recorded: SnapshotPoint | None = None
    latest: SnapshotPoint | None = None
    closest_to_kickoff: SnapshotPoint | None = None
    change_pct: dict[str, float] = Field(default_factory=dict)
    direction: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Form / H2H / sakatlik
# --------------------------------------------------------------------------
class TeamFormBlock(Base):
    available: bool = False
    source: Source = "unavailable"
    matches_used: int = 0
    wins: int = 0
    draws: int = 0
    losses: int = 0
    goals_for: int = 0
    goals_against: int = 0
    points_per_game: float | None = None
    results: list[str] = Field(default_factory=list)
    note: str | None = None


class HomeAwayBlock(Base):
    available: bool = False
    source: Source = "unavailable"
    matches: int = 0
    goals_for_avg: float | None = None
    goals_against_avg: float | None = None
    note: str | None = None


class H2HBlock(Base):
    available: bool = False
    source: Source = "unavailable"
    matches_used: int = 0
    home_wins: int = 0
    draws: int = 0
    away_wins: int = 0
    goals_home: int = 0
    goals_away: int = 0
    recent: list[dict[str, Any]] = Field(default_factory=list)
    note: str | None = None


class InjuriesBlock(Base):
    available: bool = False
    source: Source = "unavailable"
    home_count: int = 0
    away_count: int = 0
    home_players: list[dict[str, Any]] = Field(default_factory=list)
    away_players: list[dict[str, Any]] = Field(default_factory=list)
    note: str | None = None


class ApiPredictionBlock(Base):
    """API-Football'in kendi tahmini. Bizim modelimiz DEGILDIR, ayri gosterilir."""

    available: bool = False
    source: Source = "unavailable"
    winner_name: str | None = None
    advice: str | None = None
    percent: dict[str, float] = Field(default_factory=dict)
    note: str | None = None


# --------------------------------------------------------------------------
# Mac ozetleri
# --------------------------------------------------------------------------
class MatchSummary(Base):
    """Liste ekraninda gosterilen hafif kayit."""

    fixture_id: int
    league: LeagueRef
    home: TeamRef
    away: TeamRef
    kickoff: Kickoff
    status: str
    status_short: str
    bet365: Bet365Block = Field(default_factory=Bet365Block)
    implied: ImpliedProbabilities = Field(default_factory=ImpliedProbabilities)
    model: ModelBlock = Field(default_factory=ModelBlock)
    value: ValueEdge = Field(default_factory=ValueEdge)
    confidence: ConfidenceScore = Field(default_factory=ConfidenceScore)
    data_quality: DataQuality = Field(default_factory=DataQuality)
    # Kendi snapshot gecmisimizden gelir; ek API cagrisi gerektirmez.
    odds_movement: OddsMovement = Field(default_factory=OddsMovement)
    analyzed: bool = False
    analysis_note: str | None = None


class MatchDetail(MatchSummary):
    """Mac detay ekrani. Liste alanlarinin uzerine derin veriyi ekler."""

    home_form: TeamFormBlock = Field(default_factory=TeamFormBlock)
    away_form: TeamFormBlock = Field(default_factory=TeamFormBlock)
    home_venue: HomeAwayBlock = Field(default_factory=HomeAwayBlock)
    away_venue: HomeAwayBlock = Field(default_factory=HomeAwayBlock)
    h2h: H2HBlock = Field(default_factory=H2HBlock)
    injuries: InjuriesBlock = Field(default_factory=InjuriesBlock)
    api_prediction: ApiPredictionBlock = Field(default_factory=ApiPredictionBlock)
    explanation: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Toplu cevaplar
# --------------------------------------------------------------------------
class QuotaInfo(Base):
    known: bool = False
    daily_limit: int | None = None
    daily_remaining: int | None = None
    per_minute_remaining: int | None = None
    calls_this_request: int = 0
    note: str | None = None


class MatchesResponse(Base):
    generated_at: datetime
    timezone: str
    filters: dict[str, Any]
    counts: dict[str, int]
    matches: list[MatchSummary]
    quota: QuotaInfo = Field(default_factory=QuotaInfo)
    warnings: list[str] = Field(default_factory=list)


class Top5Response(Base):
    generated_at: datetime
    criteria: str
    matches: list[MatchSummary]
    excluded_counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class HighlightCard(Base):
    key: str
    label: str
    available: bool
    fixture_id: int | None = None
    headline: str | None = None
    detail: str | None = None
    value: float | None = None
    note: str | None = None


class PlanAccess(Base):
    """API-Football abonelik planinin SEZON erisim durumu.

    Ucretsiz planlar guncel sezonun verisini vermiyor. Bu blok durumu
    ACIKCA tasir; uygulama eski sezonu "bugunun maclari" gibi GOSTERMEZ.
    """

    checked: bool = False
    season_access_ok: bool | None = None
    requested_season: int | None = None
    league_id: int | None = None
    #: API-Football'in kendi cumlesi - kullaniciya aynen gosterilir.
    provider_message: str | None = None
    newest_accessible_season: int | None = None
    tried: list[dict[str, Any]] = Field(default_factory=list)
    note: str | None = None


class StatusResponse(Base):
    backend: str
    version: str
    api_key_configured: bool
    api_connected: bool
    bet365_available: bool
    bookmaker_id: int | None = None
    database: str
    database_persistent: bool
    snapshot_count: int | None = None
    last_snapshot_at: datetime | None = None
    scheduler_running: bool = False
    quota: QuotaInfo = Field(default_factory=QuotaInfo)
    timezone: str
    plan: PlanAccess = Field(default_factory=PlanAccess)
    leagues: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    checks: list[dict[str, Any]] = Field(default_factory=list)
