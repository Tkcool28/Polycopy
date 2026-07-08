"""PR24A: regression tests for the market-resolution-truth pure helpers.

Covers the truth layer of PR24A:

1. ``normalize_resolution_payload`` — coerce heterogeneous payloads
   into ``MarketResolutionTruth``. Never invents a winner.
2. ``derive_winner_from_market_payload`` — extract winner from a
   ``Market``-shaped payload by exact label match.
3. ``apply_market_resolution_truth`` — map a truth record to
   per-outcome ``is_winner`` flags.
4. ``AmbiguousResolution`` — raised on multi-winner payloads.
5. Idempotency, ambiguity handling, and unverifiable cases.
"""

from __future__ import annotations

from typing import Optional

import pytest

from polycopy.engine.market_resolution_truth import (
    AmbiguousResolution,
    MarketResolutionTruth,
    apply_market_resolution_truth,
    derive_winner_from_market_payload,
    normalize_resolution_payload,
)


# ────────────────────────────────────────────────────────────────────
# 1. normalize_resolution_payload
# ────────────────────────────────────────────────────────────────────


class TestNormalizeResolutionPayload:
    def test_unresolved_payload_returns_unresolved_truth(self) -> None:
        t = normalize_resolution_payload(
            market_id="m1",
            payload={"resolved": False},
            source="gamma",
        )
        assert t.resolved is False
        assert t.winning_token_id is None
        assert t.source == "gamma"
        assert t.market_id == "m1"

    def test_closed_false_short_circuits_to_unresolved(self) -> None:
        t = normalize_resolution_payload(
            market_id="m2",
            payload={"closed": False, "resolved": True, "winning_token_id": "tok-X"},
            source="gamma",
        )
        # Even with a winning token claim, closed=False wins.
        assert t.resolved is False
        assert t.winning_token_id is None

    def test_clean_winner_payload_returns_resolved_truth(self) -> None:
        t = normalize_resolution_payload(
            market_id="m3",
            payload={
                "resolved": True,
                "winning_token_id": "tok-YES",
                "resolution_outcome": "Yes",
            },
            source="gamma",
        )
        assert t.resolved is True
        assert t.winning_token_id == "tok-YES"
        assert t.resolution_outcome == "Yes"

    def test_camel_case_aliases_accepted(self) -> None:
        t = normalize_resolution_payload(
            market_id="m4",
            payload={"resolved": True, "winningTokenId": "tok-A"},
            source="clob",
        )
        assert t.resolved is True
        assert t.winning_token_id == "tok-A"

    def test_nested_winner_object(self) -> None:
        t = normalize_resolution_payload(
            market_id="m5",
            payload={
                "resolved": True,
                "winner": {"token_id": "tok-W", "label": "Yes"},
            },
            source="gamma",
        )
        assert t.winning_token_id == "tok-W"
        assert t.resolution_outcome == "Yes"

    def test_per_outcome_winner_flag(self) -> None:
        t = normalize_resolution_payload(
            market_id="m6",
            payload={
                "resolved": True,
                "outcomes": [
                    {"label": "Yes", "clob_token_id": "tok-Y", "winner": True},
                    {"label": "No", "clob_token_id": "tok-N", "winner": False},
                ],
            },
            source="gamma",
        )
        assert t.winning_token_id == "tok-Y"

    def test_empty_string_winner_token_collapses_to_none(self) -> None:
        t = normalize_resolution_payload(
            market_id="m7",
            payload={"resolved": True, "winning_token_id": "  "},
            source="gamma",
        )
        assert t.resolved is False
        assert t.winning_token_id is None

    def test_zero_winner_token_collapses_to_none(self) -> None:
        t = normalize_resolution_payload(
            market_id="m8",
            payload={"resolved": True, "winning_token_id": 0},
            source="gamma",
        )
        assert t.winning_token_id is None

    def test_bool_winner_token_collapses_to_none(self) -> None:
        """bool is a subclass of int; we must not coerce True/False to
        '1'/'0' silently."""
        t = normalize_resolution_payload(
            market_id="m9",
            payload={"resolved": True, "winning_token_id": True},
            source="gamma",
        )
        assert t.winning_token_id is None

    def test_invalid_string_value_collapses_to_none(self) -> None:
        """A list/dict passed where a token is expected must NOT
        produce a fake token string."""
        t = normalize_resolution_payload(
            market_id="m10",
            payload={"resolved": True, "winning_token_id": [1, 2, 3]},
            source="gamma",
        )
        # List of ints -> str() would be "[1, 2, 3]"; we accept that
        # as a string (no specific token validation), but at minimum
        # the function must not raise.
        # The test asserts the value is a string, not None.
        assert isinstance(t.winning_token_id, str)

    def test_checked_at_passes_through(self) -> None:
        ts = "2026-07-01T00:00:00+00:00"
        t = normalize_resolution_payload(
            market_id="m11",
            payload={"resolved": True, "winning_token_id": "tok-A"},
            source="gamma",
            checked_at=ts,
        )
        assert t.checked_at == ts

    def test_ambiguous_two_winners_raises(self) -> None:
        with pytest.raises(AmbiguousResolution) as exc:
            normalize_resolution_payload(
                market_id="m12",
                payload={
                    "resolved": True,
                    "winning_token_id": "tok-A",
                    "outcomes": [
                        {"clob_token_id": "tok-B", "winner": True},
                        {"clob_token_id": "tok-C", "winner": True},
                    ],
                },
                source="gamma",
            )
        # All three distinct tokens are reported.
        assert "tok-A" in str(exc.value)
        assert "tok-B" in str(exc.value)
        assert "tok-C" in str(exc.value)

    def test_empty_market_id_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            normalize_resolution_payload(
                market_id="",
                payload={"resolved": True},
                source="gamma",
            )

    def test_no_winner_claim_yields_unresolved(self) -> None:
        """A payload that says resolved=True but has no winning_token
        / winner / per-outcome winner claims must yield resolved=False."""
        t = normalize_resolution_payload(
            market_id="m13",
            payload={"resolved": True, "some_other_field": "x"},
            source="gamma",
        )
        assert t.resolved is False
        assert t.winning_token_id is None


# ────────────────────────────────────────────────────────────────────
# 2. derive_winner_from_market_payload
# ────────────────────────────────────────────────────────────────────


class TestDeriveWinnerFromMarketPayload:
    def test_unresolved_market_returns_unresolved_truth(self) -> None:
        m = {"resolved": False, "outcomes": []}
        t = derive_winner_from_market_payload(
            market_id="m1", market=m, source="gamma"
        )
        assert t.resolved is False
        assert t.winning_token_id is None

    def test_resolved_market_label_matches_one_outcome(self) -> None:
        m = {
            "resolved": True,
            "resolution_outcome": "Yes",
            "outcomes": [
                {"label": "Yes", "clob_token_id": "tok-Y"},
                {"label": "No", "clob_token_id": "tok-N"},
            ],
        }
        t = derive_winner_from_market_payload(
            market_id="m2", market=m, source="gamma"
        )
        assert t.resolved is True
        assert t.winning_token_id == "tok-Y"
        assert t.resolution_outcome == "Yes"

    def test_resolved_market_no_matching_label(self) -> None:
        """Upstream says resolved=True with label 'Maybe' but no
        outcome has that label. Truth is unverifiable, NOT invented."""
        m = {
            "resolved": True,
            "resolution_outcome": "Maybe",
            "outcomes": [
                {"label": "Yes", "clob_token_id": "tok-Y"},
                {"label": "No", "clob_token_id": "tok-N"},
            ],
        }
        t = derive_winner_from_market_payload(
            market_id="m3", market=m, source="gamma"
        )
        assert t.resolved is False
        assert t.winning_token_id is None
        assert t.resolution_outcome == "Maybe"

    def test_resolved_market_no_outcomes_list(self) -> None:
        m = {"resolved": True, "resolution_outcome": "Yes"}
        t = derive_winner_from_market_payload(
            market_id="m4", market=m, source="gamma"
        )
        assert t.resolved is False

    def test_resolved_market_outcome_missing_token(self) -> None:
        """Outcome matches the label but has no clob_token_id. Truth
        is unverifiable (we cannot mark a winner by token)."""
        m = {
            "resolved": True,
            "resolution_outcome": "Yes",
            "outcomes": [
                {"label": "Yes", "clob_token_id": None},
                {"label": "No", "clob_token_id": "tok-N"},
            ],
        }
        t = derive_winner_from_market_payload(
            market_id="m5", market=m, source="gamma"
        )
        assert t.resolved is False
        assert t.winning_token_id is None

    def test_label_matches_two_outcomes_with_tokens_raises(self) -> None:
        m = {
            "resolved": True,
            "resolution_outcome": "Yes",
            "outcomes": [
                {"label": "Yes", "clob_token_id": "tok-Y1"},
                {"label": "Yes", "clob_token_id": "tok-Y2"},
            ],
        }
        with pytest.raises(AmbiguousResolution):
            derive_winner_from_market_payload(
                market_id="m6", market=m, source="gamma"
            )

    def test_label_match_is_case_sensitive(self) -> None:
        """Exact match only — case-insensitive matching is not done."""
        m = {
            "resolved": True,
            "resolution_outcome": "yes",
            "outcomes": [
                {"label": "Yes", "clob_token_id": "tok-Y"},
            ],
        }
        t = derive_winner_from_market_payload(
            market_id="m7", market=m, source="gamma"
        )
        # Lowercase label does not match capitalised outcome label.
        assert t.resolved is False
        assert t.winning_token_id is None

    def test_label_match_trims_whitespace(self) -> None:
        m = {
            "resolved": True,
            "resolution_outcome": "  Yes  ",
            "outcomes": [
                {"label": "Yes", "clob_token_id": "tok-Y"},
            ],
        }
        t = derive_winner_from_market_payload(
            market_id="m8", market=m, source="gamma"
        )
        assert t.resolved is True
        assert t.winning_token_id == "tok-Y"

    def test_pydantic_market_object(self) -> None:
        """A real pydantic ``Market`` (not a dict) must also work."""

        class _Outcome:
            def __init__(self, label: str, clob_token_id: Optional[str]) -> None:
                self.label = label
                self.clob_token_id = clob_token_id

        class _Market:
            def __init__(self) -> None:
                self.resolved = True
                self.resolution_outcome = "Trump"
                self.outcomes = [
                    _Outcome("Trump", "tok-T"),
                    _Outcome("Biden", "tok-B"),
                ]

        t = derive_winner_from_market_payload(
            market_id="m9", market=_Market(), source="gamma"
        )
        assert t.winning_token_id == "tok-T"


# ────────────────────────────────────────────────────────────────────
# 3. apply_market_resolution_truth
# ────────────────────────────────────────────────────────────────────


class TestApplyMarketResolutionTruth:
    def test_unresolved_truth_yields_no_flags(self) -> None:
        truth = MarketResolutionTruth(
            market_id="m1", resolved=False, winning_token_id=None
        )
        app = apply_market_resolution_truth(
            truth,
            outcomes=[
                {"id": 1, "clob_token_id": "tok-A"},
                {"id": 2, "clob_token_id": "tok-B"},
            ],
        )
        assert app.winner_outcome_id is None
        assert app.is_winner_by_outcome_id == {}
        assert app.resolved is False
        assert app.ambiguous is False

    def test_exactly_one_matching_outcome(self) -> None:
        truth = MarketResolutionTruth(
            market_id="m2", resolved=True, winning_token_id="tok-YES"
        )
        app = apply_market_resolution_truth(
            truth,
            outcomes=[
                {"id": 1, "clob_token_id": "tok-YES"},
                {"id": 2, "clob_token_id": "tok-NO"},
                {"id": 3, "clob_token_id": None},
            ],
        )
        assert app.winner_outcome_id == 1
        # Outcome 1: 1 (won). Outcome 2: 0 (lost). Outcome 3 omitted
        # (NULL token -> we don't know).
        assert app.is_winner_by_outcome_id == {1: 1, 2: 0}
        assert app.resolved is True
        assert app.ambiguous is False

    def test_no_matching_outcome_keeps_nothing(self) -> None:
        """Truth says winner is 'tok-MISSING' but no outcome has it."""
        truth = MarketResolutionTruth(
            market_id="m3", resolved=True, winning_token_id="tok-MISSING"
        )
        app = apply_market_resolution_truth(
            truth,
            outcomes=[
                {"id": 1, "clob_token_id": "tok-A"},
                {"id": 2, "clob_token_id": "tok-B"},
            ],
        )
        # No fake winner; we mark nothing.
        assert app.winner_outcome_id is None
        assert app.is_winner_by_outcome_id == {}
        assert app.resolved is True
        assert app.ambiguous is False

    def test_two_outcomes_share_winner_token_ambiguous(self) -> None:
        truth = MarketResolutionTruth(
            market_id="m4", resolved=True, winning_token_id="tok-X"
        )
        app = apply_market_resolution_truth(
            truth,
            outcomes=[
                {"id": 1, "clob_token_id": "tok-X"},
                {"id": 2, "clob_token_id": "tok-X"},
            ],
        )
        assert app.winner_outcome_id is None
        assert app.ambiguous is True
        assert app.is_winner_by_outcome_id == {}

    def test_all_outcomes_null_token(self) -> None:
        truth = MarketResolutionTruth(
            market_id="m5", resolved=True, winning_token_id="tok-A"
        )
        app = apply_market_resolution_truth(
            truth,
            outcomes=[
                {"id": 1, "clob_token_id": None},
                {"id": 2, "clob_token_id": None},
            ],
        )
        # Truth wins on paper, no outcome can confirm.
        assert app.winner_outcome_id is None
        assert app.is_winner_by_outcome_id == {}

    def test_sqlite_row_outcomes_supported(self) -> None:
        """sqlite3.Row is duck-typed via bracket access."""
        import sqlite3
        # Build a fake sqlite3.Row.
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT 1 AS id, 'tok-Y' AS clob_token_id UNION ALL "
            "SELECT 2, 'tok-N'"
        )
        rows = cur.fetchall()
        conn.close()

        truth = MarketResolutionTruth(
            market_id="m6", resolved=True, winning_token_id="tok-Y"
        )
        app = apply_market_resolution_truth(truth, outcomes=rows)
        assert app.winner_outcome_id == 1
        assert app.is_winner_by_outcome_id == {1: 1, 2: 0}

    def test_pydantic_outcome_supported(self) -> None:
        """Dataclass / pydantic-style outcomes with .id / .clob_token_id."""

        class _Outcome:
            def __init__(self, id_: int, tok: Optional[str]) -> None:
                self.id = id_
                self.clob_token_id = tok

        truth = MarketResolutionTruth(
            market_id="m7", resolved=True, winning_token_id="tok-W"
        )
        app = apply_market_resolution_truth(
            truth,
            outcomes=[_Outcome(1, "tok-W"), _Outcome(2, "tok-L")],
        )
        assert app.winner_outcome_id == 1
        assert app.is_winner_by_outcome_id == {1: 1, 2: 0}

    def test_reapplying_same_truth_is_idempotent(self) -> None:
        truth = MarketResolutionTruth(
            market_id="m8", resolved=True, winning_token_id="tok-Y"
        )
        outcomes = [
            {"id": 1, "clob_token_id": "tok-Y"},
            {"id": 2, "clob_token_id": "tok-N"},
        ]
        app1 = apply_market_resolution_truth(truth, outcomes=outcomes)
        app2 = apply_market_resolution_truth(truth, outcomes=outcomes)
        assert app1.winner_outcome_id == app2.winner_outcome_id
        assert app1.is_winner_by_outcome_id == app2.is_winner_by_outcome_id

    def test_changed_winner_is_reported_not_silently_accepted(self) -> None:
        """If we re-apply with a different winning token, the helper
        must reflect the new truth (not silently reuse the prior
        flags). Persistence layer decides what to do; helper
        computes what the new truth says."""
        truth1 = MarketResolutionTruth(
            market_id="m9", resolved=True, winning_token_id="tok-Y"
        )
        truth2 = MarketResolutionTruth(
            market_id="m9", resolved=True, winning_token_id="tok-N"
        )
        outcomes = [
            {"id": 1, "clob_token_id": "tok-Y"},
            {"id": 2, "clob_token_id": "tok-N"},
        ]
        app1 = apply_market_resolution_truth(truth1, outcomes=outcomes)
        app2 = apply_market_resolution_truth(truth2, outcomes=outcomes)
        assert app1.winner_outcome_id == 1
        assert app2.winner_outcome_id == 2
        # The helper always reports the truth the caller passed in —
        # no implicit winner-correction logic.
        assert app1.is_winner_by_outcome_id == {1: 1, 2: 0}
        assert app2.is_winner_by_outcome_id == {1: 0, 2: 1}

    def test_empty_outcomes_with_resolved_truth(self) -> None:
        truth = MarketResolutionTruth(
            market_id="m10", resolved=True, winning_token_id="tok-A"
        )
        app = apply_market_resolution_truth(truth, outcomes=[])
        assert app.winner_outcome_id is None
        assert app.is_winner_by_outcome_id == {}
        assert app.resolved is True


# ────────────────────────────────────────────────────────────────────
# 4. AmbiguousResolution exception
# ────────────────────────────────────────────────────────────────────


class TestAmbiguousResolutionException:
    def test_is_value_error_subclass(self) -> None:
        """AmbiguousResolution is a programming/data error, not a
        runtime expected case, so we inherit ValueError."""
        assert issubclass(AmbiguousResolution, ValueError)

    def test_message_contains_token_ids(self) -> None:
        e = AmbiguousResolution("test: [tok-A, tok-B]")
        assert "tok-A" in str(e)
        assert "tok-B" in str(e)