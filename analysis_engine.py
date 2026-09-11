"""Analiz motoru - Dixon-Coles gol modeli ve skorlama.

Tasarim kararlari
-----------------
1. Model PIYASADAN BAGIMSIZDIR. Bet365 orani modele girdi olarak verilmez.
   Aksi halde "model / piyasa farki" dongusel bir sayi olur (piyasayi modele
   koyup sonra piyasayla karsilastirmis oluruz).
2. Tum marketler TEK bir gol modelinden turetilir. MS, IY, KG ve 2.5 ayni
   skor matrisinden cikar; boylece birbiriyle celismezler.
3. Veri yoksa sayi URETILMEZ. Ilgili blok ``available=False`` doner ve
   nedeni yazilir.
4. Sakatlik verisi gol beklentisini DEGISTIRMEZ. Oyuncu bazli agirlik
   verimiz olmadigi icin "3 sakat = %4 dusur" gibi bir katsayi uydurmak
   savunulamaz; sakatlik yalnizca guven ve veri kalitesi skorlarini etkiler.

Bu modul yalnizca standart kutuphaneyi kullanir; bagimsiz test edilebilir.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Sequence

from config import ConfidenceWeights, DataQualityWeights, ModelConfig

# Marj arindirma yontemi. Oransal (proportional) yontem seffaftir ve
# ek varsayim gerektirmez.
MARGIN_METHOD = "proportional"


# ==========================================================================
# Girdi yapilari
# ==========================================================================
@dataclass(frozen=True)
class MatchRecord:
    """Bir takimin oynadigi tek mac (bitmis)."""

    fixture_id: int
    is_home: bool
    goals_for: int
    goals_against: int
    kickoff: datetime | None = None
    ht_goals_for: int | None = None
    ht_goals_against: int | None = None

    @property
    def has_halftime(self) -> bool:
        return self.ht_goals_for is not None and self.ht_goals_against is not None


@dataclass(frozen=True)
class LeagueAverages:
    """Lig ortalamalari - takim guclerini normalize etmek icin."""

    home_goals: float
    away_goals: float
    matches: int

    @property
    def available(self) -> bool:
        return self.matches > 0 and self.home_goals > 0 and self.away_goals > 0

    @property
    def overall(self) -> float:
        return (self.home_goals + self.away_goals) / 2.0


@dataclass
class TeamStrength:
    """Bir takimin hucum/defans gucu (1.0 = lig ortalamasi)."""

    attack_home: float = 1.0
    defence_home: float = 1.0
    attack_away: float = 1.0
    defence_away: float = 1.0
    matches: int = 0
    home_matches: int = 0
    away_matches: int = 0
    halftime_share: float | None = None
    halftime_matches: int = 0
    available: bool = False
    note: str | None = None


# ==========================================================================
# Temel matematik
# ==========================================================================
def poisson_pmf(k: int, lam: float) -> float:
    """Poisson olasilik kutle fonksiyonu."""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    try:
        return math.exp(-lam) * (lam**k) / math.factorial(k)
    except OverflowError:  # pragma: no cover
        return 0.0


def dixon_coles_tau(i: int, j: int, lam_home: float, lam_away: float, rho: float) -> float:
    """Dixon-Coles dusuk skor duzeltmesi.

    Bagimsiz Poisson modeli 0-0 ve 1-1 gibi skorlari sistematik olarak
    dusuk tahmin eder. Tau bu dort hucreyi duzeltir.
    """
    if i == 0 and j == 0:
        return 1.0 - lam_home * lam_away * rho
    if i == 0 and j == 1:
        return 1.0 + lam_home * rho
    if i == 1 and j == 0:
        return 1.0 + lam_away * rho
    if i == 1 and j == 1:
        return 1.0 - rho
    return 1.0


def score_matrix(
    lam_home: float,
    lam_away: float,
    rho: float = -0.05,
    max_goals: int = 10,
) -> list[list[float]]:
    """Normalize edilmis skor olasilik matrisi dondurur.

    ``matrix[i][j]`` = ev sahibinin i, deplasmanin j gol atma olasiligi.
    """
    matrix: list[list[float]] = []
    total = 0.0
    for i in range(max_goals + 1):
        row: list[float] = []
        p_home = poisson_pmf(i, lam_home)
        for j in range(max_goals + 1):
            value = p_home * poisson_pmf(j, lam_away) * dixon_coles_tau(i, j, lam_home, lam_away, rho)
            value = max(value, 0.0)  # tau asiri rho'da negatife dusebilir
            row.append(value)
            total += value
        matrix.append(row)

    if total <= 0:  # pragma: no cover - matematiksel olarak beklenmiyor
        return [[0.0] * (max_goals + 1) for _ in range(max_goals + 1)]

    return [[value / total for value in row] for row in matrix]


def markets_from_matrix(matrix: Sequence[Sequence[float]]) -> dict[str, dict[str, float]]:
    """Skor matrisinden tum marketleri turetir."""
    home_win = draw = away_win = 0.0
    btts_yes = 0.0
    over_15 = over_25 = over_35 = 0.0

    for i, row in enumerate(matrix):
        for j, probability in enumerate(row):
            if probability <= 0:
                continue
            if i > j:
                home_win += probability
            elif i == j:
                draw += probability
            else:
                away_win += probability
            if i >= 1 and j >= 1:
                btts_yes += probability
            total_goals = i + j
            if total_goals >= 2:
                over_15 += probability
            if total_goals >= 3:
                over_25 += probability
            if total_goals >= 4:
                over_35 += probability

    return {
        "match_result": {"1": home_win, "X": draw, "2": away_win},
        "double_chance": {
            "1X": home_win + draw,
            "12": home_win + away_win,
            "X2": draw + away_win,
        },
        "both_teams_to_score": {"var": btts_yes, "yok": 1.0 - btts_yes},
        "over_under_15": {"ust": over_15, "alt": 1.0 - over_15},
        "over_under_25": {"ust": over_25, "alt": 1.0 - over_25},
        "over_under_35": {"ust": over_35, "alt": 1.0 - over_35},
    }


def most_likely_scoreline(matrix: Sequence[Sequence[float]]) -> dict[str, Any]:
    best_i = best_j = 0
    best_probability = -1.0
    for i, row in enumerate(matrix):
        for j, probability in enumerate(row):
            if probability > best_probability:
                best_probability = probability
                best_i, best_j = i, j
    return {"home": best_i, "away": best_j, "probability": best_probability}


# ==========================================================================
# Piyasa orani matematigi
# ==========================================================================
def calculate_implied_probability(odds: dict[str, float]) -> dict[str, float]:
    """P = 1 / oran. Marj DAHIL ham olasilik."""
    result: dict[str, float] = {}
    for key, value in odds.items():
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if numeric > 1.0:
            result[key] = 1.0 / numeric
    return result


def calculate_overround(implied: dict[str, float]) -> float:
    """Toplam ham olasilik. 1.0'in uzerindeki kisim bookmaker marjidir."""
    return sum(implied.values())


def normalize_probabilities(implied: dict[str, float]) -> dict[str, float]:
    """Marji oransal olarak dagitarak toplami 1.0 yapar."""
    total = calculate_overround(implied)
    if total <= 0:
        return {}
    return {key: value / total for key, value in implied.items()}


# ==========================================================================
# Takim gucu
# ==========================================================================
def _shrink(ratio: float, sample: int, k: float) -> float:
    """Kucuk orneklemi lig ortalamasina (1.0) dogru ceker."""
    if sample <= 0:
        return 1.0
    return (sample * ratio + k * 1.0) / (sample + k)


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 1.0
    return numerator / denominator


def halftime_share(records: Iterable[MatchRecord]) -> tuple[float | None, int]:
    """Takimin gollerinin ne kadari ilk yaride atilmis?

    Gercek ``score.halftime`` verisinden hesaplanir. Yeterli veri yoksa
    ``None`` doner - sabit bir katsayi UYDURULMAZ.
    """
    ht_goals = 0
    ft_goals = 0
    used = 0
    for record in records:
        if not record.has_halftime:
            continue
        used += 1
        ht_goals += int(record.ht_goals_for or 0)
        ft_goals += int(record.goals_for)
    if used == 0 or ft_goals <= 0:
        return None, used
    share = ht_goals / ft_goals
    # Mantikli sinirlar icinde tut.
    return min(max(share, 0.15), 0.75), used


def calculate_team_strength(
    records: Sequence[MatchRecord],
    league: LeagueAverages,
    config: ModelConfig,
) -> TeamStrength:
    """Mac kayitlarindan hucum/defans gucu uretir."""
    strength = TeamStrength()
    if not league.available:
        strength.note = "Lig ortalamalari hesaplanamadi"
        return strength
    if len(records) < config.min_matches:
        strength.matches = len(records)
        strength.note = f"Yeterli mac verisi yok ({len(records)}/{config.min_matches})"
        return strength

    home_records = [record for record in records if record.is_home]
    away_records = [record for record in records if not record.is_home]

    strength.matches = len(records)
    strength.home_matches = len(home_records)
    strength.away_matches = len(away_records)

    # --- Sezonluk (venue bazli) guc ---
    if home_records:
        gf = sum(record.goals_for for record in home_records) / len(home_records)
        ga = sum(record.goals_against for record in home_records) / len(home_records)
        attack_home = _shrink(_safe_ratio(gf, league.home_goals), len(home_records), config.shrinkage_k)
        defence_home = _shrink(_safe_ratio(ga, league.away_goals), len(home_records), config.shrinkage_k)
    else:
        attack_home = defence_home = 1.0

    if away_records:
        gf = sum(record.goals_for for record in away_records) / len(away_records)
        ga = sum(record.goals_against for record in away_records) / len(away_records)
        attack_away = _shrink(_safe_ratio(gf, league.away_goals), len(away_records), config.shrinkage_k)
        defence_away = _shrink(_safe_ratio(ga, league.home_goals), len(away_records), config.shrinkage_k)
    else:
        attack_away = defence_away = 1.0

    # --- Son form (ussel sonumleme, venue farki gozetmeden) ---
    recent = sorted(
        records,
        key=lambda record: record.kickoff or datetime.min,
        reverse=True,
    )[: config.form_matches]

    if recent:
        weights = [config.form_decay**index for index in range(len(recent))]
        weight_sum = sum(weights)
        weighted_gf = sum(w * r.goals_for for w, r in zip(weights, recent)) / weight_sum
        weighted_ga = sum(w * r.goals_against for w, r in zip(weights, recent)) / weight_sum
        form_attack = _shrink(
            _safe_ratio(weighted_gf, league.overall), len(recent), config.shrinkage_k
        )
        form_defence = _shrink(
            _safe_ratio(weighted_ga, league.overall), len(recent), config.shrinkage_k
        )
        weight = max(0.0, min(1.0, config.form_weight))
        attack_home = (1 - weight) * attack_home + weight * form_attack
        attack_away = (1 - weight) * attack_away + weight * form_attack
        defence_home = (1 - weight) * defence_home + weight * form_defence
        defence_away = (1 - weight) * defence_away + weight * form_defence

    strength.attack_home = attack_home
    strength.defence_home = defence_home
    strength.attack_away = attack_away
    strength.defence_away = defence_away

    share, used = halftime_share(records)
    strength.halftime_share = share
    strength.halftime_matches = used
    strength.available = True
    return strength


def calculate_league_averages(all_records: Sequence[MatchRecord]) -> LeagueAverages:
    """Elimizdeki mac kayitlarindan lig ortalamasi cikarir.

    Her mac iki kez gorulur (her takimin kaydinda), bu yuzden yalnizca ev
    sahibi perspektifindeki kayitlar sayilir.
    """
    home_view = [record for record in all_records if record.is_home]
    if not home_view:
        return LeagueAverages(0.0, 0.0, 0)
    home_goals = sum(record.goals_for for record in home_view) / len(home_view)
    away_goals = sum(record.goals_against for record in home_view) / len(home_view)
    return LeagueAverages(home_goals, away_goals, len(home_view))


def calculate_expected_goals(
    home: TeamStrength,
    away: TeamStrength,
    league: LeagueAverages,
    config: ModelConfig,
) -> tuple[float, float]:
    """Beklenen gol sayilari.

    Ev avantaji ayri bir katsayi olarak EKLENMEZ; zaten ev/deplasman
    ayrimli guclerin icinde. Iki kez saymamak icin.
    """
    lam_home = league.home_goals * home.attack_home * away.defence_away
    lam_away = league.away_goals * away.attack_away * home.defence_home
    lam_home = min(max(lam_home, config.min_lambda), config.max_lambda)
    lam_away = min(max(lam_away, config.min_lambda), config.max_lambda)
    return lam_home, lam_away


# ==========================================================================
# Model ciktisi
# ==========================================================================
def calculate_model_probability(
    home_records: Sequence[MatchRecord],
    away_records: Sequence[MatchRecord],
    config: ModelConfig,
    league: LeagueAverages | None = None,
) -> dict[str, Any]:
    """Piyasadan bagimsiz gol modelini calistirir."""
    result: dict[str, Any] = {
        "available": False,
        "source": "unavailable",
        "note": None,
        "expected_goals_home": None,
        "expected_goals_away": None,
        "matches_used_home": len(home_records),
        "matches_used_away": len(away_records),
        "markets": {},
        "first_half": {"available": False, "note": None, "probabilities": {}},
        "top_scoreline": None,
    }

    if league is None:
        league = calculate_league_averages(list(home_records) + list(away_records))

    if not league.available:
        result["note"] = "Lig gol ortalamalari hesaplanamadi (yetersiz mac verisi)"
        return result

    home_strength = calculate_team_strength(home_records, league, config)
    away_strength = calculate_team_strength(away_records, league, config)

    if not home_strength.available or not away_strength.available:
        notes = [note for note in (home_strength.note, away_strength.note) if note]
        result["note"] = "; ".join(notes) or "Yeterli veri yok"
        return result

    lam_home, lam_away = calculate_expected_goals(home_strength, away_strength, league, config)
    matrix = score_matrix(lam_home, lam_away, config.rho, config.max_goals)

    result["available"] = True
    result["source"] = "model"
    result["expected_goals_home"] = round(lam_home, 3)
    result["expected_goals_away"] = round(lam_away, 3)
    result["markets"] = markets_from_matrix(matrix)
    result["top_scoreline"] = most_likely_scoreline(matrix)

    # --- Ilk yari ---
    share_home = home_strength.halftime_share
    share_away = away_strength.halftime_share
    ht_matches = min(home_strength.halftime_matches, away_strength.halftime_matches)

    if share_home is None or share_away is None or ht_matches < config.min_halftime_matches:
        result["first_half"] = {
            "available": False,
            "probabilities": {},
            "note": (
                "Ilk yari skoru bilinen mac sayisi yetersiz "
                f"({ht_matches}/{config.min_halftime_matches}) - yeterli veri yok"
            ),
        }
    else:
        ht_matrix = score_matrix(
            max(lam_home * share_home, config.min_lambda),
            max(lam_away * share_away, config.min_lambda),
            config.rho,
            config.max_goals,
        )
        ht_markets = markets_from_matrix(ht_matrix)
        result["first_half"] = {
            "available": True,
            "probabilities": ht_markets["match_result"],
            "note": None,
            "expected_goals_home": round(lam_home * share_home, 3),
            "expected_goals_away": round(lam_away * share_away, 3),
            "matches_used": ht_matches,
        }

    result["strength_home"] = home_strength
    result["strength_away"] = away_strength
    result["league"] = league
    return result


# ==========================================================================
# Deger (value) analizi
# ==========================================================================
def calculate_value_score(
    model_probabilities: dict[str, float],
    market_normalized: dict[str, float],
) -> dict[str, Any]:
    """Model olasiligi ile marj arindirilmis piyasa olasiligi farki.

    Fark YUZDE PUANI cinsindendir: 0.61 - 0.53 = +8 puan.
    """
    result: dict[str, Any] = {
        "available": False,
        "source": "unavailable",
        "edges": {},
        "best_outcome": None,
        "best_edge": None,
        "note": None,
    }
    if not model_probabilities or not market_normalized:
        result["note"] = "Model veya Bet365 orani olmadan fark hesaplanamaz"
        return result

    edges: dict[str, float] = {}
    for key, model_value in model_probabilities.items():
        market_value = market_normalized.get(key)
        if market_value is None:
            continue
        edges[key] = round((model_value - market_value) * 100, 2)

    if not edges:
        result["note"] = "Model ve piyasa ayni marketi kapsamiyor"
        return result

    best = max(edges.items(), key=lambda item: item[1])
    result.update(
        {
            "available": True,
            "source": "derived",
            "edges": edges,
            "best_outcome": best[0],
            "best_edge": best[1],
        }
    )
    return result


# ==========================================================================
# Oran hareketi (kendi snapshot'larimizdan)
# ==========================================================================
def calculate_odds_movement(
    snapshots: Sequence[dict[str, Any]],
    kickoff: datetime | None = None,
    threshold_pct: float = 0.5,
) -> dict[str, Any]:
    """Kaydedilmis snapshot'lardan oran hareketi.

    "Acilis orani" veya "T-15" ETIKETI KULLANILMAZ. Yalnizca gercekten
    kaydedilmis anlar, gercek zaman damgalariyla raporlanir.
    """
    result: dict[str, Any] = {
        "available": False,
        "source": "unavailable",
        "note": None,
        "snapshot_count": len(snapshots),
        "first_recorded": None,
        "latest": None,
        "closest_to_kickoff": None,
        "change_pct": {},
        "direction": {},
    }

    usable = [
        snapshot
        for snapshot in snapshots
        if snapshot.get("home_odds") and snapshot.get("away_odds")
    ]
    if not usable:
        result["note"] = "Bu mac icin kaydedilmis Bet365 oran snapshot'i bulunmuyor"
        return result
    if len(usable) < 2:
        result["note"] = (
            "Yalnizca tek snapshot var - hareket hesaplanamaz "
            "(gecmis oran uydurulmaz)"
        )
        result["snapshot_count"] = len(usable)
        result["first_recorded"] = _snapshot_point(usable[0], "Ilk kaydedilen", kickoff)
        return result

    ordered = sorted(usable, key=lambda item: str(item.get("captured_at") or ""))
    first, latest = ordered[0], ordered[-1]

    before_kickoff = ordered
    if kickoff is not None:
        filtered = [
            snapshot
            for snapshot in ordered
            if _parse_dt(snapshot.get("captured_at")) is not None
            and _parse_dt(snapshot.get("captured_at")) <= kickoff  # type: ignore[operator]
        ]
        if filtered:
            before_kickoff = filtered

    result["available"] = True
    result["source"] = "snapshot"
    result["snapshot_count"] = len(ordered)
    result["first_recorded"] = _snapshot_point(first, "Ilk kaydedilen", kickoff)
    result["latest"] = _snapshot_point(latest, "Guncel", kickoff)
    result["closest_to_kickoff"] = _snapshot_point(
        before_kickoff[-1], "Baslama saatine en yakin kayit", kickoff
    )

    for key, column in (("1", "home_odds"), ("X", "draw_odds"), ("2", "away_odds")):
        start = first.get(column)
        end = latest.get(column)
        if not start or not end:
            continue
        change = (float(end) - float(start)) / float(start) * 100.0
        result["change_pct"][key] = round(change, 2)
        if change <= -threshold_pct:
            result["direction"][key] = "dustu"
        elif change >= threshold_pct:
            result["direction"][key] = "yukseldi"
        else:
            result["direction"][key] = "sabit"

    return result


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _snapshot_point(
    snapshot: dict[str, Any], label: str, kickoff: datetime | None
) -> dict[str, Any]:
    captured = _parse_dt(snapshot.get("captured_at"))
    minutes: int | None = None
    if captured is not None and kickoff is not None:
        try:
            minutes = int((kickoff - captured).total_seconds() // 60)
        except (TypeError, ValueError):  # pragma: no cover
            minutes = None
    values = {}
    for key, column in (("1", "home_odds"), ("X", "draw_odds"), ("2", "away_odds")):
        value = snapshot.get(column)
        if value:
            values[key] = float(value)
    return {
        "label": label,
        "captured_at": captured,
        "values": values,
        "minutes_to_kickoff": minutes,
    }


# ==========================================================================
# Skor bilesenleri
# ==========================================================================
def _component(
    key: str,
    label: str,
    points: float,
    max_points: float,
    available: bool,
    source: str,
    note: str | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "points": round(max(0.0, min(points, max_points)), 2),
        "max_points": round(max_points, 2),
        "available": available,
        "source": source,
        "note": note,
    }


def calculate_form_score(
    home_records: Sequence[MatchRecord],
    away_records: Sequence[MatchRecord],
    config: ModelConfig,
    max_points: float,
) -> dict[str, Any]:
    """Form bileseninin GUVENILIRLIGI: ornek buyuklugune dayanir."""
    sample = min(len(home_records), len(away_records))
    if sample < config.min_matches:
        return _component(
            "form",
            "Takim formu",
            0.0,
            max_points,
            False,
            "unavailable",
            f"Yeterli mac verisi yok ({sample}/{config.min_matches})",
        )
    ratio = min(1.0, sample / max(1, config.form_matches))
    return _component(
        "form",
        "Takim formu",
        max_points * ratio,
        max_points,
        True,
        "api",
        f"{sample} maclik veri kullanildi",
    )


def calculate_home_away_score(
    home_records: Sequence[MatchRecord],
    away_records: Sequence[MatchRecord],
    max_points: float,
    target: int = 3,
) -> dict[str, Any]:
    """Ev sahibinin evindeki, deplasmanin deplasmandaki mac sayisi."""
    home_at_home = sum(1 for record in home_records if record.is_home)
    away_at_away = sum(1 for record in away_records if not record.is_home)
    sample = min(home_at_home, away_at_away)
    if sample < 2:
        return _component(
            "home_away",
            "Ev / deplasman performansi",
            0.0,
            max_points,
            False,
            "unavailable",
            f"Yeterli ev/deplasman verisi yok (ev {home_at_home}, deplasman {away_at_away})",
        )
    ratio = min(1.0, sample / target)
    return _component(
        "home_away",
        "Ev / deplasman performansi",
        max_points * ratio,
        max_points,
        True,
        "api",
        f"Ev {home_at_home} mac, deplasman {away_at_away} mac",
    )


def calculate_h2h_score(h2h_matches: int, max_points: float, target: int = 5) -> dict[str, Any]:
    if h2h_matches <= 0:
        return _component(
            "h2h", "Karsilikli maclar", 0.0, max_points, False, "unavailable", "H2H verisi yok"
        )
    ratio = min(1.0, h2h_matches / target)
    return _component(
        "h2h",
        "Karsilikli maclar",
        max_points * ratio,
        max_points,
        True,
        "api",
        f"{h2h_matches} karsilikli mac",
    )


def calculate_injury_score(
    injuries_available: bool,
    home_count: int,
    away_count: int,
    max_points: float,
    saturation: int = 10,
) -> dict[str, Any]:
    """Sakatlik GOL BEKLENTISINI DEGISTIRMEZ, yalnizca guveni etkiler.

    Oyuncu bazli agirlik verimiz olmadigi icin eksik oyuncu sayisi ne kadar
    fazlaysa tahmine o kadar az guveniriz - fakat olasilik kaydirilmaz.
    """
    if not injuries_available:
        return _component(
            "injuries",
            "Kadro / sakatlik durumu",
            0.0,
            max_points,
            False,
            "unavailable",
            "Sakatlik verisi bulunamadi",
        )
    total = max(0, home_count) + max(0, away_count)
    ratio = 1.0 - min(1.0, total / saturation)
    return _component(
        "injuries",
        "Kadro / sakatlik durumu",
        max_points * ratio,
        max_points,
        True,
        "api",
        f"Ev {home_count}, deplasman {away_count} eksik oyuncu",
    )


def _model_probability_component(
    model_result: dict[str, Any], max_points: float
) -> dict[str, Any]:
    if not model_result.get("available"):
        return _component(
            "model_probability",
            "Model olasiligi",
            0.0,
            max_points,
            False,
            "unavailable",
            model_result.get("note") or "Model calistirilamadi",
        )
    probabilities = model_result["markets"]["match_result"]
    top = max(probabilities.values())
    # 0.34 (tam belirsiz) -> 0 puan, 0.75 ve uzeri -> tam puan.
    ratio = max(0.0, min(1.0, (top - 0.34) / (0.75 - 0.34)))
    return _component(
        "model_probability",
        "Model olasiligi",
        max_points * ratio,
        max_points,
        True,
        "model",
        f"En yuksek MS olasiligi %{top * 100:.1f}",
    )


def _market_agreement_component(
    value_result: dict[str, Any], max_points: float
) -> dict[str, Any]:
    """Model piyasadan cok uzaklastikca guven DUSER.

    Buyuk sapma genelde gercek bir avantaj degil, eksik bilgi (kadro haberi,
    motivasyon, transfer) isaretidir. Bu yuzden fark bir firsat olarak ayrica
    gosterilir ama guveni yukseltmez.
    """
    if not value_result.get("available"):
        return _component(
            "market_agreement",
            "Piyasa uyumu",
            0.0,
            max_points,
            False,
            "unavailable",
            value_result.get("note") or "Bet365 orani yok",
        )
    best_edge = abs(float(value_result.get("best_edge") or 0.0))
    if best_edge <= 5.0:
        ratio = 1.0
    elif best_edge >= 25.0:
        ratio = 0.0
    else:
        ratio = 1.0 - (best_edge - 5.0) / 20.0
    return _component(
        "market_agreement",
        "Piyasa uyumu",
        max_points * ratio,
        max_points,
        True,
        "derived",
        f"Model / piyasa farki {best_edge:.1f} puan",
    )


def _odds_movement_component(movement: dict[str, Any], max_points: float) -> dict[str, Any]:
    if not movement.get("available"):
        return _component(
            "odds_movement",
            "Oran hareketi",
            0.0,
            max_points,
            False,
            "unavailable",
            movement.get("note") or "Snapshot gecmisi yok",
        )
    changes = movement.get("change_pct") or {}
    if not changes:
        return _component(
            "odds_movement",
            "Oran hareketi",
            0.0,
            max_points,
            False,
            "unavailable",
            "Karsilastirilabilir snapshot degeri yok",
        )
    biggest = max(abs(value) for value in changes.values())
    ratio = 1.0 - min(1.0, biggest / 10.0)
    return _component(
        "odds_movement",
        "Oran hareketi",
        max_points * ratio,
        max_points,
        True,
        "snapshot",
        f"En buyuk degisim %{biggest:.2f}",
    )


def calculate_data_quality(
    *,
    bet365_available: bool,
    form_matches: int,
    h2h_matches: int,
    home_away_available: bool,
    predictions_available: bool,
    injuries_available: bool,
    snapshot_count: int,
    weights: DataQualityWeights,
    min_form_matches: int = 5,
) -> dict[str, Any]:
    """Elimizdeki verinin ne kadar tam oldugunu olcer (guvenden AYRI)."""
    components = [
        _component(
            "bet365_odds",
            "Bet365 orani",
            weights.bet365_odds if bet365_available else 0.0,
            weights.bet365_odds,
            bet365_available,
            "api" if bet365_available else "unavailable",
            None if bet365_available else "Bet365 orani yok",
        ),
        _component(
            "recent_form",
            "Son maclar",
            weights.recent_form * min(1.0, form_matches / max(1, min_form_matches)),
            weights.recent_form,
            form_matches > 0,
            "api" if form_matches else "unavailable",
            f"{form_matches} mac" if form_matches else "Mac gecmisi yok",
        ),
        _component(
            "h2h",
            "Karsilikli maclar",
            weights.h2h * min(1.0, h2h_matches / 5.0),
            weights.h2h,
            h2h_matches > 0,
            "api" if h2h_matches else "unavailable",
            f"{h2h_matches} mac" if h2h_matches else "H2H verisi yok",
        ),
        _component(
            "home_away_split",
            "Ev / deplasman ayrimi",
            weights.home_away_split if home_away_available else 0.0,
            weights.home_away_split,
            home_away_available,
            "api" if home_away_available else "unavailable",
            None if home_away_available else "Ev/deplasman verisi yetersiz",
        ),
        _component(
            "predictions",
            "API predictions",
            weights.predictions if predictions_available else 0.0,
            weights.predictions,
            predictions_available,
            "api" if predictions_available else "unavailable",
            None if predictions_available else "Predictions verisi cekilmedi",
        ),
        _component(
            "injuries",
            "Sakatlik verisi",
            weights.injuries if injuries_available else 0.0,
            weights.injuries,
            injuries_available,
            "api" if injuries_available else "unavailable",
            None if injuries_available else "Sakatlik verisi yok",
        ),
        _component(
            "odds_snapshot",
            "Oran snapshot gecmisi",
            weights.odds_snapshot * min(1.0, snapshot_count / 2.0),
            weights.odds_snapshot,
            snapshot_count > 0,
            "snapshot" if snapshot_count else "unavailable",
            f"{snapshot_count} snapshot" if snapshot_count else "Snapshot yok",
        ),
    ]
    score = sum(component["points"] for component in components)
    missing = [component["label"] for component in components if not component["available"]]
    return {
        "score": round(score, 1),
        "max_score": round(weights.total(), 1),
        "components": components,
        "missing": missing,
    }


def calculate_confidence(
    *,
    model_result: dict[str, Any],
    value_result: dict[str, Any],
    movement: dict[str, Any],
    home_records: Sequence[MatchRecord],
    away_records: Sequence[MatchRecord],
    h2h_matches: int,
    injuries_available: bool,
    home_injuries: int,
    away_injuries: int,
    data_quality_score: float,
    weights: ConfidenceWeights,
    model_config: ModelConfig,
) -> dict[str, Any]:
    """Seffaf guven skoru: her bilesen kaynagiyla birlikte raporlanir."""
    components = [
        _model_probability_component(model_result, weights.model_probability),
        _market_agreement_component(value_result, weights.market_agreement),
        calculate_form_score(home_records, away_records, model_config, weights.form),
        calculate_home_away_score(home_records, away_records, weights.home_away),
        calculate_h2h_score(h2h_matches, weights.h2h),
        calculate_injury_score(
            injuries_available, home_injuries, away_injuries, weights.injuries
        ),
        _odds_movement_component(movement, weights.odds_movement),
        _component(
            "data_quality",
            "Veri kalitesi",
            weights.data_quality * max(0.0, min(1.0, data_quality_score / 100.0)),
            weights.data_quality,
            data_quality_score > 0,
            "derived",
            f"Veri kalitesi {data_quality_score:.0f}/100",
        ),
    ]
    score = sum(component["points"] for component in components)
    return {
        "score": round(score, 1),
        "max_score": round(weights.total(), 1),
        "components": components,
    }


# ==========================================================================
# Aciklama uretimi
# ==========================================================================
def build_explanation(
    *,
    home_name: str,
    away_name: str,
    model_result: dict[str, Any],
    value_result: dict[str, Any],
    movement: dict[str, Any],
    odds: dict[str, float] | None,
    data_quality: dict[str, Any],
    home_records: Sequence[MatchRecord],
    away_records: Sequence[MatchRecord],
) -> list[str]:
    """Yalnizca GERCEKTEN bulunan verilere dayanan aciklama satirlari.

    "kesin", "garanti", "banko" gibi ifadeler kullanilmaz.
    """
    lines: list[str] = []

    if model_result.get("available"):
        probabilities = model_result["markets"]["match_result"]
        pick = max(probabilities.items(), key=lambda item: item[1])
        label = {"1": home_name, "X": "Beraberlik", "2": away_name}[pick[0]]
        lines.append(
            f"Model en yuksek olasiligi {label} icin veriyor (%{pick[1] * 100:.1f})."
        )
        lines.append(
            "Beklenen gol: "
            f"{home_name} {model_result['expected_goals_home']:.2f} - "
            f"{model_result['expected_goals_away']:.2f} {away_name}."
        )

    if home_records:
        wins = sum(1 for record in home_records if record.goals_for > record.goals_against)
        lines.append(
            f"{home_name} son {len(home_records)} macta {wins} galibiyet aldi."
        )
    if away_records:
        wins = sum(1 for record in away_records if record.goals_for > record.goals_against)
        lines.append(
            f"{away_name} son {len(away_records)} macta {wins} galibiyet aldi."
        )

    if odds:
        parts = [f"{key} {value:.2f}" for key, value in odds.items()]
        lines.append("Bet365 orani: " + ", ".join(parts) + ".")

    if value_result.get("available") and value_result.get("best_edge") is not None:
        edge = value_result["best_edge"]
        outcome = value_result["best_outcome"]
        label = {"1": home_name, "X": "Beraberlik", "2": away_name}.get(outcome, outcome)
        if edge > 0:
            lines.append(
                f"Model, {label} icin piyasadan {edge:.1f} puan daha yuksek olasilik veriyor."
            )
        else:
            lines.append(
                f"Model hicbir sonucta piyasanin uzerine cikmiyor (en iyi fark {edge:.1f} puan)."
            )

    if movement.get("available"):
        for key, direction in (movement.get("direction") or {}).items():
            change = movement["change_pct"].get(key)
            if direction != "sabit" and change is not None:
                lines.append(
                    f"{key} orani ilk kaydimizdan bu yana %{abs(change):.2f} {direction}."
                )
                break
    elif movement.get("note"):
        lines.append(movement["note"] + ".")

    lines.append(f"Veri kalitesi {data_quality['score']:.0f}/{data_quality['max_score']:.0f}.")
    return lines


# ==========================================================================
# Ana orkestrasyon
# ==========================================================================
def generate_match_analysis(
    *,
    home_name: str,
    away_name: str,
    home_records: Sequence[MatchRecord],
    away_records: Sequence[MatchRecord],
    odds_1x2: dict[str, float] | None,
    snapshots: Sequence[dict[str, Any]] = (),
    kickoff: datetime | None = None,
    h2h_matches: int = 0,
    injuries_available: bool = False,
    home_injuries: int = 0,
    away_injuries: int = 0,
    predictions_available: bool = False,
    model_config: ModelConfig | None = None,
    confidence_weights: ConfidenceWeights | None = None,
    data_quality_weights: DataQualityWeights | None = None,
    league_averages: LeagueAverages | None = None,
) -> dict[str, Any]:
    """Bir mac icin tam analiz uretir.

    Girdi olarak yalnizca GERCEK veriler beklenir. Eksik girdi, eksik cikti
    demektir - doldurma yapilmaz.
    """
    model_config = model_config or ModelConfig()
    confidence_weights = confidence_weights or ConfidenceWeights()
    data_quality_weights = data_quality_weights or DataQualityWeights()

    model_result = calculate_model_probability(
        home_records, away_records, model_config, league_averages
    )

    # --- Piyasa ---
    implied: dict[str, float] = {}
    normalized: dict[str, float] = {}
    overround: float | None = None
    if odds_1x2:
        implied = calculate_implied_probability(odds_1x2)
        if implied:
            overround = calculate_overround(implied)
            normalized = normalize_probabilities(implied)

    market_block = {
        "available": bool(normalized),
        "source": "api" if normalized else "unavailable",
        "raw": {key: round(value, 4) for key, value in implied.items()},
        "normalized": {key: round(value, 4) for key, value in normalized.items()},
        "overround": round(overround, 4) if overround else None,
        "margin_pct": round((overround - 1.0) * 100, 2) if overround else None,
        "method": MARGIN_METHOD if normalized else None,
    }

    model_1x2 = model_result["markets"].get("match_result", {}) if model_result["available"] else {}
    value_result = calculate_value_score(model_1x2, normalized)
    movement = calculate_odds_movement(list(snapshots), kickoff)

    home_at_home = sum(1 for record in home_records if record.is_home)
    away_at_away = sum(1 for record in away_records if not record.is_home)

    data_quality = calculate_data_quality(
        bet365_available=bool(odds_1x2),
        form_matches=min(len(home_records), len(away_records)),
        h2h_matches=h2h_matches,
        home_away_available=home_at_home >= 2 and away_at_away >= 2,
        predictions_available=predictions_available,
        injuries_available=injuries_available,
        snapshot_count=movement.get("snapshot_count", 0),
        weights=data_quality_weights,
    )

    confidence = calculate_confidence(
        model_result=model_result,
        value_result=value_result,
        movement=movement,
        home_records=home_records,
        away_records=away_records,
        h2h_matches=h2h_matches,
        injuries_available=injuries_available,
        home_injuries=home_injuries,
        away_injuries=away_injuries,
        data_quality_score=data_quality["score"],
        weights=confidence_weights,
        model_config=model_config,
    )

    explanation = build_explanation(
        home_name=home_name,
        away_name=away_name,
        model_result=model_result,
        value_result=value_result,
        movement=movement,
        odds=odds_1x2,
        data_quality=data_quality,
        home_records=home_records,
        away_records=away_records,
    )

    return {
        "model": model_result,
        "market": market_block,
        "value": value_result,
        "movement": movement,
        "confidence": confidence,
        "data_quality": data_quality,
        "explanation": explanation,
    }


def rank_top_matches(
    analyses: Sequence[dict[str, Any]],
    size: int = 5,
    min_data_quality: float = 50.0,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Top-5 siralamasi.

    Ham olasiliga gore DEGIL, ``fark x guven`` carpimina gore siralanir ve
    veri kalitesi esigini gecmeyen maclar disarida birakilir.
    """
    excluded = {"bet365_yok": 0, "model_yok": 0, "dusuk_veri_kalitesi": 0}
    scored: list[tuple[float, dict[str, Any]]] = []

    for item in analyses:
        analysis = item.get("analysis") or {}
        if not (analysis.get("market") or {}).get("available"):
            excluded["bet365_yok"] += 1
            continue
        if not (analysis.get("model") or {}).get("available"):
            excluded["model_yok"] += 1
            continue
        if (analysis.get("data_quality") or {}).get("score", 0) < min_data_quality:
            excluded["dusuk_veri_kalitesi"] += 1
            continue
        edge = (analysis.get("value") or {}).get("best_edge")
        confidence = (analysis.get("confidence") or {}).get("score", 0.0)
        if edge is None:
            excluded["model_yok"] += 1
            continue
        score = max(0.0, float(edge)) * (float(confidence) / 100.0)
        scored.append((score, item))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[:size]], excluded
