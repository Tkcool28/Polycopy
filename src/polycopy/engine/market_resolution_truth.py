"""Pure resolution-truth helpers (PR24A).

This module owns the *normalization* logic that turns a raw resolution
payload (from a ``ResolutionProvider`` or any internal caller) into a
single, unambiguous :class:`MarketResolutionTruth` record, and the
*derivation* logic that maps a truth record to ``is_winner`` flags on
every ``market_outcomes`` row.

Three responsibilities, all pure / testable:

1. :func:`normalize_resolution_payload` — coerce a heterogeneous
   upstream payload into a :class:`MarketResolutionTruth`. Never
   invents winners from text. Surfaces ambiguity as ambiguity.

2. :func:`derive_winner_from_market_payload` — given a dict-shaped
   market payload (typically what ``PolymarketPublicAdapter`` returns
   from a Gamma or CLOB resolution check), produce the truth record.
   Exactly one winner when the payload is unambiguous; zero winners
   when unresolved; raises :class:`AmbiguousResolution` when two or
   more winning tokens are claimed.

3. :func:`apply_market_resolution_truth` — given a
   :class:`MarketResolutionTruth` and the current list of
   ``market_outcomes`` rows for that market, return a deterministic
   ``(winner_outcome_id | None, is_winner_by_outcome_id)`` mapping
   suitable for the persistence layer to write back. This is also pure:
   it returns a mapping, not a side-effect.

Why this lives in its own module
================================

* Keeps the persistence layer's job narrow (turn a truth + mapping into
  SQL UPDATE/INSERT statements).
* Lets the settlement helper (:mod:`trade_resolution`) consume a
  resolved truth without re-deriving it from a payload.
* Lets tests exercise every branch (unresolved, exactly-one-winner,
  no-winner, ambiguous, changed-winner) without a database.

Ambiguity contract
==================

An ambiguous truth is NOT collapsed to a winner. ``apply_market_resolution_truth``
will return ``winner_outcome_id=None`` and an empty mapping when the truth
is unresolved OR ambiguous; the persistence layer is then responsible for
NOT writing a fake winner. We deliberately never silently pick one row.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional


class AmbiguousResolution(ValueError):
    """Raised when a resolved payload claims more than one winning token.

    This is a programming error / data integrity issue, not a runtime
    expected case. The caller should log it with the payload and the
    market id so an operator can inspect the upstream.
    """


@dataclass(frozen=True)
class MarketResolutionTruth:
    """Durable, normalized resolution truth for one market.

    Fields
    ------

    * ``market_id`` — internal market id (UUID).
    * ``resolved`` — True iff the truth record is a complete,
      unambiguous, single-winner claim. False if the market is
      unresolved, the winner is unknown, or the truth is ambiguous.
    * ``winning_token_id`` — token id of the winning outcome when
      ``resolved`` is True. ``None`` otherwise (including for the
      ambiguous case — we do not pick arbitrarily).
    * ``winning_outcome_id`` — internal ``market_outcomes.id`` of the
      winning outcome, when known. Optional; callers that do not yet
      have a joined ``market_outcomes`` row (e.g. a fresh
      resolution-check before the next ingest round) can leave it
      ``None`` and let the persistence layer resolve it.
    * ``resolution_outcome`` — human-readable label of the winning
      outcome (e.g. ``"Yes"`` / ``"Donald Trump"``). Optional; some
      upstream sources do not emit a label.
    * ``source`` — provenance tag (e.g. ``"polymarket_gamma"``,
      ``"clob"``, ``"manual_test_fixture"``). Used to populate
      ``markets.resolution_source``.
    * ``checked_at`` — ISO-8601 UTC timestamp of when the truth was
      captured. Used to populate ``markets.resolution_checked_at``.

    Constructing a truth record is the caller's job; this dataclass
    only documents the contract.
    """

    market_id: str
    resolved: bool
    winning_token_id: Optional[str]
    winning_outcome_id: Optional[int] = None
    resolution_outcome: Optional[str] = None
    source: Optional[str] = None
    checked_at: Optional[str] = None

    @property
    def is_unknown(self) -> bool:
        """True iff we explicitly do not know the winner.

        Distinguishes "the source said no winner" (this) from "we
        haven't checked yet" (also this) from "ambiguous" (also this).
        All three collapse to ``resolved=False`` plus ``winning_token_id=None``.
        """
        return not self.resolved or self.winning_token_id is None


# ── normalize_resolution_payload ──────────────────────────────────────────────


def _coerce_token_id(value: Any) -> Optional[str]:
    """Coerce an upstream token-id field into a canonical ``str | None``.

    Empty strings, whitespace-only, ``None``, and ``0`` collapse to
    ``None``. Everything else is stringified and stripped; we do NOT
    validate the hex format here — that's an upstream contract, not
    ours. Returning a non-None value with a weird shape lets the
    downstream equality check fail loudly rather than silently
    matching nothing.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # bool is a subclass of int; we want string coercion, so refuse.
        return None
    if isinstance(value, (int, float)):
        if value == 0:
            return None
        return str(value)
    try:
        s = str(value).strip()
    except Exception:
        return None
    return s or None


def normalize_resolution_payload(
    *,
    market_id: str,
    payload: Mapping[str, Any],
    source: str,
    checked_at: Optional[str] = None,
) -> MarketResolutionTruth:
    """Coerce a heterogeneous resolution-check payload into a truth record.

    Recognized payload shapes (case-insensitive field names; the
    function is forgiving about which upstream produced the payload):

    * ``{"resolved": True, "winning_token_id": "...", "resolution_outcome": "..."}``
    * ``{"closed": True, "resolved": True, "winningTokenId": "..."}``
    * ``{"winner": {"token_id": "..."}}`` — nested winner object.
    * ``{"outcomes": [{"id": 1, "winner": True}, ...]}`` — per-outcome
      winner flag (only used when the winner is unambiguous on the
      payload itself).

    Returns
    -------

    * ``resolved=True, winning_token_id="..."`` — exactly one
      unambiguous winner was found.
    * ``resolved=False, winning_token_id=None`` — no winner claim in
      the payload, or the payload says the market is still open /
      unresolved / unknown.
    * Raises :class:`AmbiguousResolution` if the payload names two or
      more distinct winning tokens (data integrity issue — log it).

    The function never invents a winner from text. If the payload does
    not explicitly identify a winning token, the truth is unresolved.
    """
    if not market_id:
        raise ValueError("market_id must be a non-empty string")

    # Case-insensitive scan for the canonical fields.
    lower: dict[str, Any] = {str(k).lower(): v for k, v in payload.items()}

    # If the upstream explicitly says resolved=False / closed=False,
    # short-circuit to unresolved.
    if lower.get("resolved") is False or lower.get("closed") is False:
        return MarketResolutionTruth(
            market_id=market_id,
            resolved=False,
            winning_token_id=None,
            source=source,
            checked_at=checked_at,
        )

    candidates: list[str] = []
    nested_winner = lower.get("winner")
    if isinstance(nested_winner, Mapping):
        w = _coerce_token_id(
            nested_winner.get("token_id")
            or nested_winner.get("tokenid")
            or nested_winner.get("clob_token_id")
        )
        if w is not None:
            candidates.append(w)

    direct = _coerce_token_id(
        lower.get("winning_token_id")
        or lower.get("winningtokenid")
        or lower.get("winner_token_id")
        or lower.get("winner_tokenid")
    )
    if direct is not None:
        candidates.append(direct)

    # Per-outcome winner flags (each outcome marked True).
    nested_outcomes = lower.get("outcomes")
    if isinstance(nested_outcomes, Iterable) and not isinstance(nested_outcomes, (str, bytes)):
        for outcome in nested_outcomes:
            if not isinstance(outcome, Mapping):
                continue
            if outcome.get("winner") is True:
                t = _coerce_token_id(
                    outcome.get("clob_token_id")
                    or outcome.get("token_id")
                    or outcome.get("tokenid")
                )
                if t is not None:
                    candidates.append(t)

    # De-dup, preserving order, while detecting ambiguity.
    seen: set[str] = set()
    deduped: list[str] = []
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        deduped.append(c)

    if len(deduped) == 0:
        # Either the market is genuinely unresolved or the payload
        # didn't carry a winner claim. We do not invent one.
        nested_label = None
        if isinstance(nested_winner, Mapping):
            nested_label = _safe_str(
                nested_winner.get("label")
                or nested_winner.get("resolution_outcome")
            )
        return MarketResolutionTruth(
            market_id=market_id,
            resolved=False,
            winning_token_id=None,
            resolution_outcome=_safe_str(lower.get("resolution_outcome")) or nested_label,
            source=source,
            checked_at=checked_at,
        )

    if len(deduped) > 1:
        # Surface every candidate so the caller can audit. Do not
        # arbitrarily pick one.
        raise AmbiguousResolution(
            f"market {market_id} payload claims {len(deduped)} distinct "
            f"winning tokens: {deduped!r}"
        )

    nested_label = None
    if isinstance(nested_winner, Mapping):
        nested_label = _safe_str(
            nested_winner.get("label")
            or nested_winner.get("resolution_outcome")
        )
    return MarketResolutionTruth(
        market_id=market_id,
        resolved=True,
        winning_token_id=deduped[0],
        resolution_outcome=_safe_str(lower.get("resolution_outcome")) or nested_label,
        source=source,
        checked_at=checked_at,
    )


def _safe_str(value: Any) -> Optional[str]:
    """Best-effort string coercion for label fields; None on unusable input."""
    if value is None:
        return None
    try:
        s = str(value).strip()
    except Exception:
        return None
    return s or None


# ── derive_winner_from_market_payload ─────────────────────────────────────────


def derive_winner_from_market_payload(
    *,
    market_id: str,
    market: Mapping[str, Any],
    source: str,
    checked_at: Optional[str] = None,
) -> MarketResolutionTruth:
    """Derive a truth record from a market-shaped payload.

    The :class:`ResolutionProvider` contract says
    :meth:`check_resolution` returns either ``None`` (still open) or
    a :class:`Market` whose ``resolved`` flag is set. We accept either
    a ``Market`` dataclass or a plain dict (e.g. from a JSON payload
    persisted into ``raw_snapshots``).

    The market's ``resolution_outcome`` label is consulted ONLY to
    record the human-readable outcome; the actual winner token is
    selected from ``market.outcomes[*].clob_token_id`` by matching
    the label exactly (case-sensitive, trimmed) — never via fuzzy
    matching.

    If the market has no ``outcomes`` list, the truth is unresolved
    (we cannot pick a token without an outcomes table to match
    against).
    """
    if not market_id:
        raise ValueError("market_id must be a non-empty string")

    resolved_flag = bool(market.get("resolved")) if hasattr(market, "get") else bool(getattr(market, "resolved", False))
    if not resolved_flag:
        return MarketResolutionTruth(
            market_id=market_id,
            resolved=False,
            winning_token_id=None,
            resolution_outcome=None,
            source=source,
            checked_at=checked_at,
        )

    label = _safe_str(market.get("resolution_outcome") if hasattr(market, "get") else getattr(market, "resolution_outcome", None))
    raw_outcomes = market.get("outcomes") if hasattr(market, "get") else getattr(market, "outcomes", None)

    if not isinstance(raw_outcomes, Iterable) or isinstance(raw_outcomes, (str, bytes)):
        return MarketResolutionTruth(
            market_id=market_id,
            resolved=False,
            winning_token_id=None,
            resolution_outcome=label,
            source=source,
            checked_at=checked_at,
        )

    # Find the outcome(s) whose label matches the declared winner label.
    # If the upstream gave us a label but no matching outcome has that
    # label, the truth is incomplete (no winner) — we do NOT fall back
    # to positional / first-row heuristics.
    #
    # Each outcome can be a Mapping, sqlite3.Row, or any object that
    # exposes ``label`` and ``clob_token_id`` attributes. We skip an
    # outcome only if we cannot access either form.
    matched_token_ids: list[str] = []
    if label is not None:
        for outcome in raw_outcomes:
            if isinstance(outcome, Mapping):
                outcome_label = outcome.get("label")
                tok = outcome.get("clob_token_id")
            elif isinstance(outcome, sqlite3.Row):
                try:
                    outcome_label = outcome["label"]
                except (IndexError, KeyError):
                    outcome_label = None
                try:
                    tok = outcome["clob_token_id"]
                except (IndexError, KeyError):
                    tok = None
            else:
                if not hasattr(outcome, "label") and not hasattr(outcome, "clob_token_id"):
                    continue
                outcome_label = getattr(outcome, "label", None)
                tok = getattr(outcome, "clob_token_id", None)
            outcome_label = _safe_str(outcome_label)
            if outcome_label == label:
                tok = _coerce_token_id(tok)
                if tok is not None:
                    matched_token_ids.append(tok)

    if len(matched_token_ids) == 0:
        # The upstream claims resolution but we can't identify a winner
        # token from the outcomes we have. This is incomplete / unknown,
        # not a reason to invent one.
        return MarketResolutionTruth(
            market_id=market_id,
            resolved=False,
            winning_token_id=None,
            resolution_outcome=label,
            source=source,
            checked_at=checked_at,
        )

    if len(matched_token_ids) > 1:
        raise AmbiguousResolution(
            f"market {market_id} label {label!r} matches {len(matched_token_ids)} "
            f"distinct outcomes (token ids: {matched_token_ids!r})"
        )

    return MarketResolutionTruth(
        market_id=market_id,
        resolved=True,
        winning_token_id=matched_token_ids[0],
        resolution_outcome=label,
        source=source,
        checked_at=checked_at,
    )


# ── apply_market_resolution_truth ─────────────────────────────────────────────


@dataclass(frozen=True)
class MarketTruthApplication:
    """The result of applying a truth record to a market's outcomes.

    * ``winner_outcome_id`` — internal ``market_outcomes.id`` of the
      winning outcome, or ``None`` if the truth is unresolved or
      ambiguous.
    * ``is_winner_by_outcome_id`` — mapping of every ``market_outcomes.id``
      under this market to its ``is_winner`` flag. 1=won, 0=lost,
      None=not in this mapping (will be passed through by the
      persistence layer).
    * ``resolved`` — mirrored from the truth for the persistence
      layer to write to ``markets.resolved``.
    * ``ambiguous`` — True iff the truth record claimed a winner but
      multiple outcomes had matching token ids (race / data
      integrity issue). The persistence layer should NOT mark a
      winner in this case.
    """

    winner_outcome_id: Optional[int]
    is_winner_by_outcome_id: dict[int, Optional[int]]
    resolved: bool
    ambiguous: bool = False


def apply_market_resolution_truth(
    truth: MarketResolutionTruth,
    *,
    outcomes: Iterable[Any],
) -> MarketTruthApplication:
    """Map a truth record to per-outcome ``is_winner`` flags.

    Parameters
    ----------

    * ``truth`` — the normalized truth record.
    * ``outcomes`` — iterable of ``market_outcomes`` rows for this
      market. Each row is expected to expose ``id`` (int) and
      ``clob_token_id`` (str | None). Plain dicts, dataclasses, and
      sqlite3.Row are all supported via duck typing.

    Returns
    -------

    A :class:`MarketTruthApplication` describing what the persistence
    layer should write. The function never writes; callers (and
    tests) inspect the mapping and then either ``UPDATE`` the DB or
    discard the application.

    Rules
    -----

    * Unresolved truth → ``winner_outcome_id=None``; no row gets
      ``is_winner=1``; ``is_winner_by_outcome_id`` is empty (caller
      preserves existing flags). ``resolved`` mirrors the truth.
    * Exactly one outcome's ``clob_token_id`` equals
      ``truth.winning_token_id`` → that outcome gets ``is_winner=1``;
      every other outcome with a non-NULL ``clob_token_id`` gets
      ``is_winner=0``; outcomes with ``clob_token_id IS NULL`` are
      omitted (we cannot conclusively say they lost).
    * No outcome matches the winning token → truth wins on paper but
      the outcomes we have cannot be marked. We surface this as
      ``winner_outcome_id=None`` and ``ambiguous=False`` — the
      caller (persistence layer) must NOT mark a winner in this case;
      it should still record ``markets.resolution_checked_at`` and
      ``markets.resolution_source``.
    * Two or more outcomes share the winning token id → ambiguous;
      ``winner_outcome_id=None``, ``ambiguous=True``.
    """
    # Collect (id, clob_token_id) pairs. sqlite3.Row, dict, and
    # dataclass are all supported.
    #
    # NOTE: sqlite3.Row does NOT inherit from Mapping; it only supports
    # ``row['col']`` / ``row[col_index]``. We try Mapping first (covers
    # plain dict and ``dataclasses.asdict``), then Row's bracket form
    # (covers sqlite3.Row), then attribute access (covers pydantic
    # models and dataclass instances). Any of the three is fine; the
    # function is intentionally lenient.
    def _coerce_pair(row: Any) -> Optional[tuple[int, Optional[str]]]:
        rid: Any = None
        tok: Any = None
        if isinstance(row, Mapping):
            rid = row.get("id")
            tok = row.get("clob_token_id")
        elif isinstance(row, sqlite3.Row):
            try:
                rid = row["id"]
            except (IndexError, KeyError):
                rid = None
            try:
                tok = row["clob_token_id"]
            except (IndexError, KeyError):
                tok = None
        else:
            rid = getattr(row, "id", None)
            tok = getattr(row, "clob_token_id", None)
        if rid is None:
            return None
        if tok in (None, ""):
            return (int(rid), None)
        return (int(rid), str(tok))

    pairs: list[tuple[int, Optional[str]]] = []
    for row in outcomes:
        pair = _coerce_pair(row)
        if pair is not None:
            pairs.append(pair)

    if not truth.resolved or truth.winning_token_id is None:
        return MarketTruthApplication(
            winner_outcome_id=None,
            is_winner_by_outcome_id={},
            resolved=False,
            ambiguous=False,
        )

    winning_token = truth.winning_token_id
    matched: list[int] = [rid for rid, tok in pairs if tok == winning_token]

    if len(matched) == 0:
        # Truth says we have a winner but no outcome's token matches.
        # We do not mark anything; the persistence layer treats this
        # as "checked but unverifiable".
        return MarketTruthApplication(
            winner_outcome_id=None,
            is_winner_by_outcome_id={},
            resolved=True,
            ambiguous=False,
        )

    if len(matched) > 1:
        # Same token on multiple outcome rows = data integrity issue.
        # Do not mark a winner.
        return MarketTruthApplication(
            winner_outcome_id=None,
            is_winner_by_outcome_id={},
            resolved=True,
            ambiguous=True,
        )

    winner_id = matched[0]
    flags: dict[int, Optional[int]] = {}
    for rid, tok in pairs:
        if rid == winner_id:
            flags[rid] = 1
        elif tok is not None and tok != "":
            flags[rid] = 0
        # Outcomes with NULL clob_token_id are omitted from the
        # mapping; the persistence layer's UPDATE should only touch
        # rows present in the mapping.

    return MarketTruthApplication(
        winner_outcome_id=winner_id,
        is_winner_by_outcome_id=flags,
        resolved=True,
        ambiguous=False,
    )


__all__ = [
    "AmbiguousResolution",
    "MarketResolutionTruth",
    "MarketTruthApplication",
    "normalize_resolution_payload",
    "derive_winner_from_market_payload",
    "apply_market_resolution_truth",
]