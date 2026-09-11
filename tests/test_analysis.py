"""Analiz motoru testleri.

``python -m unittest discover tests`` veya ``pytest tests/`` ile calisir.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis_engine import (  # noqa: E402
    LeagueAverages,
    MatchRecord,
    calculate_confidence,
    calculate_data_quality,
    calculate_implied_probability,
    calculate_league_averages,
    calculate_model_probability,
    calculate_odds_movement,
    calculate_overround,
    calculate_team_strength,
    calculate_value_score,
    dixon_coles_tau,
    generate_match_analysis,
    halftime_share,
    markets_from_matrix,
    most_likely_scoreline,
    normalize_probabilities,
    poisson_pmf,
    rank_top_matches,
    score_matrix,
)
from config import ConfidenceWeights, DataQualityWeights, ModelConfig  # noqa: E402

NOW = datetime(2026, 9, 7, 18, 0, tzinfo=timezone.utc)


def make_records(
    count: int,
    *,
    goals_for: int = 2,
    goals_against: int = 1,
    alternate_venue: bool = True,
    with_halftime: bool = True,
) -> list[MatchRecord]:
    records = []
    for index in range(count):
        records.append(
            MatchRecord(
                fixture_id=1000 + index,
                is_home=(index % 2 == 0) if alternate_venue else True,
                goals_for=goals_for,
                goals_against=goals_against,
                kickoff=NOW - timedelta(days=7 * (index + 1)),
                ht_goals_for=(goals_for // 2) if with_halftime else None,
                ht_goals_against=(goals_against // 2) if with_halftime else None,
            )
        )
    return records


class TestPoisson(unittest.TestCase):
    def test_pmf_is_a_distribution(self) -> None:
        total = sum(poisson_pmf(k, 1.4) for k in range(0, 40))
        self.assertAlmostEqual(total, 1.0, places=9)

    def test_pmf_zero_lambda(self) -> None:
        self.assertEqual(poisson_pmf(0, 0.0), 1.0)
        self.assertEqual(poisson_pmf(3, 0.0), 0.0)

    def test_tau_neutral_outside_low_scores(self) -> None:
        self.assertEqual(dixon_coles_tau(3, 2, 1.4, 1.1, -0.05), 1.0)

    def test_tau_raises_draws_when_rho_negative(self) -> None:
        self.assertGreater(dixon_coles_tau(0, 0, 1.4, 1.1, -0.05), 1.0)
        self.assertGreater(dixon_coles_tau(1, 1, 1.4, 1.1, -0.05), 1.0)
        self.assertLess(dixon_coles_tau(1, 0, 1.4, 1.1, -0.05), 1.0)


class TestScoreMatrix(unittest.TestCase):
    def test_matrix_normalises_to_one(self) -> None:
        matrix = score_matrix(1.6, 1.2, -0.05, 10)
        total = sum(sum(row) for row in matrix)
        self.assertAlmostEqual(total, 1.0, places=9)

    def test_rho_zero_matches_independent_poisson(self) -> None:
        matrix = score_matrix(1.5, 1.1, 0.0, 12)
        expected = poisson_pmf(2, 1.5) * poisson_pmf(1, 1.1)
        self.assertAlmostEqual(matrix[2][1], expected, places=5)

    def test_no_negative_cells(self) -> None:
        matrix = score_matrix(0.4, 0.3, -0.9, 8)
        for row in matrix:
            for value in row:
                self.assertGreaterEqual(value, 0.0)

    def test_stronger_home_side_wins_more(self) -> None:
        markets = markets_from_matrix(score_matrix(2.2, 0.8, -0.05, 10))
        result = markets["match_result"]
        self.assertGreater(result["1"], result["2"])
        self.assertGreater(result["1"], 0.5)

    def test_markets_are_internally_consistent(self) -> None:
        markets = markets_from_matrix(score_matrix(1.7, 1.3, -0.05, 10))
        self.assertAlmostEqual(sum(markets["match_result"].values()), 1.0, places=9)
        self.assertAlmostEqual(sum(markets["both_teams_to_score"].values()), 1.0, places=9)
        self.assertAlmostEqual(sum(markets["over_under_25"].values()), 1.0, places=9)
        # Cifte sans, tekil sonuclarla tutarli olmali.
        self.assertAlmostEqual(
            markets["double_chance"]["1X"],
            markets["match_result"]["1"] + markets["match_result"]["X"],
            places=9,
        )
        # 1.5 ust her zaman 2.5 ustten buyuk olmali.
        self.assertGreater(markets["over_under_15"]["ust"], markets["over_under_25"]["ust"])

    def test_most_likely_scoreline(self) -> None:
        best = most_likely_scoreline(score_matrix(0.5, 0.4, -0.05, 8))
        self.assertEqual((best["home"], best["away"]), (0, 0))


class TestMarketMath(unittest.TestCase):
    def test_implied_probability_matches_spec_example(self) -> None:
        implied = calculate_implied_probability({"1": 1.80, "X": 3.50, "2": 4.20})
        self.assertAlmostEqual(implied["1"] * 100, 55.56, places=2)
        self.assertAlmostEqual(implied["X"] * 100, 28.57, places=2)
        self.assertAlmostEqual(implied["2"] * 100, 23.81, places=2)

    def test_overround_matches_spec_example(self) -> None:
        implied = calculate_implied_probability({"1": 1.80, "X": 3.50, "2": 4.20})
        self.assertAlmostEqual(calculate_overround(implied) * 100, 107.94, places=2)

    def test_normalisation_sums_to_one(self) -> None:
        implied = calculate_implied_probability({"1": 1.80, "X": 3.50, "2": 4.20})
        normalized = normalize_probabilities(implied)
        self.assertAlmostEqual(sum(normalized.values()), 1.0, places=9)
        self.assertLess(normalized["1"], implied["1"])  # marj cikarilinca duser

    def test_invalid_odds_are_ignored_not_invented(self) -> None:
        implied = calculate_implied_probability({"1": 0.0, "X": None, "2": "abc"})  # type: ignore[dict-item]
        self.assertEqual(implied, {})
        self.assertEqual(normalize_probabilities(implied), {})

    def test_value_edge(self) -> None:
        value = calculate_value_score({"1": 0.61, "X": 0.22, "2": 0.17}, {"1": 0.53, "X": 0.26, "2": 0.21})
        self.assertTrue(value["available"])
        self.assertAlmostEqual(value["edges"]["1"], 8.0, places=2)
        self.assertEqual(value["best_outcome"], "1")

    def test_value_edge_unavailable_without_market(self) -> None:
        value = calculate_value_score({"1": 0.61}, {})
        self.assertFalse(value["available"])
        self.assertIsNotNone(value["note"])


class TestOddsMovement(unittest.TestCase):
    def _snapshot(self, hours_ago: int, home: float, draw: float, away: float) -> dict:
        return {
            "home_odds": home,
            "draw_odds": draw,
            "away_odds": away,
            "captured_at": (NOW - timedelta(hours=hours_ago)).isoformat(),
        }

    def test_movement_matches_spec_example(self) -> None:
        movement = calculate_odds_movement(
            [self._snapshot(9, 2.40, 3.30, 3.00), self._snapshot(1, 2.15, 3.40, 3.30)],
            kickoff=NOW + timedelta(hours=2),
        )
        self.assertTrue(movement["available"])
        self.assertAlmostEqual(movement["change_pct"]["1"], -10.42, places=2)
        self.assertEqual(movement["direction"]["1"], "dustu")

    def test_single_snapshot_does_not_invent_history(self) -> None:
        movement = calculate_odds_movement([self._snapshot(1, 2.15, 3.40, 3.30)])
        self.assertFalse(movement["available"])
        self.assertEqual(movement["snapshot_count"], 1)
        self.assertIsNone(movement["latest"])
        self.assertIn("uydurulmaz", movement["note"])

    def test_no_snapshot_reports_absence(self) -> None:
        movement = calculate_odds_movement([])
        self.assertFalse(movement["available"])
        self.assertEqual(movement["change_pct"], {})
        self.assertIn("bulunmuyor", movement["note"])

    def test_labels_never_claim_opening_or_t15(self) -> None:
        movement = calculate_odds_movement(
            [self._snapshot(9, 2.40, 3.30, 3.00), self._snapshot(1, 2.15, 3.40, 3.30)],
            kickoff=NOW + timedelta(hours=2),
        )
        labels = " ".join(
            point["label"]
            for point in (
                movement["first_recorded"],
                movement["latest"],
                movement["closest_to_kickoff"],
            )
        ).lower()
        self.assertNotIn("acilis", labels)
        self.assertNotIn("t-15", labels)
        self.assertIn("ilk kaydedilen", labels)


class TestTeamStrength(unittest.TestCase):
    def test_insufficient_data_is_reported_not_guessed(self) -> None:
        config = ModelConfig()
        league = LeagueAverages(1.5, 1.2, 100)
        strength = calculate_team_strength(make_records(2), league, config)
        self.assertFalse(strength.available)
        self.assertIn("Yeterli mac verisi yok", strength.note or "")

    def test_average_team_gets_neutral_strength(self) -> None:
        config = ModelConfig(shrinkage_k=0.0, form_weight=0.0)
        league = LeagueAverages(1.5, 1.2, 100)
        records = [
            MatchRecord(i, is_home=True, goals_for=1, goals_against=1, kickoff=NOW)
            for i in range(6)
        ]
        strength = calculate_team_strength(records, league, config)
        self.assertTrue(strength.available)
        self.assertAlmostEqual(strength.attack_home, 1.0 / 1.5, places=6)

    def test_league_averages_count_each_match_once(self) -> None:
        records = [
            MatchRecord(1, is_home=True, goals_for=2, goals_against=1),
            MatchRecord(1, is_home=False, goals_for=1, goals_against=2),
        ]
        league = calculate_league_averages(records)
        self.assertEqual(league.matches, 1)
        self.assertAlmostEqual(league.home_goals, 2.0)
        self.assertAlmostEqual(league.away_goals, 1.0)

    def test_halftime_share_needs_real_data(self) -> None:
        share, used = halftime_share(make_records(6, with_halftime=False))
        self.assertIsNone(share)
        self.assertEqual(used, 0)

    def test_halftime_share_from_real_data(self) -> None:
        records = [
            MatchRecord(i, is_home=True, goals_for=2, goals_against=0, ht_goals_for=1, ht_goals_against=0)
            for i in range(6)
        ]
        share, used = halftime_share(records)
        self.assertEqual(used, 6)
        self.assertAlmostEqual(share or 0, 0.5, places=6)


class TestModel(unittest.TestCase):
    def test_model_unavailable_with_thin_data(self) -> None:
        result = calculate_model_probability(make_records(2), make_records(2), ModelConfig())
        self.assertFalse(result["available"])
        self.assertIsNone(result["expected_goals_home"])

    def test_model_runs_with_enough_data(self) -> None:
        home = make_records(8, goals_for=2, goals_against=1)
        away = make_records(8, goals_for=1, goals_against=2)
        result = calculate_model_probability(home, away, ModelConfig())
        self.assertTrue(result["available"])
        self.assertGreater(result["markets"]["match_result"]["1"], result["markets"]["match_result"]["2"])
        self.assertAlmostEqual(sum(result["markets"]["match_result"].values()), 1.0, places=9)

    def test_first_half_unavailable_without_halftime_data(self) -> None:
        home = make_records(8, with_halftime=False)
        away = make_records(8, with_halftime=False)
        result = calculate_model_probability(home, away, ModelConfig())
        self.assertFalse(result["first_half"]["available"])
        self.assertEqual(result["first_half"]["probabilities"], {})
        self.assertIn("yeterli veri yok", (result["first_half"]["note"] or "").lower())

    def test_first_half_goals_are_below_full_time(self) -> None:
        home = [
            MatchRecord(i, is_home=(i % 2 == 0), goals_for=2, goals_against=1,
                        kickoff=NOW - timedelta(days=i), ht_goals_for=1, ht_goals_against=0)
            for i in range(8)
        ]
        away = [
            MatchRecord(100 + i, is_home=(i % 2 == 1), goals_for=1, goals_against=2,
                        kickoff=NOW - timedelta(days=i), ht_goals_for=1, ht_goals_against=1)
            for i in range(8)
        ]
        result = calculate_model_probability(home, away, ModelConfig())
        self.assertTrue(result["first_half"]["available"])
        self.assertLess(
            result["first_half"]["expected_goals_home"], result["expected_goals_home"]
        )

    def test_lambdas_are_clamped(self) -> None:
        config = ModelConfig(max_lambda=2.0)
        home = make_records(10, goals_for=9, goals_against=0)
        away = make_records(10, goals_for=0, goals_against=9)
        result = calculate_model_probability(home, away, config)
        self.assertLessEqual(result["expected_goals_home"], 2.0)


class TestScores(unittest.TestCase):
    def test_data_quality_full_marks(self) -> None:
        weights = DataQualityWeights()
        quality = calculate_data_quality(
            bet365_available=True,
            form_matches=10,
            h2h_matches=6,
            home_away_available=True,
            predictions_available=True,
            injuries_available=True,
            snapshot_count=4,
            weights=weights,
        )
        self.assertAlmostEqual(quality["score"], 100.0, places=1)
        self.assertEqual(quality["missing"], [])

    def test_data_quality_reports_missing(self) -> None:
        quality = calculate_data_quality(
            bet365_available=False,
            form_matches=0,
            h2h_matches=0,
            home_away_available=False,
            predictions_available=False,
            injuries_available=False,
            snapshot_count=0,
            weights=DataQualityWeights(),
        )
        self.assertEqual(quality["score"], 0.0)
        self.assertEqual(len(quality["missing"]), 7)

    def test_confidence_zero_when_nothing_available(self) -> None:
        confidence = calculate_confidence(
            model_result={"available": False, "note": "yok"},
            value_result={"available": False},
            movement={"available": False},
            home_records=[],
            away_records=[],
            h2h_matches=0,
            injuries_available=False,
            home_injuries=0,
            away_injuries=0,
            data_quality_score=0.0,
            weights=ConfidenceWeights(),
            model_config=ModelConfig(),
        )
        self.assertEqual(confidence["score"], 0.0)
        self.assertTrue(all(not c["available"] for c in confidence["components"]))

    def test_confidence_never_exceeds_max(self) -> None:
        home = make_records(10, goals_for=3, goals_against=0)
        away = make_records(10, goals_for=0, goals_against=3)
        model = calculate_model_probability(home, away, ModelConfig())
        confidence = calculate_confidence(
            model_result=model,
            value_result={"available": True, "best_edge": 1.0},
            movement={"available": True, "change_pct": {"1": 0.1}},
            home_records=home,
            away_records=away,
            h2h_matches=10,
            injuries_available=True,
            home_injuries=0,
            away_injuries=0,
            data_quality_score=100.0,
            weights=ConfidenceWeights(),
            model_config=ModelConfig(),
        )
        self.assertLessEqual(confidence["score"], confidence["max_score"])
        self.assertGreater(confidence["score"], 60)

    def test_large_disagreement_lowers_market_agreement(self) -> None:
        close = calculate_confidence(
            model_result={"available": False},
            value_result={"available": True, "best_edge": 2.0},
            movement={"available": False},
            home_records=[], away_records=[], h2h_matches=0,
            injuries_available=False, home_injuries=0, away_injuries=0,
            data_quality_score=0.0, weights=ConfidenceWeights(), model_config=ModelConfig(),
        )
        far = calculate_confidence(
            model_result={"available": False},
            value_result={"available": True, "best_edge": 30.0},
            movement={"available": False},
            home_records=[], away_records=[], h2h_matches=0,
            injuries_available=False, home_injuries=0, away_injuries=0,
            data_quality_score=0.0, weights=ConfidenceWeights(), model_config=ModelConfig(),
        )
        self.assertGreater(close["score"], far["score"])


class TestFullAnalysis(unittest.TestCase):
    def _analysis(self, **overrides):
        payload = dict(
            home_name="Real Betis",
            away_name="Girona",
            home_records=make_records(8, goals_for=2, goals_against=1),
            away_records=make_records(8, goals_for=1, goals_against=2),
            odds_1x2={"1": 2.31, "X": 3.40, "2": 3.10},
            snapshots=[],
            kickoff=NOW + timedelta(hours=3),
            h2h_matches=4,
            injuries_available=True,
            home_injuries=1,
            away_injuries=2,
            predictions_available=True,
        )
        payload.update(overrides)
        return generate_match_analysis(**payload)

    def test_full_analysis_shape(self) -> None:
        analysis = self._analysis()
        for key in ("model", "market", "value", "movement", "confidence", "data_quality", "explanation"):
            self.assertIn(key, analysis)
        self.assertTrue(analysis["model"]["available"])
        self.assertTrue(analysis["market"]["available"])
        self.assertGreater(analysis["market"]["overround"], 1.0)

    def test_analysis_without_odds_reports_absence(self) -> None:
        analysis = self._analysis(odds_1x2=None)
        self.assertFalse(analysis["market"]["available"])
        self.assertFalse(analysis["value"]["available"])
        self.assertEqual(analysis["market"]["raw"], {})

    def test_explanation_avoids_forbidden_language(self) -> None:
        text = " ".join(self._analysis()["explanation"]).lower()
        for banned in ("kesin", "garanti", "banko", "risksiz", "kesin kazanir"):
            self.assertNotIn(banned, text)

    def test_explanation_mentions_missing_snapshots(self) -> None:
        text = " ".join(self._analysis()["explanation"]).lower()
        self.assertIn("snapshot", text)


class TestRanking(unittest.TestCase):
    def _item(self, edge, confidence, quality, market=True, model=True):
        return {
            "analysis": {
                "market": {"available": market},
                "model": {"available": model},
                "value": {"best_edge": edge},
                "confidence": {"score": confidence},
                "data_quality": {"score": quality},
            }
        }

    def test_ranking_prefers_edge_times_confidence(self) -> None:
        items = [
            self._item(12.0, 40.0, 80),   # 4.8
            self._item(6.0, 90.0, 80),    # 5.4  -> ilk sirada olmali
            self._item(20.0, 10.0, 80),   # 2.0
        ]
        top, _ = rank_top_matches(items, size=3)
        self.assertEqual(top[0], items[1])

    def test_ranking_excludes_missing_or_low_quality(self) -> None:
        items = [
            self._item(10.0, 70.0, 80),
            self._item(10.0, 70.0, 20),          # dusuk veri kalitesi
            self._item(10.0, 70.0, 80, market=False),
            self._item(10.0, 70.0, 80, model=False),
        ]
        top, excluded = rank_top_matches(items, size=5, min_data_quality=50.0)
        self.assertEqual(len(top), 1)
        self.assertEqual(excluded["dusuk_veri_kalitesi"], 1)
        self.assertEqual(excluded["bet365_yok"], 1)
        self.assertEqual(excluded["model_yok"], 1)

    def test_empty_input_returns_empty(self) -> None:
        top, excluded = rank_top_matches([], size=5)
        self.assertEqual(top, [])
        self.assertEqual(sum(excluded.values()), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
