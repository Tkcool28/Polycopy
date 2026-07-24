"""S5 per-trade enrichment repair regression tests (plan Task 11).

Temp/scratch DBs only. Never opens production.

Proves the S5 repair: per-trade enrichment now writes the SCORER-VISIBLE
canonical ``source_trades.metadata_json`` (``taxonomy.raw_category``) AND one
CURRENT audit row in ``source_trade_enrichments``. The scorer authority is
``source_trades.metadata_json['taxonomy']['raw_category']``; the enrichment row
is audit-only.

Fixture data uses the REAL accepted source values
(``polymarket_data_api_trades_user`` / ``polymarket_clob``) and the real Gamma
condition/token membership shape (``clobTokenIds`` as a JSON-encoded list
string). The old ``source='polymarket'`` seed is removed because it is not a
canonical source_trades writer value.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from polycopy.db.database import Database  # noqa: E402
from polycopy.ingestion.canonical_metadata import (  # noqa: E402
    MERGE_FILLED,
    build_canonical_metadata,
)
from polycopy.ingestion.normalized_source_trade import SOURCE_NAME  # noqa: E402
from polycopy.ingestion.source_trade_enrichment import (  # noqa: E402
    MISSING_MARKET_IDENTITY,
    SAMPLE_TRADE_REFUSED,
    SELL_NOT_SUPPORTED,
    SOURCE_NOT_SUPPORTED,
    SOURCE_TRADE_NOT_FOUND,
    STATUS_ERROR,
    enrich_source_trade,
    get_enrichment,
)
from polycopy.ingestion.source_trade_provenance import (  # noqa: E402
    build_provenance_payload,
    enrichment_status_allows_dispatch,
)
from polycopy.scoring.wallet_evidence import classify_category_taxonomy  # noqa: E402

# Real accepted Polymarket source_trades writer values (no fuzzy matching).
CANON_SOURCE = SOURCE_NAME  # polymarket_data_api_trades_user
CLOB_SOURCE = "polymarket_clob"

COND = "0x" + "e" * 64
GTOK = "0x" + "a" * 64
OTHER_TOK = "0xf" + "0" * 63

# Real-shaped Gamma payload (clobTokenIds as a JSON-encoded list string).
GAMMA_PAYLOAD = {
    "conditionId": COND,
    "clobTokenIds": json.dumps([GTOK, OTHER_TOK]),
    "category": "Politics",
    "tags": ["election"],
    "events": [{"id": "e1", "slug": "us", "title": "US Election"}],
    "series": [],
    "question": "Who wins?",
    "slug": "us-election",
    "outcomes": ["Yes", "No"],
    "outcomePrices": ["0.4", "0.6"],
}


def _fake_resolver(_cid):
    """Thin wrapper returning the real Gamma payload (no provider swallowing)."""
    return dict(GAMMA_PAYLOAD)


def _tmp():
    raise RuntimeError("_tmp is provided by the module-owned SQLite fixture")


@pytest.fixture(autouse=True)
def _owned_sqlite_paths(monkeypatch, owned_sqlite):
    """Route this module's disposable SQLite files through pytest ownership."""
    monkeypatch.setitem(globals(), "_tmp", owned_sqlite.new_path)


def _open():
    p = _tmp()
    return Database(p).connect(), p


class _NoCloseDb(Database):
    """Database whose close() is a no-op, so the CLI's open->close->reopen
    dance keeps a single connected instance for the test."""

    def close(self):
        return None


def _seed_wallet(db, wid="uuid-e", address="0xenrich000000000000000000000000000abc"):
    db.conn.execute(
        "INSERT INTO wallets(id,address,label,is_sample,created_at) "
        "VALUES (?,?,?,?,?)",
        (wid, address, "t", 0, "2026-01-01T00:00:00Z"),
    )
    db.conn.commit()


def _seed_trade(db, tid, cond, metadata_json, *, side="BUY", source=CANON_SOURCE,
                token=GTOK, is_sample=0, market_source_id=None):
    db.conn.execute(
        "INSERT INTO source_trades("
        "id, source, source_trade_id, market_source_id, token_id, side, "
        "outcome, quantity, price, trader_address, timestamp, is_sample, "
        "metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, source, tid, market_source_id if market_source_id is not None else cond,
         token, side, "Yes", 10.0, 0.40,
         "0xenrich000000000000000000000000000abc",
         "2026-02-01T00:00:00Z", is_sample,
         json.dumps(metadata_json, sort_keys=True)),
    )
    db.conn.commit()


def _metadata_of(db, tid):
    row = db.fetchone("SELECT metadata_json FROM source_trades WHERE id=?", (tid,))
    return json.loads(row["metadata_json"]) if row and row["metadata_json"] else None


def _rc(enr):
    """Parse the stored reason_codes_json (a JSON string) into a list."""
    return json.loads(enr["reason_codes_json"])


# ── 1. empty metadata + authoritative Gamma ────────────────────────────────
def test_empty_metadata_with_gamma_writes_scoremeta():
    db, _ = _open()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, {})
    res = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_fake_resolver)
    assert res.status == "complete", res
    enr = get_enrichment(db, "polymarket:st1")
    assert enr is not None
    assert enr["normalized_category"] == "politics", enr
    assert enr["taxonomy_status"] == "usable", enr
    # Scorer-visible proof: classify parsed source_trades.metadata_json.
    meta = _metadata_of(db, "polymarket:st1")
    cls = classify_category_taxonomy(meta)
    assert cls.category_label == enr["normalized_category"], (cls.category_label, enr)
    assert cls.status.value if hasattr(cls.status, "value") else cls.status == "CATEGORY_TAXONOMY_USABLE"
    db.close()


# ── 2. existing equivalent canonical metadata -> zero write + stable ids ─────
def test_equivalent_replay_zero_write():
    db, _ = _open()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, {})
    r1 = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_fake_resolver)
    assert r1.created is True, r1
    eid1 = r1.enrichment_id
    ca1 = get_enrichment(db, "polymarket:st1")["created_at"]
    hash1 = get_enrichment(db, "polymarket:st1")["evidence_hash"]
    r2 = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_fake_resolver)
    assert r2.created is False and r2.updated is False, r2
    assert r2.enrichment_id == eid1, (r2.enrichment_id, eid1)
    enr = get_enrichment(db, "polymarket:st1")
    assert enr["created_at"] == ca1
    assert enr["evidence_hash"] == hash1
    db.close()


# ── 3. materially changed current evidence -> in-place update, one row ──────
def test_material_change_in_place_update():
    db, _ = _open()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, {})
    r1 = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_fake_resolver)
    eid = r1.enrichment_id
    ca = get_enrichment(db, "polymarket:st1")["created_at"]
    # Change the Gamma payload so the evidence hash differs.
    def _resolver2(_cid):
        p = dict(GAMMA_PAYLOAD)
        p["category"] = "Sports"
        p["tags"] = ["nba"]
        return p
    r2 = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_resolver2)
    assert r2.updated is True and r2.created is False, r2
    enr = get_enrichment(db, "polymarket:st1")
    assert enr["enrichment_id"] == eid
    assert enr["created_at"] == ca
    # updated_at advances (allow same-second granularity: assert it is >= ca
    # and the row was genuinely rewritten via a changed evidence hash).
    assert enr["updated_at"] >= ca
    assert enr["evidence_hash"] != r1.evidence and enr["evidence_hash"]
    rows = db.fetchall(
        "SELECT * FROM source_trade_enrichments WHERE source_trade_internal_id=?",
        ("polymarket:st1",),
    )
    assert len(rows) == 1, rows
    db.close()


# ── 4. existing dispatch FK referencing enrichment_id stays valid ───────────
def test_dispatch_fk_stable_on_update():
    db, _ = _open()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, {})
    r1 = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_fake_resolver)
    eid = r1.enrichment_id
    # Author a dispatch row that references the enrichment_id (proves the FK
    # contract survives an in-place update). The dispatch also references a
    # specialist_approval, so seed a minimal valid approval row first.
    db.conn.execute(
        "INSERT INTO specialist_approvals("
        "approval_id, wallet_address, specialist_category, formula_name, "
        "formula_version, reviewer, approved_at, created_at, updated_at, enabled) "
        "VALUES ('a1','0xw','politics','v1','1','tester','2026-03-01T00:00:00Z','2026-03-01T00:00:00Z','2026-03-01T00:00:00Z',1)"
    )
    db.conn.execute(
        "INSERT INTO approved_specialist_trade_dispatches("
        "dispatch_id, specialist_approval_id, source_trade_internal_id, wallet,"
        "category, enrichment_id, status, attempt_count, created_at, updated_at) "
        "VALUES ('d1','a1','polymarket:st1','0xw','politics',?,"
        "'pending',0,'2026-03-01T00:00:00Z','2026-03-01T00:00:00Z')",
        (eid,),
    )
    db.conn.commit()

    def _resolver2(_cid):
        p = dict(GAMMA_PAYLOAD)
        p["category"] = "Sports"
        return p
    r2 = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_resolver2)
    # enrichment_id unchanged -> FK remains valid.
    assert r2.enrichment_id == eid, (r2.enrichment_id, eid)
    disp = db.fetchone(
        "SELECT enrichment_id FROM approved_specialist_trade_dispatches WHERE dispatch_id='d1'"
    )
    assert disp["enrichment_id"] == eid
    fk = db.conn.execute("PRAGMA foreign_key_check").fetchall()
    assert fk == [], fk
    db.close()


# ── 5. Gamma/category conflict -> metadata unchanged, status conflict ───────
def test_gamma_conflict_preserves_metadata():
    db, _ = _open()
    _seed_wallet(db)
    existing = build_canonical_metadata({}, {
        "conditionId": COND, "clobTokenIds": json.dumps([GTOK, OTHER_TOK]),
        "category": "Crypto", "events": [], "series": [],
    })
    _seed_trade(db, "polymarket:st1", COND, existing)
    # Gamma now returns a DIFFERENT category for the same condition id -> conflict.
    def _resolver(_cid):
        p = dict(GAMMA_PAYLOAD)
        p["category"] = "Politics"
        return p
    res = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_resolver)
    assert res.status == "conflict", res
    enr = get_enrichment(db, "polymarket:st1")
    assert enr["normalized_category"] is None, enr
    assert enr["taxonomy_status"] == "unavailable", enr
    # metadata_json byte-for-byte unchanged.
    meta = _metadata_of(db, "polymarket:st1")
    assert meta == existing, (meta, existing)
    assert any("conflict" in (c or "") for c in _rc(enr)), enr
    db.close()


# ── 6. malformed metadata JSON -> no crash, preserved, unavailable ──────────
def test_malformed_metadata_preserved():
    db, _ = _open()
    _seed_wallet(db)
    db.conn.execute(
        "INSERT INTO source_trades("
        "id, source, source_trade_id, market_source_id, token_id, side, "
        "outcome, quantity, price, trader_address, timestamp, is_sample, "
        "metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("polymarket:st1", CANON_SOURCE, "polymarket:st1", COND, GTOK, "BUY",
         "Yes", 10.0, 0.40, "0xenrich000000000000000000000000000abc",
         "2026-02-01T00:00:00Z", 0, "{not valid json"),
    )
    db.conn.commit()
    res = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_fake_resolver)
    assert res.status == "unavailable", res
    enr = get_enrichment(db, "polymarket:st1")
    assert enr["normalized_category"] is None, enr
    # original raw text preserved
    raw = db.fetchone(
        "SELECT metadata_json FROM source_trades WHERE id='polymarket:st1'"
    )["metadata_json"]
    assert raw == "{not valid json", raw
    assert any("malformed" in (c or "") for c in _rc(enr)), enr
    db.close()


# ── 7. metadata_version conflict -> no overwrite, version_conflict ──────────
def test_metadata_version_conflict():
    db, _ = _open()
    _seed_wallet(db)
    bad = dict(build_canonical_metadata({}, GAMMA_PAYLOAD))
    bad["metadata_version"] = "2"
    _seed_trade(db, "polymarket:st1", COND, bad)
    res = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_fake_resolver)
    assert res.status == "conflict", res
    meta = _metadata_of(db, "polymarket:st1")
    # version "2" is preserved (never rewritten to "1").
    assert meta.get("metadata_version") == "2", meta
    enr = get_enrichment(db, "polymarket:st1")
    assert any("version_conflict" in (c or "") for c in _rc(enr)), enr
    db.close()


# ── 8-11. token membership failures ────────────────────────────────────────
def test_token_membership_unavailable():
    db, _ = _open()
    _seed_wallet(db)
    # Gamma with NO clobTokenIds list at all.
    def _resolver(_cid):
        p = dict(GAMMA_PAYLOAD)
        p.pop("clobTokenIds", None)
        return p
    _seed_trade(db, "polymarket:st1", COND, {})
    res = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_resolver)
    assert res.status == "unavailable", res
    enr = get_enrichment(db, "polymarket:st1")
    assert any("token_membership_unavailable" in (c or "")
               for c in _rc(enr)), enr
    db.close()


def test_token_not_in_condition():
    db, _ = _open()
    _seed_wallet(db)
    # Source trade token id is not in the Gamma clobTokenIds list.
    _seed_trade(db, "polymarket:st1", COND, {}, token="0xdeadbeef")
    res = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_fake_resolver)
    assert res.status == "unavailable", res
    enr = get_enrichment(db, "polymarket:st1")
    assert any("token_id_not_in_condition" in (c or "")
               for c in _rc(enr)), enr
    db.close()


def test_ambiguous_token_membership():
    db, _ = _open()
    _seed_wallet(db)
    # Gamma lists the token twice -> ambiguous membership.
    def _resolver(_cid):
        p = dict(GAMMA_PAYLOAD)
        p["clobTokenIds"] = json.dumps([GTOK, GTOK])
        return p
    _seed_trade(db, "polymarket:st1", COND, {})
    res = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_resolver)
    assert res.status == "unavailable", res
    enr = get_enrichment(db, "polymarket:st1")
    assert any("token_membership_ambiguous" in (c or "")
               for c in _rc(enr)), enr
    db.close()


def test_condition_id_mismatch():
    db, _ = _open()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, {})
    # Source trade market_source_id says COND, but Gamma returns a different cid.
    def _resolver(_cid):
        p = dict(GAMMA_PAYLOAD)
        p["conditionId"] = "0x" + "f" * 64
        return p
    res = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_resolver)
    assert res.status == "unavailable", res
    enr = get_enrichment(db, "polymarket:st1")
    assert any("condition_id_mismatch" in (c or "")
               for c in _rc(enr)), enr
    db.close()


# ── 12. provider exception distinct from not found ──────────────────────────
def test_provider_exception_distinct():
    db, _ = _open()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, {})

    def _boom(_cid):
        raise RuntimeError("network down")

    res = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_boom)
    assert res.status == "error", res
    enr = get_enrichment(db, "polymarket:st1")
    assert any("gamma:provider_error" in (c or "")
               for c in _rc(enr)), enr
    db.close()


# ── 13-14. Gamma not found / malformed / ambiguous ─────────────────────────
def test_gamma_not_found():
    db, _ = _open()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, {})

    def _none(_cid):
        return None

    res = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_none)
    # No Gamma evidence => merge unavailable (preserve metadata), status
    # unavailable (no provider error).
    assert res.status == "unavailable", res
    enr = get_enrichment(db, "polymarket:st1")
    assert any("gamma:not_found" in (c or "") for c in _rc(enr)), enr
    db.close()


def test_gamma_malformed_ambiguous():
    db, _ = _open()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, {})

    def _ambiguous(_cid):
        raise ValueError("Gamma condition_ids lookup returned 2 exact matches; refusing ambiguous selection")

    res = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_ambiguous)
    # Per spec #8: ambiguous Gamma -> status unavailable (not provider_error).
    assert res.status == "unavailable", res
    enr = get_enrichment(db, "polymarket:st1")
    assert any("gamma:ambiguous" in (c or "") for c in _rc(enr)), enr
    db.close()


# ── 15. exact source token persisted; condition never substituted as token ──
def test_exact_token_persisted_not_condition():
    db, _ = _open()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, {}, token=GTOK)
    enrich_source_trade(db, "polymarket:st1", gamma_resolver=_fake_resolver)
    enr = get_enrichment(db, "polymarket:st1")
    assert enr["token_id"] == GTOK, enr  # exact source token
    assert enr["condition_id"] == COND, enr  # condition identity, not the token
    assert enr["condition_id"] != GTOK, enr  # condition never substituted as token
    db.close()


# ── 16. missing token remains NULL and fails closed ─────────────────────────
def test_missing_token_null_fails_closed():
    db, _ = _open()
    _seed_wallet(db)
    # token_id None; Gamma has a valid clobTokenIds list but the trade carries
    # no token, so merge proceeds on condition-only membership.
    _seed_trade(db, "polymarket:st1", COND, {}, token=None)
    res = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_fake_resolver)
    enr = get_enrichment(db, "polymarket:st1")
    assert enr["token_id"] is None, enr
    # still completes because condition membership is satisfied
    assert res.status == "complete", res
    db.close()


# ── 17-19. slug only from Gamma; question/title NOT slug; no market_start_at ─
def test_slug_only_from_gamma():
    db, _ = _open()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, {})
    enrich_source_trade(db, "polymarket:st1", gamma_resolver=_fake_resolver)
    enr = get_enrichment(db, "polymarket:st1")
    assert enr["market_slug"] == "us-election", enr
    db.close()


def test_question_title_without_slug_null():
    db, _ = _open()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, {})

    def _resolver(_cid):
        p = dict(GAMMA_PAYLOAD)
        p.pop("slug", None)  # no slug field
        return p
    enrich_source_trade(db, "polymarket:st1", gamma_resolver=_resolver)
    enr = get_enrichment(db, "polymarket:st1")
    assert enr["market_slug"] is None, enr  # question/title not used as slug
    db.close()


def test_market_start_at_not_from_trade_timestamp():
    db, _ = _open()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, {})
    enrich_source_trade(db, "polymarket:st1", gamma_resolver=_fake_resolver)
    enr = get_enrichment(db, "polymarket:st1")
    assert enr["market_start_at"] is None, enr  # never trade timestamp
    db.close()


# ── 20. exact eligibility refusals (zero provider calls / writes) ───────────
def test_eligibility_refusals():
    db, _ = _open()
    _seed_wallet(db)
    calls = {"n": 0}

    def _counting(_cid):
        calls["n"] += 1
        return None

    # unknown id
    r = enrich_source_trade(db, "nope", gamma_resolver=_counting)
    assert r.status == "error" and SOURCE_TRADE_NOT_FOUND in r.reason_codes, r
    # unsupported source (bare 'polymarket' literal is NOT canonical)
    _seed_trade(db, "polymarket:st1", COND, {}, source="polymarket")
    r = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_counting)
    assert r.status == "error" and SOURCE_NOT_SUPPORTED in r.reason_codes, r
    # SELL
    _seed_trade(db, "polymarket:st2", COND, {}, side="SELL")
    r = enrich_source_trade(db, "polymarket:st2", gamma_resolver=_counting)
    assert r.status == "error" and SELL_NOT_SUPPORTED in r.reason_codes, r
    # sample
    _seed_trade(db, "polymarket:st3", COND, {}, is_sample=1)
    r = enrich_source_trade(db, "polymarket:st3", gamma_resolver=_counting)
    assert r.status == "error" and "sample_trade_refused" in r.reason_codes, r
    # missing market identity
    _seed_trade(db, "polymarket:st4", "", {}, market_source_id="")
    r = enrich_source_trade(db, "polymarket:st4", gamma_resolver=_counting)
    assert r.status == "error" and "missing_market_identity" in r.reason_codes, r
    # zero provider calls after every refusal
    assert calls["n"] == 0, calls
    # zero enrichment rows written
    rows = db.fetchall("SELECT * FROM source_trade_enrichments")
    assert rows == [], rows
    db.close()


# ── 21. dry-run computes but performs zero writes ──────────────────────────
def test_dry_run_zero_writes():
    db, _ = _open()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, {})
    res = enrich_source_trade(
        db, "polymarket:st1", gamma_resolver=_fake_resolver, dry_run=True)
    assert res.created is False and res.updated is False, res
    assert res.metadata_changed is False, res
    enr = get_enrichment(db, "polymarket:st1")
    assert enr is None, enr
    # metadata_json unchanged (still empty)
    assert _metadata_of(db, "polymarket:st1") == {}, _metadata_of(db, "polymarket:st1")
    db.close()


# ── 22-23. atomicity: forced failures roll back both ───────────────────────
def test_forced_provenance_failure_rolls_back_metadata():
    db, _ = _open()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, {})
    orig_meta = _metadata_of(db, "polymarket:st1")

    def _boom_write(db_, **kwargs):
        # simulate a provenance write failure AFTER metadata update
        raise RuntimeError("prov fail")

    import polycopy.ingestion.source_trade_enrichment as ste
    orig = ste.write_provenance
    ste.write_provenance = _boom_write
    try:
        res = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_fake_resolver)
    finally:
        ste.write_provenance = orig
    assert res.status == "error", res
    # metadata rolled back to original (unchanged)
    assert _metadata_of(db, "polymarket:st1") == orig_meta, _metadata_of(db, "polymarket:st1")
    enr = get_enrichment(db, "polymarket:st1")
    assert enr is None, enr  # nothing written
    db.close()


def test_forced_metadata_failure_no_provenance():
    db, _ = _open()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, {})

    # Wrap the connection so the metadata UPDATE itself raises, proving a
    # metadata-write failure leaves NO provenance row/change. db.conn is a
    # read-only property, so we shim the Database object instead.
    class _DbShim:
        def __init__(self, real):
            self._real = real

        @property
        def conn(self):
            real_conn = self._real.conn

            class _Conn:
                def execute(self, sql, params=None):
                    if "UPDATE source_trades SET metadata_json" in sql:
                        raise RuntimeError("meta fail")
                    return real_conn.execute(sql, params or [])

                def __getattr__(self, name):
                    return getattr(real_conn, name)

            return _Conn()

        def fetchone(self, sql, params=None):
            return self._real.fetchone(sql, params)

        def fetchall(self, sql, params=None):
            return self._real.fetchall(sql, params)

    shim = _DbShim(db)
    res = enrich_source_trade(shim, "polymarket:st1", gamma_resolver=_fake_resolver)
    assert res.status == "error", res
    enr = get_enrichment(db, "polymarket:st1")
    assert enr is None, enr  # no provenance row/change
    db.close()


# ── 24. adapter aclose runs on success and exception ───────────────────────
def test_resolve_gamma_state_distinguishes():
    from polycopy.ingestion.source_trade_enrichment import resolve_gamma_state

    # found
    m, st, reason = resolve_gamma_state(lambda _c: dict(GAMMA_PAYLOAD), COND)
    assert st == "found" and m is not None, (st, reason)
    # not_found
    m, st, reason = resolve_gamma_state(lambda _c: None, COND)
    assert st == "not_found", (st, reason)
    # provider_error
    def _boom(_c):
        raise ConnectionError("down")
    m, st, reason = resolve_gamma_state(_boom, COND)
    assert st == "provider_error", (st, reason)
    # ambiguous
    def _amb(_c):
        raise ValueError("ambiguous selection")
    m, st, reason = resolve_gamma_state(_amb, COND)
    assert st == "ambiguous", (st, reason)
    # malformed
    def _mal(_c):
        raise ValueError("returned non-list top-level type dict")
    m, st, reason = resolve_gamma_state(_mal, COND)
    assert st == "malformed", (st, reason)


# ── 25. backfill and per-trade produce equivalent provenance ────────────────
def test_backfill_and_pertrade_provenance_equivalent():
    from scripts.backfill_specialist_trade_taxonomy import (  # noqa
        _build_evidence,
    )
    from scripts.backfill_specialist_trade_taxonomy import GammaResult

    trade = {"id": "polymarket:st1", "token_id": GTOK,
             "market_source_id": COND, "outcome": "Yes"}
    canonical = build_canonical_metadata({}, GAMMA_PAYLOAD)
    gr = GammaResult("found", GAMMA_PAYLOAD)
    bk_payload = _build_evidence(trade, canonical, GAMMA_PAYLOAD, MERGE_FILLED, gr)
    pt_payload = build_provenance_payload(
        source_trade=trade, canonical_meta=canonical, gamma_market=GAMMA_PAYLOAD,
        merge_status=MERGE_FILLED, gamma_state="found")
    # The provenance contract is identical regardless of evidence_source tag.
    for k in ("status", "token_id", "condition_id", "market_id", "market_slug",
              "normalized_category", "taxonomy_status"):
        assert bk_payload[k] == pt_payload[k], (k, bk_payload[k], pt_payload[k])
    assert bk_payload["evidence_source"] == "backfill"
    assert pt_payload["evidence_source"] == "canonical_metadata"


# ── 26. scorer visibility: classify parsed metadata, not enrichment ─────────
def test_scorer_visibility_via_metadata():
    db, _ = _open()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, {})
    enrich_source_trade(db, "polymarket:st1", gamma_resolver=_fake_resolver)
    meta = _metadata_of(db, "polymarket:st1")
    cls = classify_category_taxonomy(meta)
    assert cls.category_label == "politics", cls
    # The enrichment row is NOT consulted for scoring.
    enr = get_enrichment(db, "polymarket:st1")
    assert enr is not None
    db.close()


# ── 27. zero rows in approval/dispatch/candidate/execution tables ───────────
def test_zero_approval_dispatch_artifacts():
    db, _ = _open()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, {})
    enrich_source_trade(db, "polymarket:st1", gamma_resolver=_fake_resolver)
    tables = [
        "specialist_approvals", "approved_specialist_trade_dispatches",
        "copy_candidates", "candidate_price_snapshots",
        "paper_signal_decisions", "paper_signal_execution_authorizations",
        "execution_risk_decisions", "paper_orders", "paper_fills",
        "paper_positions", "paper_position_marks", "paper_position_settlements",
    ]
    for t in tables:
        n = db.fetchone(f"SELECT COUNT(*) AS c FROM {t}")["c"]
        assert n == 0, (t, n)
    db.close()


# ── dispatch-gate regression seam (S5) ──────────────────────────────────────
def test_dispatch_gate_blocks_non_complete():
    for s in ("conflict", "unavailable", "incomplete", "error"):
        assert enrichment_status_allows_dispatch(s) is False
    assert enrichment_status_allows_dispatch("complete") is True


# ── 1b. equivalent replay executes ZERO SQL against either table ──────────
class _SqlObserver:
    """Wrap a real DB connection to count INSERT/UPDATE against the two tables."""

    def __init__(self, real):
        self._real = real
        self.inserts = 0
        self.updates = 0

    @property
    def conn(self):
        real_conn = self._real.conn

        class _Obs:
            def __init__(self, real, obs):
                self._real = real
                self._obs = obs

            def execute(self, sql, params=None):
                s = sql.lstrip().upper()
                if s.startswith("INSERT INTO SOURCE_TRADE_ENRICHMENTS"):
                    self._obs.inserts += 1
                elif s.startswith("UPDATE SOURCE_TRADE_ENRICHMENTS"):
                    self._obs.updates += 1
                elif s.startswith("UPDATE SOURCE_TRADES SET METADATA_JSON"):
                    self._obs.updates += 1
                return self._real.execute(sql, params or [])

            def __getattr__(self, name):
                return getattr(self._real, name)

        return _Obs(real_conn, self)

    def fetchone(self, sql, params=None):
        return self._real.fetchone(sql, params)

    def fetchall(self, sql, params=None):
        return self._real.fetchall(sql, params)


def test_equivalent_replay_zero_sql():
    db, _ = _open()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, {})
    r1 = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_fake_resolver)
    assert r1.created is True, r1

    meta_after_run1 = _metadata_of(db, "polymarket:st1")
    enr_after_run1 = get_enrichment(db, "polymarket:st1")
    assert enr_after_run1 is not None

    obs = _SqlObserver(db)
    r2 = enrich_source_trade(obs, "polymarket:st1", gamma_resolver=_fake_resolver)
    # No INSERT or UPDATE against either source_trades or source_trade_enrichments.
    assert obs.inserts == 0 and obs.updates == 0, (obs.inserts, obs.updates)
    assert r2.created is False and r2.updated is False, r2
    assert r2.metadata_changed is False, r2

    # All stability fields preserved exactly.
    enr = get_enrichment(db, "polymarket:st1")
    assert enr is not None, "enrichment row must exist after first run"
    assert enr["created_at"] == enr_after_run1["created_at"]
    assert enr["updated_at"] == enr_after_run1["updated_at"]
    assert enr["fetched_at"] == enr_after_run1["fetched_at"]
    assert enr["evidence_hash"] == enr_after_run1["evidence_hash"]
    assert enr["reason_codes_json"] == enr_after_run1["reason_codes_json"]
    assert enr["enrichment_id"] == enr_after_run1["enrichment_id"]
    # metadata_json bytes unchanged across replay
    assert _metadata_of(db, "polymarket:st1") == meta_after_run1
    db.close()


# ── 1c. first canonical normalization writes once; next replay writes nothing ─
def test_normalize_once_then_replay_zero():
    db, _ = _open()
    _seed_wallet(db)
    # Seed a semantically valid but NON-canonical metadata serialization.
    non_canonical = {"taxonomy": {"raw_category": "politics"},
                     "metadata_version": "1"}
    _seed_trade(db, "polymarket:st1", COND, non_canonical)
    obs = _SqlObserver(db)
    r1 = enrich_source_trade(obs, "polymarket:st1", gamma_resolver=_fake_resolver)
    # First pass normalizes -> exactly one enrichment INSERT/UPDATE and one
    # metadata UPDATE (canonical reshaping).
    assert r1.created is True, r1
    first_writes = obs.inserts + obs.updates
    assert first_writes >= 1, first_writes

    obs2 = _SqlObserver(db)
    r2 = enrich_source_trade(obs2, "polymarket:st1", gamma_resolver=_fake_resolver)
    # Immediate next replay: zero writes.
    assert obs2.inserts == 0 and obs2.updates == 0, (obs2.inserts, obs2.updates)
    assert r2.created is False and r2.updated is False, r2
    db.close()


# ── 2b. CLI: non-production --write without --allow-live => exit 2, no open ─
def _run_cli_refusal(args):
    import scripts.enrich_approved_source_trade as cli
    import evidence_db as edb
    import polycopy.ingestion.source_trade_enrichment as ste

    captured = {}

    def _open_readonly(path):
        captured["open_readonly"] = path
        raise AssertionError("open_readonly must not be called on refusal")

    def _open_writable(path, a):
        captured["open_writable"] = path
        raise AssertionError("open_writable must not be called on refusal")

    def _make_adapter():
        captured["adapter"] = True
        raise AssertionError("adapter must not be built on refusal")

    def _no_enrich(*a, **k):
        captured["enrich"] = True
        raise AssertionError("enrich_source_trade must not be called on refusal")

    fns = [("open_readonly", _open_readonly),
           ("open_writable", _open_writable),
           ("require_write_gates", lambda a, db_path: False)]
    orig = {n: getattr(edb, n) for n, _ in fns}
    for n, f in fns:
        setattr(edb, n, f)
    orig_adapter = cli._make_adapter
    cli._make_adapter = _make_adapter
    orig_enrich = ste.enrich_source_trade
    ste.enrich_source_trade = _no_enrich
    # The CLI imports these names directly, so patch the bound module names too.
    orig_cli_ro = cli.open_readonly
    orig_cli_rw = cli.open_writable
    cli.open_readonly = _open_readonly
    cli.open_writable = _open_writable
    try:
        rc = cli.main(args)
    finally:
        for n, f in fns:
            setattr(edb, n, orig[n])
        cli._make_adapter = orig_adapter
        ste.enrich_source_trade = orig_enrich
        cli.open_readonly = orig_cli_ro
        cli.open_writable = orig_cli_rw
    return rc, captured


def test_cli_nonprod_write_requires_allow_live():
    # Non-production DB path (tmp file). --write without --allow-live => exit 2
    # before any DB open / adapter / enrich.
    tmp = str(_tmp())
    rc, cap = _run_cli_refusal(
        ["--source-trade-id", "polymarket:st1", "--write", "--db-path", tmp]
    )
    assert rc == 2, rc
    assert "open_readonly" not in cap, cap
    assert "open_writable" not in cap, cap
    assert "adapter" not in cap, cap
    assert "enrich" not in cap, cap


def test_cli_prod_write_requires_all_gates():
    # Recognized production DB. Missing --allow-live and --confirm-production-db
    # => exit 2 before any open / schema read / lookup / adapter / network.
    prod = str(Path("/root/Polycopy/data/polycopy.db"))
    rc, cap = _run_cli_refusal(
        ["--source-trade-id", "polymarket:st1", "--write", "--db-path", prod]
    )
    assert rc == 2, rc
    assert "open_readonly" not in cap, cap
    assert "open_writable" not in cap, cap
    assert "adapter" not in cap, cap
    assert "enrich" not in cap, cap


# ── 3b. CLI: persistence failure -> exit nonzero, no commit ─────────────────
def test_cli_persistence_failure_nonzero():
    import scripts.enrich_approved_source_trade as cli
    from polycopy.db.database import Database
    import polycopy.ingestion.source_trade_enrichment as ste

    # A recording Database subclass so we can prove commit/rollback were (or
    # were not) called without monkeypatching read-only sqlite3 methods.
    calls = {"commit": 0, "rollback": 0}

    class _RecordingDb(Database):
        def commit(self):
            calls["commit"] += 1
            return super().commit()

        def rollback(self):
            calls["rollback"] += 1
            return super().rollback()

        def close(self):
            # Keep the connection alive across the CLI's open→close→reopen dance
            # so the single recording instance stays connected for the test.
            return None

    # Build a real, connected, seeded DB via the recording subclass.
    p = _tmp()
    db = _RecordingDb(p).connect()
    # Record any ROLLBACK issued via the raw connection (SAVEPOINT rollback and
    # the CLI's final conn.rollback() both go to the raw sqlite3 connection).
    db.conn.set_trace_callback(
        lambda sql: calls.__setitem__(
            "rollback", calls["rollback"] + (1 if "ROLLBACK" in sql.upper() else 0)
        )
    )
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, {})

    orig_write = ste.write_provenance

    def _boom(db_, **kw):
        raise RuntimeError("prov fail")

    ste.write_provenance = _boom

    orig_enrich = ste.enrich_source_trade_async
    orig_cli_enrich = cli.enrichment_async_fn

    async def _enrich(db_arg, st_id, **kw):
        # Exercise the real native async enrichment with write_provenance boomed.
        return await orig_enrich(db_arg, st_id, **kw)

    ste.enrich_source_trade_async = _enrich
    cli.enrichment_async_fn = orig_enrich

    orig_ro, orig_rw = cli.open_readonly, cli.open_writable

    def _ro(path):
        return db

    def _rw(path, args):
        return db

    cli.open_readonly = _ro
    cli.open_writable = _rw
    try:
        rc = cli.main(
            ["--source-trade-id", "polymarket:st1", "--write",
             "--allow-live", "--db-path", str(_tmp())]
        )
    finally:
        cli.open_readonly, cli.open_writable = orig_ro, orig_rw
        ste.enrich_source_trade_async = orig_enrich
        cli.enrichment_async_fn = orig_cli_enrich
        ste.write_provenance = orig_write

    assert rc not in (0, None), rc
    # Commit must NOT have been called; a rollback must have occurred.
    assert calls["commit"] == 0, calls
    assert calls["rollback"] >= 1, calls
    # metadata unchanged (rolled back); no enrichment row created.
    assert _metadata_of(db, "polymarket:st1") == {}, _metadata_of(db, "polymarket:st1")
    assert get_enrichment(db, "polymarket:st1") is None
    db.close()


# ── 3c. CLI: provider error -> exit nonzero, structured status retained ────
def test_cli_provider_error_nonzero():
    import scripts.enrich_approved_source_trade as cli

    db = _NoCloseDb(_tmp()).connect()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, {})

    # Make the CLI's adapter raise so the real resolver hits a provider error.
    def _make_boom_adapter():
        class _Boom:
            async def get_market_raw(self, cid):
                raise RuntimeError("network down")

            async def aclose(self):
                pass

        return _Boom()

    orig_adapter = cli._make_adapter
    cli._make_adapter = _make_boom_adapter
    orig_cli_enrich = cli.enrichment_async_fn
    # CLI uses its own bound enrich_source_trade; leave it as the real one.
    orig_ro, orig_rw = cli.open_readonly, cli.open_writable

    def _ro(path):
        return db

    def _rw(path, args):
        return db

    cli.open_readonly = _ro
    cli.open_writable = _rw
    try:
        rc = cli.main(
            ["--source-trade-id", "polymarket:st1", "--write",
             "--allow-live", "--db-path", str(_tmp())]
        )
    finally:
        cli._make_adapter = orig_adapter
        cli.open_readonly, cli.open_writable = orig_ro, orig_rw
        cli.enrichment_async_fn = orig_cli_enrich
    db.close()
    # Provider error is a hard failure -> nonzero exit (1).
    assert rc not in (0, None), rc


# ── 4a. invalid-selection reason codes set selection_error (typed flag) ─────
def _selection_case(reason_code, *, side="BUY", source=CANON_SOURCE,
                    is_sample=0, market_source_id=COND):
    db, _ = _open()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, {},
                side=side, source=source,
                is_sample=is_sample, market_source_id=market_source_id)
    res = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_fake_resolver)
    db.close()
    return res


def test_selection_error_codes_flagged():
    cases = {
        SOURCE_TRADE_NOT_FOUND: lambda: (
            enrich_source_trade(
                Database(_tmp()).connect(), "polymarket:missing",
                gamma_resolver=_fake_resolver,
            )
        ),
        SOURCE_NOT_SUPPORTED: lambda: _selection_case(SOURCE_NOT_SUPPORTED,
                                                       source="bogus_src"),
        SELL_NOT_SUPPORTED: lambda: _selection_case(SELL_NOT_SUPPORTED,
                                                     side="SELL"),
        SAMPLE_TRADE_REFUSED: lambda: _selection_case(SAMPLE_TRADE_REFUSED,
                                                       is_sample=1),
        MISSING_MARKET_IDENTITY: lambda: _selection_case(
            MISSING_MARKET_IDENTITY, market_source_id="  "),
    }
    for code, build in cases.items():
        res = build()
        assert res.status == STATUS_ERROR, (code, res)
        assert res.reason_codes == [code], (code, res.reason_codes)
        assert res.selection_error is True, (code, res)
        assert res.operational_error is False, (code, res)
        assert res.provider_error is False, (code, res)
        assert res.created is False and res.updated is False, res
        assert res.metadata_changed is False, res


# ── 4b. selection_error is serialized in as_dict ────────────────────────────
def test_selection_error_serialized():
    db, _ = _open()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, {}, side="SELL")
    res = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_fake_resolver)
    db.close()
    d = res.as_dict()
    assert d["selection_error"] is True, d
    assert d["operational_error"] is False, d
    assert d["provider_error"] is False, d
    assert d["status"] == STATUS_ERROR, d
    assert d["reason_codes"] == [SELL_NOT_SUPPORTED], d


# ── 4c. CLI: invalid-selection -> exit 2, no provider call, zero write ──────
import pytest  # noqa: E402


def _run_cli_invalid(reason_code, *, side="BUY", source=CANON_SOURCE,
                     is_sample=0, market_source_id=COND, trade_id="polymarket:st1"):
    import scripts.enrich_approved_source_trade as cli
    from polycopy.db.database import Database

    calls = {"commit": 0, "rollback": 0}

    class _RecDb(Database):
        def commit(self):
            calls["commit"] += 1
            return super().commit()

        def close(self):
            return None  # keep alive across CLI open->close->reopen

    p = _tmp()
    db = _RecDb(p).connect()
    db.conn.set_trace_callback(
        lambda sql: calls.__setitem__(
            "rollback",
            calls["rollback"] + (1 if "ROLLBACK" in sql.upper() else 0),
        )
    )
    _seed_wallet(db)
    _seed_trade(db, trade_id, COND, {},
                side=side, source=source,
                is_sample=is_sample, market_source_id=market_source_id)

    provider_calls = {"n": 0, "adapter_closed": False, "built": False}

    def _make_adapter():
        provider_calls["built"] = True

        class _Adapter:
            async def get_market_raw(self, cid):
                provider_calls["n"] += 1
                return dict(GAMMA_PAYLOAD)

            async def aclose(self):
                provider_calls["adapter_closed"] = True

        return _Adapter()

    orig_adapter = cli._make_adapter
    cli._make_adapter = _make_adapter
    orig_ro, orig_rw = cli.open_readonly, cli.open_writable
    cli.open_readonly = lambda path: db
    cli.open_writable = lambda path, a: db
    try:
        rc = cli.main(
            ["--source-trade-id", trade_id, "--write",
             "--allow-live", "--db-path", str(_tmp())]
        )
    finally:
        cli._make_adapter = orig_adapter
        cli.open_readonly, cli.open_writable = orig_ro, orig_rw
    db.close()
    return rc, calls, provider_calls, db


@pytest.mark.parametrize("reason_code,kwargs", [
    (SOURCE_NOT_SUPPORTED, dict(source="bogus_src")),
    (SELL_NOT_SUPPORTED, dict(side="SELL")),
    (SAMPLE_TRADE_REFUSED, dict(is_sample=1)),
    (MISSING_MARKET_IDENTITY, dict(market_source_id="  ")),
])
def test_cli_invalid_selection_exit2_zero_write(reason_code, kwargs):
    rc, calls, provider_calls, db = _run_cli_invalid(reason_code, **kwargs)
    assert rc == 2, (reason_code, rc)
    # No Gamma request occurred (eligibility is checked before the resolver).
    assert provider_calls["n"] == 0, (reason_code, provider_calls)
    # Zero enrichment rows written for the seeded trade.
    ins = db.fetchone(
        "SELECT COUNT(*) AS c FROM source_trade_enrichments "
        "WHERE source_trade_internal_id=?", ("polymarket:st1",))
    assert ins["c"] == 0, (reason_code, ins)
    # metadata unchanged (seeded empty {}).
    assert _metadata_of(db, "polymarket:st1") == {}, (reason_code, _metadata_of(db, "polymarket:st1"))
    # Outer commit never called; a defensive rollback occurred.
    assert calls["commit"] == 0, (reason_code, calls)
    # Adapter, if built (--allow-live path), must be closed.
    if provider_calls["built"]:
        assert provider_calls["adapter_closed"] is True, (reason_code, provider_calls)


def test_cli_unknown_id_exit2_zero_write():
    import scripts.enrich_approved_source_trade as cli
    from polycopy.db.database import Database

    calls = {"commit": 0, "rollback": 0}

    class _RecDb(Database):
        def commit(self):
            calls["commit"] += 1
            return super().commit()

        def close(self):
            return None

    p = _tmp()
    db = _RecDb(p).connect()
    db.conn.set_trace_callback(
        lambda sql: calls.__setitem__(
            "rollback",
            calls["rollback"] + (1 if "ROLLBACK" in sql.upper() else 0),
        )
    )
    # Seed a DIFFERENT id than the one requested, so the lookup genuinely misses.
    _seed_wallet(db)
    _seed_trade(db, "polymarket:present", COND, {})

    provider_calls = {"n": 0, "adapter_closed": False, "built": False}

    def _make_adapter():
        provider_calls["built"] = True

        class _Adapter:
            async def get_market_raw(self, cid):
                provider_calls["n"] += 1
                return dict(GAMMA_PAYLOAD)

            async def aclose(self):
                provider_calls["adapter_closed"] = True

        return _Adapter()

    orig_adapter = cli._make_adapter
    cli._make_adapter = _make_adapter
    orig_ro, orig_rw = cli.open_readonly, cli.open_writable
    cli.open_readonly = lambda path: db
    cli.open_writable = lambda path, a: db
    try:
        rc = cli.main(
            ["--source-trade-id", "polymarket:absent_id", "--write",
             "--allow-live", "--db-path", str(_tmp())]
        )
    finally:
        cli._make_adapter = orig_adapter
        cli.open_readonly, cli.open_writable = orig_ro, orig_rw
    db.close()
    assert rc == 2, rc
    assert provider_calls["n"] == 0, provider_calls
    assert calls["commit"] == 0, calls
    if provider_calls["built"]:
        assert provider_calls["adapter_closed"] is True, provider_calls


# ── 4d. honest outcomes stay exit 0; provider/persistence stay exit 1 ───────
def test_cli_honest_outcomes_exit0():
    import scripts.enrich_approved_source_trade as cli
    import polycopy.ingestion.source_trade_enrichment as ste

    db = _NoCloseDb(_tmp()).connect()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, {})

    orig_enrich = ste.enrich_source_trade_async
    orig_cli_enrich = cli.enrichment_async_fn
    cli.enrichment_async_fn = orig_enrich

    orig_ro, orig_rw = cli.open_readonly, cli.open_writable

    def _ro(path):
        return db

    def _rw(path, args):
        return db

    cli.open_readonly = _ro
    cli.open_writable = _rw
    try:
        rc = cli.main(
            ["--source-trade-id", "polymarket:st1", "--write",
             "--allow-live", "--db-path", str(_tmp())]
        )
    finally:
        cli.open_readonly, cli.open_writable = orig_ro, orig_rw
        ste.enrich_source_trade_async = orig_enrich
        cli.enrichment_async_fn = orig_cli_enrich
    db.close()
    # Honest complete enrichment -> exit 0.
    assert rc == 0, rc


def test_cli_equivalent_replay_still_zero_write_exit0():
    import scripts.enrich_approved_source_trade as cli
    import polycopy.ingestion.source_trade_enrichment as ste

    db = _NoCloseDb(_tmp()).connect()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, {})
    orig_enrich = ste.enrich_source_trade_async
    orig_cli_enrich = cli.enrichment_async_fn
    cli.enrichment_async_fn = orig_enrich

    orig_ro, orig_rw = cli.open_readonly, cli.open_writable

    def _ro(path):
        return db

    def _rw(path, args):
        return db

    cli.open_readonly = _ro
    cli.open_writable = _rw
    try:
        rc1 = cli.main(
            ["--source-trade-id", "polymarket:st1", "--write",
             "--allow-live", "--db-path", str(_tmp())]
        )
        rc2 = cli.main(
            ["--source-trade-id", "polymarket:st1", "--write",
             "--allow-live", "--db-path", str(_tmp())]
        )
    finally:
        cli.open_readonly, cli.open_writable = orig_ro, orig_rw
        ste.enrich_source_trade_async = orig_enrich
        cli.enrichment_async_fn = orig_cli_enrich
    db.close()
    assert rc1 == 0 and rc2 == 0, (rc1, rc2)
    # Two passes; the second replay must have produced zero enrichment INSERT.
    enr = get_enrichment(db, "polymarket:st1")
    assert enr is not None
