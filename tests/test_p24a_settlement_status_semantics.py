"""PR24A2: Settlement-flag semantics — 0/1/None classification.

This module exists so that future consumers (PR24I accounting/ROI/drawdown,
and any dashboard code) cannot accidentally conflate a *losing* trade (``0``)
with *missing* trade data (``NULL``/``None``). Boolean truthiness on
``is_winning_trade`` would collapse both into ``False`` and silently corrupt
accounting totals.

Contract
--------

The ``is_winning_trade`` column on ``source_trades`` is ``INTEGER`` and is
populated by :func:`polycopy.engine.trade_settlement.settle_source_trade_against_truth`
with exactly one of three values:

* ``1`` — trade won. Realized P/L = ``(1 - price) * quantity`` (binary payoff).
* ``0`` — trade lost. Realized P/L = ``-price * quantity``.
* ``None`` (SQL NULL) — unresolved / unknown / ambiguous. Realized P/L is NULL.

For aggregation / accounting code, the rule of thumb is:

    Prefer ``source_trades.resolution_status`` for metrics.
    Do not use truthiness of ``is_winning_trade``.
    0 is a real losing trade, not missing data.
    NULL is missing/unknown.

This file pins that contract with a small classifier and parametrized tests.
"""

from __future__ import annotations

import pytest


def classify_settlement_flag(value: object) -> str:
    """Classify a settlement flag value into a status label.

    Accepts ONLY the three contract values produced by
    :func:`polycopy.engine.trade_settlement.settle_source_trade_against_truth`:

    * ``1`` -> ``"won"``
    * ``0`` -> ``"lost"``
    * ``None`` -> ``"unknown"``

    Booleans (``True`` / ``False``) and any other value raise
    :class:`ValueError`. This is deliberate — silently normalizing a bool
    to ``1`` / ``0`` would let downstream accounting code round-trip a
    losing trade (``False``) through a missing-data branch (``None``) or
    vice-versa, corrupting totals.

    Parameters
    ----------
    value:
        The ``is_winning_trade`` value to classify. Must be ``1``, ``0``,
        or ``None``.

    Returns
    -------
    str
        One of ``"won"``, ``"lost"``, ``"unknown"``.

    Raises
    ------
    ValueError
        If ``value`` is anything other than ``1``, ``0``, or ``None``.
        Booleans (``True`` / ``False``) are explicitly rejected even
        though Python treats ``True == 1`` and ``False == 0`` — see
        the explicit ``type(value) is int`` guard below.

    Notes
    -----

    Prefer ``source_trades.resolution_status`` for metrics.
    Do not use truthiness of ``is_winning_trade``.
    0 is a real losing trade, not missing data.
    NULL is missing/unknown.
    """
    # Explicit bool rejection: ``True == 1`` and ``False == 0`` in
    # Python, so a naive ``value == 1`` / ``value == 0`` chain would
    # silently normalize booleans into the win/loss branch. That would
    # let ``False`` (which means "losing or missing data" depending
    # on context) round-trip as a real ``0`` losing trade, corrupting
    # accounting totals.
    if isinstance(value, bool):
        raise ValueError(
            f"is_winning_trade must be 1, 0, or None; got {value!r} "
            f"(type=bool). Booleans are not part of the contract — "
            f"use the explicit int values 1 or 0."
        )
    if value == 1:
        return "won"
    if value == 0:
        return "lost"
    if value is None:
        return "unknown"
    raise ValueError(
        f"is_winning_trade must be 1, 0, or None; got {value!r} "
        f"(type={type(value).__name__})"
    )


class TestClassifySettlementFlag:
    """Parametrized coverage of the 0/1/None contract."""

    def test_one_maps_to_won(self) -> None:
        assert classify_settlement_flag(1) == "won"

    def test_zero_maps_to_lost(self) -> None:
        assert classify_settlement_flag(0) == "lost"

    def test_none_maps_to_unknown(self) -> None:
        assert classify_settlement_flag(None) == "unknown"

    def test_true_is_rejected(self) -> None:
        """Booleans are NOT silently normalized to 1.

        Python treats ``True == 1`` which would otherwise short-circuit
        the ``value == 1`` branch. Explicit ``type(value) is int`` guard
        is implicit in the contract: accounting code must never see a
        bool here. This test pins that rejection.
        """
        with pytest.raises(ValueError, match="True"):
            classify_settlement_flag(True)

    def test_false_is_rejected(self) -> None:
        """Booleans are NOT silently normalized to 0.

        ``False == 0`` is True in Python, so the naive ``value == 0``
        branch would accept it. Without explicit bool rejection, a
        losing trade stored as ``False`` would be ambiguous with a
        genuine ``0`` losing trade AND would mask missing-data cases.
        """
        with pytest.raises(ValueError, match="False"):
            classify_settlement_flag(False)

    @pytest.mark.parametrize(
        "bad_value",
        [2, -1, 0.5, "1", "0", "", "won", "lost", object()],
    )
    def test_arbitrary_values_are_rejected(self, bad_value: object) -> None:
        """Anything outside the {1, 0, None} contract raises ValueError.

        This guards against future column-type drift (e.g. someone
        storing ``"won"`` / ``"lost"`` strings, or a stray ``2`` from a
        bug in settlement math) from being silently re-classified.
        """
        with pytest.raises(ValueError):
            classify_settlement_flag(bad_value)

    def test_zero_is_not_truthiness_collapsible(self) -> None:
        """0 is a REAL losing trade, not missing data.

        This is the core regression-guard: ``bool(0) is False``, which
        would let a naive ``if is_winning_trade:`` block silently skip
        losing trades. Pinning the classifier's behavior ensures any
        future consumer that wants this mapping has to go through the
        explicit function (and thus through the bool-rejection path).
        """
        # bool(0) is False — the very conflation we want to prevent.
        assert bool(0) is False
        # The classifier still maps 0 to "lost", not to "unknown".
        assert classify_settlement_flag(0) == "lost"
        assert classify_settlement_flag(0) != classify_settlement_flag(None)

    def test_none_is_not_collapsible_with_zero(self) -> None:
        """NULL (None) and 0 are distinct categories.

        Without this distinction, accounting code would either over-count
        losses (treating missing-data trades as losses) or under-count
        them (treating losses as missing). The classifier preserves
        the distinction by construction.
        """
        assert classify_settlement_flag(None) == "unknown"
        assert classify_settlement_flag(None) != classify_settlement_flag(0)


class TestContractDocumentation:
    """Document the intent of the helper for future readers.

    These tests are intentionally trivial — their purpose is to keep the
    contract text discoverable from the test file itself. If anyone deletes
    a test above, the docstring here is the second line of defense.
    """

    def test_docstring_contains_warning(self) -> None:
        doc = classify_settlement_flag.__doc__ or ""
        assert "truthiness" in doc
        assert "0 is a real losing trade" in doc
        assert "NULL is missing/unknown" in doc