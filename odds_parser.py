"""Bet365 oranlarinin normalizasyonu.

Iki katı kural:

1. Yalnizca kesfedilen Bet365 bookmaker ID'sine ait blok okunur. Baska
   bookmaker'in orani hicbir kosulda Bet365 yerine kullanilmaz.
2. Market eslestirmesi bet ID'sine degil bet ADINA gore yapilir.
   API-Football'da pre-match ve live bet ID'leri ayri sistemlerdir; ID'ye
   guvenmek sessizce yanlis market okumaya yol acar.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

# Market anahtarlari
MARKET_1X2 = "1x2"
MARKET_BTTS = "kg"
MARKET_OU25 = "au25"
MARKET_HT_1X2 = "iy1x2"
MARKET_DC = "cifte_sans"

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _norm(text: Any) -> str:
    return _NON_ALNUM.sub(" ", str(text or "").strip().lower()).strip()


def to_float(value: Any) -> float | None:
    """Oran metnini sayiya cevirir. Cevrilemiyorsa None - tahmin yapilmaz."""
    if value is None:
        return None
    try:
        number = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    # 1.00 ve alti gecerli bir bahis orani degildir.
    return number if number > 1.0 else None


# --------------------------------------------------------------------------
# Market tanimlari
# --------------------------------------------------------------------------
# Her market icin: kabul edilen bet adlari + deger etiketi eslemesi.
_BET_NAMES: dict[str, tuple[str, ...]] = {
    MARKET_1X2: ("match winner", "1x2", "full time result", "fulltime result", "match result"),
    MARKET_BTTS: ("both teams score", "both teams to score", "btts", "goal goal no goal"),
    MARKET_OU25: ("goals over under", "over under", "total goals", "goals over/under"),
    MARKET_HT_1X2: (
        "first half winner",
        "1st half winner",
        "half time result",
        "halftime result",
        "1st half 1x2",
    ),
    MARKET_DC: ("double chance",),
}

_OUTCOME_1X2: dict[str, str] = {
    "home": "1",
    "1": "1",
    "draw": "X",
    "x": "X",
    "away": "2",
    "2": "2",
}

_OUTCOME_BTTS: dict[str, str] = {
    "yes": "var",
    "no": "yok",
    "var": "var",
    "yok": "yok",
}

_OUTCOME_DC: dict[str, str] = {
    "home draw": "1X",
    "1x": "1X",
    "home away": "12",
    "12": "12",
    "draw away": "X2",
    "x2": "X2",
}

# Ondalik nokta korunmali, bu yuzden _norm() yerine ham metin uzerinde calisir.
_OU_PATTERN = re.compile(
    r"^(over|under|ust|üst|alt)\s*[:\-]?\s*([0-9]+(?:[.,][0-9]+)?)$", re.IGNORECASE
)


def _match_market(bet_name: Any) -> str | None:
    normalized = _norm(bet_name)
    if not normalized:
        return None
    for market, names in _BET_NAMES.items():
        for candidate in names:
            if normalized == _norm(candidate):
                return market
    # Tam eslesme yoksa "icerir" kontrolu (ör. "Goals Over/Under First Half"
    # gibi varyantlar yanlislikla ana markete dusmemeli, o yuzden dikkatli).
    if normalized in {"result 1x2", "winner"}:
        return MARKET_1X2
    return None


def match_market_name(bet_name: Any) -> str | None:
    """Bir bet adini tanidigimiz market anahtarina cevirir (yoksa None).

    Public: teshis araci "API market gonderiyor ama parser tanimiyor mu?"
    sorusunu bununla cevapliyor.
    """
    return _match_market(bet_name)


def _parse_1x2(values: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for item in values:
        key = _OUTCOME_1X2.get(_norm(item.get("value")))
        odd = to_float(item.get("odd"))
        if key and odd:
            result[key] = odd
    return result if len(result) == 3 else {}


def _parse_btts(values: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for item in values:
        key = _OUTCOME_BTTS.get(_norm(item.get("value")))
        odd = to_float(item.get("odd"))
        if key and odd:
            result[key] = odd
    return result if len(result) == 2 else {}


def _parse_double_chance(values: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for item in values:
        key = _OUTCOME_DC.get(_norm(item.get("value")))
        odd = to_float(item.get("odd"))
        if key and odd:
            result[key] = odd
    return result


def _parse_over_under(
    values: Iterable[Mapping[str, Any]], line: str = "2.5"
) -> dict[str, float]:
    """Sadece istenen cizgiyi (varsayilan 2.5) alir."""
    result: dict[str, float] = {}
    try:
        target = float(line.replace(",", "."))
    except ValueError:  # pragma: no cover - cagirandan gelen hatali cizgi
        return {}

    for item in values:
        raw = str(item.get("value") or "").strip()
        match = _OU_PATTERN.match(raw)
        if not match:
            continue
        side = match.group(1).lower()
        try:
            found_line = float(match.group(2).replace(",", "."))
        except ValueError:  # pragma: no cover
            continue
        if abs(found_line - target) > 1e-9:
            continue
        odd = to_float(item.get("odd"))
        if not odd:
            continue
        result["ust" if side in {"over", "ust", "üst"} else "alt"] = odd
    return result if len(result) == 2 else {}


_PARSERS = {
    MARKET_1X2: _parse_1x2,
    MARKET_BTTS: _parse_btts,
    MARKET_DC: _parse_double_chance,
    MARKET_HT_1X2: _parse_1x2,
    MARKET_OU25: _parse_over_under,
}


def extract_markets(bets: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
    """Bir bookmaker'in bet listesinden tanidigimiz marketleri cikarir.

    Tanimadigimiz marketler sessizce atlanir - uydurulmaz.
    """
    markets: dict[str, dict[str, float]] = {}
    for bet in bets or []:
        market = _match_market(bet.get("name"))
        if market is None or market in markets:
            continue
        parser = _PARSERS[market]
        parsed = parser(bet.get("values") or [])
        if parsed:
            markets[market] = parsed
    return markets


# --------------------------------------------------------------------------
# Bookmaker secimi
# --------------------------------------------------------------------------
def select_bookmaker(
    odds_row: Mapping[str, Any], bookmaker_id: int
) -> Mapping[str, Any] | None:
    """Yalnizca verilen ID'ye sahip bookmaker blogunu dondurur.

    Bulunamazsa None. BASKA BOOKMAKER'A DUSULMEZ.
    """
    for bookmaker in odds_row.get("bookmakers") or []:
        try:
            if int(bookmaker.get("id")) == int(bookmaker_id):
                return bookmaker
        except (TypeError, ValueError):
            continue
    return None


def parse_odds_rows(
    rows: Iterable[Mapping[str, Any]],
    bookmaker_id: int,
    bookmaker_name: str,
) -> dict[int, dict[str, Any]]:
    """/odds cevabini ``{fixture_id: {...}}`` sozlugune cevirir.

    Yalnizca istenen bookmaker'in verisi alinir; o bookmaker o mac icin
    yoksa fixture sonuca HIC eklenmez (bos oran uretilmez).
    """
    parsed: dict[int, dict[str, Any]] = {}
    for row in rows:
        fixture = row.get("fixture") or {}
        try:
            fixture_id = int(fixture.get("id"))
        except (TypeError, ValueError):
            continue

        bookmaker = select_bookmaker(row, bookmaker_id)
        if bookmaker is None:
            continue

        markets = extract_markets(bookmaker.get("bets") or [])
        if not markets:
            continue

        parsed[fixture_id] = {
            "fixture_id": fixture_id,
            "bookmaker_id": bookmaker_id,
            "bookmaker_name": bookmaker_name,
            "updated_at": row.get("update"),
            "markets": markets,
            "kickoff": fixture.get("date"),
        }
    return parsed


def market_1x2(parsed_entry: Mapping[str, Any] | None) -> dict[str, float] | None:
    """Bir mac girdisinden 1X2 oranlarini alir (yoksa None)."""
    if not parsed_entry:
        return None
    values = (parsed_entry.get("markets") or {}).get(MARKET_1X2)
    if not values or len(values) != 3:
        return None
    return dict(values)
