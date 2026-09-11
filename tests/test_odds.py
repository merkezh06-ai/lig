"""Oran parser ve Bet365 kesfi testleri.

En kritik kural burada test edilir: BASKA BOOKMAKER BET365 YERINE GECMEZ.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bookmakers import find_bookmaker  # noqa: E402
from odds_parser import (  # noqa: E402
    MARKET_1X2,
    MARKET_BTTS,
    MARKET_DC,
    MARKET_HT_1X2,
    MARKET_OU25,
    extract_markets,
    market_1x2,
    parse_odds_rows,
    select_bookmaker,
    to_float,
)

BET365_ID = 8
OTHER_ID = 6


def bet(name: str, values: list[tuple[str, str]]) -> dict:
    return {"name": name, "values": [{"value": v, "odd": o} for v, o in values]}


BET365_BETS = [
    bet("Match Winner", [("Home", "2.10"), ("Draw", "3.40"), ("Away", "3.20")]),
    bet("Both Teams Score", [("Yes", "1.72"), ("No", "2.05")]),
    bet("Goals Over/Under", [("Over 1.5", "1.25"), ("Over 2.5", "1.85"),
                             ("Under 2.5", "1.95"), ("Under 1.5", "3.80")]),
    bet("First Half Winner", [("Home", "2.90"), ("Draw", "2.10"), ("Away", "4.20")]),
    bet("Double Chance", [("Home/Draw", "1.30"), ("Home/Away", "1.28"), ("Draw/Away", "1.55")]),
    bet("Asian Handicap", [("Home -0.5", "2.10"), ("Away +0.5", "1.75")]),
]

OTHER_BETS = [
    bet("Match Winner", [("Home", "9.99"), ("Draw", "9.99"), ("Away", "9.99")]),
]


def odds_row(fixture_id: int, bookmaker_ids: list[int]) -> dict:
    books = []
    for bookmaker_id in bookmaker_ids:
        books.append(
            {
                "id": bookmaker_id,
                "name": "Bet365" if bookmaker_id == BET365_ID else "Bwin",
                "bets": BET365_BETS if bookmaker_id == BET365_ID else OTHER_BETS,
            }
        )
    return {
        "fixture": {"id": fixture_id, "date": "2026-09-07T18:00:00+00:00"},
        "update": "2026-09-07T12:00:00+00:00",
        "bookmakers": books,
    }


class TestOddValues(unittest.TestCase):
    def test_to_float_accepts_valid(self) -> None:
        self.assertAlmostEqual(to_float("2.10"), 2.10)
        self.assertAlmostEqual(to_float("2,10"), 2.10)
        self.assertAlmostEqual(to_float(3), 3.0)

    def test_to_float_rejects_impossible(self) -> None:
        for value in (None, "", "abc", "1.00", "0.5", "-2"):
            self.assertIsNone(to_float(value), value)


class TestMarketExtraction(unittest.TestCase):
    def test_extracts_known_markets(self) -> None:
        markets = extract_markets(BET365_BETS)
        self.assertEqual(markets[MARKET_1X2], {"1": 2.10, "X": 3.40, "2": 3.20})
        self.assertEqual(markets[MARKET_BTTS], {"var": 1.72, "yok": 2.05})
        self.assertEqual(markets[MARKET_OU25], {"ust": 1.85, "alt": 1.95})
        self.assertEqual(markets[MARKET_HT_1X2], {"1": 2.90, "X": 2.10, "2": 4.20})
        self.assertEqual(markets[MARKET_DC]["1X"], 1.30)

    def test_over_under_picks_the_right_line(self) -> None:
        markets = extract_markets(BET365_BETS)
        # 1.5 cizgisi 2.5 marketine sizmamali.
        self.assertNotIn(1.25, markets[MARKET_OU25].values())

    def test_unknown_markets_are_skipped_not_invented(self) -> None:
        markets = extract_markets([bet("Corners Over/Under", [("Over 9.5", "1.90")])])
        self.assertEqual(markets, {})

    def test_incomplete_1x2_is_rejected(self) -> None:
        markets = extract_markets([bet("Match Winner", [("Home", "2.10"), ("Draw", "3.40")])])
        self.assertNotIn(MARKET_1X2, markets)

    def test_alternate_naming(self) -> None:
        markets = extract_markets([bet("1X2", [("1", "2.00"), ("X", "3.00"), ("2", "4.00")])])
        self.assertEqual(markets[MARKET_1X2], {"1": 2.00, "X": 3.00, "2": 4.00})

    def test_zero_and_text_odds_do_not_create_a_market(self) -> None:
        markets = extract_markets(
            [bet("Match Winner", [("Home", "0"), ("Draw", "-"), ("Away", "x")])]
        )
        self.assertEqual(markets, {})


class TestBookmakerSelection(unittest.TestCase):
    def test_selects_only_the_requested_bookmaker(self) -> None:
        row = odds_row(1, [OTHER_ID, BET365_ID])
        selected = select_bookmaker(row, BET365_ID)
        self.assertIsNotNone(selected)
        self.assertEqual(selected["name"], "Bet365")  # type: ignore[index]

    def test_returns_none_when_bookmaker_absent(self) -> None:
        row = odds_row(1, [OTHER_ID])
        self.assertIsNone(select_bookmaker(row, BET365_ID))

    def test_never_falls_back_to_another_bookmaker(self) -> None:
        """Bet365 yoksa fixture SONUCA HIC GIRMEZ - baska oranla doldurulmaz."""
        rows = [odds_row(1, [BET365_ID]), odds_row(2, [OTHER_ID])]
        parsed = parse_odds_rows(rows, BET365_ID, "Bet365")
        self.assertIn(1, parsed)
        self.assertNotIn(2, parsed)
        # Bwin'in 9.99 orani hicbir yerde gorunmemeli.
        self.assertNotIn(9.99, parsed[1]["markets"][MARKET_1X2].values())

    def test_parsed_entry_carries_provenance(self) -> None:
        parsed = parse_odds_rows([odds_row(7, [BET365_ID])], BET365_ID, "Bet365")
        entry = parsed[7]
        self.assertEqual(entry["bookmaker_id"], BET365_ID)
        self.assertEqual(entry["bookmaker_name"], "Bet365")
        self.assertIsNotNone(entry["updated_at"])

    def test_market_1x2_helper(self) -> None:
        parsed = parse_odds_rows([odds_row(7, [BET365_ID])], BET365_ID, "Bet365")
        self.assertEqual(market_1x2(parsed[7]), {"1": 2.10, "X": 3.40, "2": 3.20})
        self.assertIsNone(market_1x2(None))
        self.assertIsNone(market_1x2({"markets": {}}))

    def test_bookmaker_without_known_markets_is_dropped(self) -> None:
        row = {
            "fixture": {"id": 3, "date": "2026-09-07T18:00:00+00:00"},
            "bookmakers": [{"id": BET365_ID, "name": "Bet365",
                            "bets": [bet("Corners", [("Over 9.5", "1.90")])]}],
        }
        self.assertEqual(parse_odds_rows([row], BET365_ID, "Bet365"), {})


class TestBookmakerDiscovery(unittest.TestCase):
    ROWS = [
        {"id": 1, "name": "10Bet"},
        {"id": 6, "name": "Bwin"},
        {"id": 8, "name": "Bet365"},
        {"id": 27, "name": "Betsson"},
    ]

    def test_finds_bet365_by_name(self) -> None:
        found = find_bookmaker(self.ROWS, "Bet365")
        self.assertIsNotNone(found)
        self.assertEqual(found.id, 8)  # type: ignore[union-attr]

    def test_case_and_spacing_insensitive(self) -> None:
        self.assertEqual(find_bookmaker(self.ROWS, "bet 365").id, 8)  # type: ignore[union-attr]

    def test_returns_none_when_absent(self) -> None:
        rows = [row for row in self.ROWS if row["id"] != 8]
        self.assertIsNone(find_bookmaker(rows, "Bet365"))

    def test_does_not_match_a_different_brand(self) -> None:
        self.assertIsNone(find_bookmaker([{"id": 5, "name": "365Bet"}], "Bet365"))

    def test_prefix_variant_is_accepted(self) -> None:
        found = find_bookmaker([{"id": 99, "name": "Bet365 Bonus"}], "Bet365")
        self.assertEqual(found.id, 99)  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main(verbosity=2)
