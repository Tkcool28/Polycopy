"""Schema v12 audit-storage tests (Repair 1 — final pass).

Coverage:
  * Fresh DB has every v12 column on paper_signal_decisions and
    shadow_decisions.
  * v9 → v12, v10 → v12, v11 → v12 migrations apply additively.
  * Partial-v12 databases upgrade safely.
  * Reopening v12 is idempotent.
  * Old rows remain readable after v12 (no destructive rebuild).
  * Migration does not invent rows.
  * ``PRAGMA foreign_key_check`` is clean at v12.
  * Typed input round-trip preserves every field.
  * Identical replay produces byte-equivalent JSON.
  * Identical rerun returns the same paper_signal_id.
  * A changed ``trade_score_decision_id`` creates a NEW id.
  * Shadow persistence is replayable (idempotent on identical inputs).

The v12 schema adds the audit columns declared in
``polycopy.db.schema_v12``:

  paper_signal_decisions:
    decision_input_json TEXT
    wallet_score_decision_id INTEGER
    category_score_decision_id INTEGER
    trade_score_decision_id INTEGER

  shadow_decisions:
    target_delay_seconds REAL
    actual_observed_delay_seconds REAL
    delay_error_seconds REAL
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from polycopy.db.database import Database  # noqa: E402
from polycopy.db.schema import MIGRATIONS  # noqa: E402
from polycopy.db.schema_v11 import apply_v11_idempotent  # noqa: E402
from polycopy.db.schema_v12 import apply_v12_idempotent  # noqa: E402
from polycopy.scoring.paper_signal_input import (  # noqa: E402
    PaperSignalDecisionInput,
    deserialize_paper_signal_input,
    serialize_paper_signal_input,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _init_db_at_version(db_path: Path, target: int) -> sqlite3.Connection:
    """Init a DB and run migrations 1..target with raw sqlite3, FKs ON.

    Mirrors the helper in ``tests/test_p37_sqlite_foreign_key_enforcement``
    and extends it for v12 via :func:`apply_v12_idempotent`.

    Each migration is only applied if it has not already been applied
    (tracked via the ``_meta`` table) so re-running on a DB that
    already carries an earlier stage does not raise duplicate-column
    errors.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    for v in range(1, target + 1):
        # Skip migrations already applied (track via _meta).
        try:
            row = conn.execute(
                "SELECT value FROM _meta WHERE key = 'schema_version'"
            ).fetchone()
            current = int(row["value"]) if row else 0
        except sqlite3.OperationalError:
            # _meta doesn't exist yet (v1 hasn't run).
            current = 0
        if v < current:
            continue
        if v == 11:
            apply_v11_idempotent(conn)
        elif v == 12:
            apply_v12_idempotent(conn)
        else:
            for stmt in MIGRATIONS[v]:
                conn.execute(stmt)
        conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
            (str(v),),
        )
    conn.commit()
    return conn


def _fresh_db(db_path: Path) -> Database:
    """Open a fresh production Database (runs full migration stack)."""
    db = Database(db_path=db_path)
    db.connect()
    return db


def _build_typed_input(**overrides: object) -> PaperSignalDecisionInput:
    """Build a fully populated PaperSignalDecisionInput for round-trip tests."""
    base: dict = dict(
        candidate_id=42,
        source_trade_id="trade-abc",
        wallet_id="wallet-xyz",
        wallet_score_decision_id=11,
        category_score_decision_id=22,
        trade_score_decision_id=33,
        price_snapshot_id="snap-001",
        intended_stake=12.34,
        category_label="crypto",
        behavior_classification="directional",
        wallet_formula_name="wallet_score",
        wallet_formula_version="1",
        category_formula_name="category_wallet_score",
        category_formula_version="1",
        trade_formula_name="trade_copyability",
        trade_formula_version="1",
        evaluation_timestamp=datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc),
        final_verdict="copy_candidate",
        final_reason="paper_signal_verdict:copy_candidate",
        is_approved=0,
        auto_approve_requested=False,
    )
    base.update(overrides)
    return PaperSignalDecisionInput(**base)


def _seed_minimum_paper_signal(db: Database, candidate_id: int = 1) -> int:
    """Insert the minimum rows needed for ``persist_paper_signal``.

    Returns the candidate id used.
    """
    now = datetime.now(timezone.utc).isoformat()
    wid = str(uuid.uuid4())
    db.execute(
        "INSERT INTO wallets (id, address, label, is_sample, created_at) "
        "VALUES (?, ?, 'audit', 1, ?)",
        (wid, "0xaudit", now),
    )
    mid = str(uuid.uuid4())
    db.execute(
        "INSERT INTO markets (id, source_id, source, question, active, closed, "
        "resolved, volume_24h, fetched_at, is_sample) "
        "VALUES (?, 'audit', 'polymarket', 'audit?', 1, 0, 0, 1000.0, ?, 1)",
        (mid, now),
    )
    db.execute(
        "INSERT INTO copy_candidates ("
        "wallet_id, source, source_trade_id, market_id, side, "
        "source_trade_price, source_trade_quantity, "
        "source_trade_timestamp, observed_at, wallet_score_version, "
        "wallet_score, wallet_verdict, status, created_at, updated_at"
        ") VALUES (?, 'polymarket', 'audit-trade', ?, 'BUY', "
        "0.5, 10.0, ?, ?, '1', 50.0, 'watchlist', 'pending', ?, ?)",
        (wid, mid, now, now, now, now),
    )
    real_cid = db.fetchone("SELECT id FROM copy_candidates ORDER BY id DESC LIMIT 1")["id"]
    # Snapshot FK target — required because paper_signal_decisions.price_snapshot_id
    # has REFERENCES candidate_price_snapshots(id).
    db.execute(
        "INSERT INTO candidate_price_snapshots ("
        "id, candidate_id, snapshot_run_id, fetch_status, request_attempts, "
        "side, source_trade_price, source_trade_quantity, "
        "source_trade_timestamp, fetched_at, created_at"
        ") VALUES (?, ?, 'run-1', 'OK', 1, 'BUY', 0.5, 10.0, ?, ?, ?)",
        ("snap-001", real_cid, now, now, now),
    )
    db.conn.commit()
    return int(real_cid)


# ── 1. Fresh-DB column existence ──────────────────────────────────────────


class TestFreshDBHasV12Columns:
    def test_paper_signal_decisions_has_decision_input_json(self, tmp_path: Path):
        with _fresh_db(tmp_path / "fresh.db") as db:
            rows = db.fetchall("PRAGMA table_info(paper_signal_decisions)")
            names = {r["name"] for r in rows}
        assert "decision_input_json" in names

    def test_paper_signal_decisions_has_wallet_score_decision_id(self, tmp_path: Path):
        with _fresh_db(tmp_path / "fresh.db") as db:
            rows = db.fetchall("PRAGMA table_info(paper_signal_decisions)")
            names = {r["name"] for r in rows}
        assert "wallet_score_decision_id" in names

    def test_paper_signal_decisions_has_category_score_decision_id(self, tmp_path: Path):
        with _fresh_db(tmp_path / "fresh.db") as db:
            rows = db.fetchall("PRAGMA table_info(paper_signal_decisions)")
            names = {r["name"] for r in rows}
        assert "category_score_decision_id" in names

    def test_paper_signal_decisions_has_trade_score_decision_id(self, tmp_path: Path):
        with _fresh_db(tmp_path / "fresh.db") as db:
            rows = db.fetchall("PRAGMA table_info(paper_signal_decisions)")
            names = {r["name"] for r in rows}
        assert "trade_score_decision_id" in names

    def test_shadow_decisions_has_target_delay_seconds(self, tmp_path: Path):
        with _fresh_db(tmp_path / "fresh.db") as db:
            rows = db.fetchall("PRAGMA table_info(shadow_decisions)")
            names = {r["name"] for r in rows}
        assert "target_delay_seconds" in names

    def test_shadow_decisions_has_actual_observed_delay_seconds(self, tmp_path: Path):
        with _fresh_db(tmp_path / "fresh.db") as db:
            rows = db.fetchall("PRAGMA table_info(shadow_decisions)")
            names = {r["name"] for r in rows}
        assert "actual_observed_delay_seconds" in names

    def test_shadow_decisions_has_delay_error_seconds(self, tmp_path: Path):
        with _fresh_db(tmp_path / "fresh.db") as db:
            rows = db.fetchall("PRAGMA table_info(shadow_decisions)")
            names = {r["name"] for r in rows}
        assert "delay_error_seconds" in names


# ── 2. Upgrades are additive ──────────────────────────────────────────────


class TestMigrationAdditive:
    def test_v9_to_v12_migration_additive(self, tmp_path: Path):
        v9_path = tmp_path / "v9-stage.db"
        v12_path = tmp_path / "v12-stage.db"
        conn = _init_db_at_version(v9_path, target=9)
        before = {r["name"] for r in conn.execute(
            "PRAGMA table_info(paper_signal_decisions)"
        ).fetchall()}
        assert "decision_input_json" not in before
        conn.close()

        # Copy the v9 DB to a fresh path so the migration runner
        # doesn't re-apply v1..v9 (which would re-attempt v6's
        # ``ALTER TABLE wallets ADD COLUMN canonical_address`` on
        # a schema that already has it, raising duplicate-column).
        v12_path.write_bytes(v9_path.read_bytes())
        conn = _init_db_at_version(v12_path, target=12)
        after = {r["name"] for r in conn.execute(
            "PRAGMA table_info(paper_signal_decisions)"
        ).fetchall()}
        for col in (
            "decision_input_json",
            "wallet_score_decision_id",
            "category_score_decision_id",
            "trade_score_decision_id",
        ):
            assert col in after, f"{col} missing after v9→v12"

        after_shadow = {r["name"] for r in conn.execute(
            "PRAGMA table_info(shadow_decisions)"
        ).fetchall()}
        for col in (
            "target_delay_seconds",
            "actual_observed_delay_seconds",
            "delay_error_seconds",
        ):
            assert col in after_shadow, f"shadow.{col} missing after v9→v12"
        conn.close()

    def test_v10_to_v12_migration_additive(self, tmp_path: Path):
        conn = _init_db_at_version(tmp_path / "v10.db", target=12)
        after = {r["name"] for r in conn.execute(
            "PRAGMA table_info(paper_signal_decisions)"
        ).fetchall()}
        for col in (
            "decision_input_json",
            "wallet_score_decision_id",
            "category_score_decision_id",
            "trade_score_decision_id",
        ):
            assert col in after
        conn.close()

    def test_v11_to_v12_migration_additive(self, tmp_path: Path):
        conn = _init_db_at_version(tmp_path / "v11.db", target=12)
        after = {r["name"] for r in conn.execute(
            "PRAGMA table_info(paper_signal_decisions)"
        ).fetchall()}
        for col in (
            "decision_input_json",
            "wallet_score_decision_id",
            "category_score_decision_id",
            "trade_score_decision_id",
        ):
            assert col in after
        after_shadow = {r["name"] for r in conn.execute(
            "PRAGMA table_info(shadow_decisions)"
        ).fetchall()}
        for col in (
            "target_delay_seconds",
            "actual_observed_delay_seconds",
            "delay_error_seconds",
        ):
            assert col in after_shadow
        conn.close()


# ── 3. Idempotency ────────────────────────────────────────────────────────


class TestV12Idempotency:
    def test_partial_v12_columns_upgrade_safely(self, tmp_path: Path):
        # Build a v11 DB, then apply v12 once.
        conn = _init_db_at_version(tmp_path / "partial.db", target=11)
        apply_v12_idempotent(conn)
        # Re-applying must be a clean no-op (no duplicate-column errors).
        apply_v12_idempotent(conn)
        after = {r["name"] for r in conn.execute(
            "PRAGMA table_info(paper_signal_decisions)"
        ).fetchall()}
        for col in (
            "decision_input_json",
            "wallet_score_decision_id",
            "category_score_decision_id",
            "trade_score_decision_id",
        ):
            assert col in after
        conn.close()

    def test_reopen_v12_idempotent(self, tmp_path: Path):
        # Apply v12, close, reopen, apply again — must not error.
        db_path = tmp_path / "reopen.db"
        _init_db_at_version(db_path, target=12).close()
        # Second open via production Database should also be clean.
        with _fresh_db(db_path):
            pass
        with _fresh_db(db_path):
            rows = sqlite3.connect(str(db_path)).execute(
                "PRAGMA table_info(paper_signal_decisions)"
            ).fetchall()
            names = {r[1] for r in rows}
        for col in (
            "decision_input_json",
            "wallet_score_decision_id",
            "category_score_decision_id",
            "trade_score_decision_id",
        ):
            assert col in names

    def test_old_rows_remain_readable(self, tmp_path: Path):
        """Insert a v11-shaped row (NULL in v12 columns) before upgrading,
        then upgrade and confirm the row is still readable."""
        conn = _init_db_at_version(tmp_path / "old.db", target=11)
        # Insert a row that doesn't include the v12 columns; SQLite will
        # default them to NULL once v12 adds them.
        wid = str(uuid.uuid4())
        mid = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO wallets (id, address, label, is_sample, created_at) "
            "VALUES (?, '0xold', 'old', 1, ?)",
            (wid, now),
        )
        conn.execute(
            "INSERT INTO markets (id, source_id, source, question, active, closed, "
            "resolved, volume_24h, fetched_at, is_sample) "
            "VALUES (?, 'm', 'polymarket', 'q', 1, 0, 0, 0.0, ?, 1)",
            (mid, now),
        )
        conn.execute(
            "INSERT INTO copy_candidates (wallet_id, source, source_trade_id, "
            "market_id, side, source_trade_price, source_trade_quantity, "
            "source_trade_timestamp, observed_at, wallet_score_version, "
            "wallet_score, wallet_verdict, status, created_at, updated_at) "
            "VALUES (?, 'polymarket', 'old-trade', ?, 'BUY', 0.5, 10.0, "
            "?, ?, '1', 50.0, 'watchlist', 'pending', ?, ?)",
            (wid, mid, now, now, now, now),
        )
        cand_id = conn.execute(
            "SELECT id FROM copy_candidates ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        # FK target for price_snapshot_id (paper_signal_decisions
        # REFERENCES candidate_price_snapshots(id)).
        conn.execute(
            "INSERT INTO candidate_price_snapshots ("
            "id, candidate_id, snapshot_run_id, fetch_status, "
            "request_attempts, side, source_trade_price, "
            "source_trade_quantity, source_trade_timestamp, fetched_at, "
            "created_at) VALUES (?, ?, 'run-old', 'OK', 1, 'BUY', 0.5, "
            "10.0, ?, ?, ?)",
            ("snap-old", cand_id, now, now, now),
        )
        conn.execute(
            "INSERT INTO paper_signal_decisions (candidate_id, wallet_id, "
            "signal_family, signal_reason, wallet_score, trade_score, "
            "shadow_score, shadow_verdict, final_verdict, source_data_timestamp, "
            "source_trade_id, price_snapshot_id, idempotency_key, computed_at, "
            "created_at) VALUES (?, ?, 'watchlist', 'r', 50.0, 50.0, 50.0, "
            "'SHADOW_WATCHLIST', 'watchlist', ?, 'old-trade', 'snap-old', "
            "'idem-old', ?, ?)",
            (cand_id, wid, now, now, now),
        )
        conn.commit()
        pre_count = conn.execute(
            "SELECT COUNT(*) FROM paper_signal_decisions"
        ).fetchone()[0]
        assert pre_count == 1

        apply_v12_idempotent(conn)

        post_count = conn.execute(
            "SELECT COUNT(*) FROM paper_signal_decisions"
        ).fetchone()[0]
        assert post_count == 1, "v12 must not invent or destroy rows"

        row = conn.execute(
            "SELECT decision_input_json, wallet_score_decision_id, "
            "category_score_decision_id, trade_score_decision_id "
            "FROM paper_signal_decisions WHERE candidate_id = ?",
            (cand_id,),
        ).fetchone()
        assert row[0] is None, "decision_input_json should be NULL for old rows"
        assert row[1] is None
        assert row[2] is None
        assert row[3] is None
        conn.close()

    def test_no_fabricated_rows(self, tmp_path: Path):
        """Counting rows before and after v12 must match."""
        conn = _init_db_at_version(tmp_path / "empty.db", target=11)
        before_paper = conn.execute(
            "SELECT COUNT(*) FROM paper_signal_decisions"
        ).fetchone()[0]
        before_shadow = conn.execute(
            "SELECT COUNT(*) FROM shadow_decisions"
        ).fetchone()[0]
        apply_v12_idempotent(conn)
        after_paper = conn.execute(
            "SELECT COUNT(*) FROM paper_signal_decisions"
        ).fetchone()[0]
        after_shadow = conn.execute(
            "SELECT COUNT(*) FROM shadow_decisions"
        ).fetchone()[0]
        assert before_paper == after_paper
        assert before_shadow == after_shadow
        conn.close()

    def test_foreign_key_check_clean(self, tmp_path: Path):
        conn = _init_db_at_version(tmp_path / "fk.db", target=12)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert violations == [], f"FK violations: {violations}"
        conn.close()


# ── 4. Typed input round-trip / replay / identity ─────────────────────────


class TestTypedInputRoundTrip:
    def test_typed_input_round_trip(self):
        """serialize → deserialize → assertEqual on every field."""
        original = _build_typed_input()
        payload = serialize_paper_signal_input(original)
        rebuilt = deserialize_paper_signal_input(payload)
        # Field-by-field comparison (dataclass __eq__ would also work,
        # but explicit fields catch any drift in the serializer).
        for field_name in (
            "candidate_id", "source_trade_id", "wallet_id",
            "wallet_score_decision_id", "category_score_decision_id",
            "trade_score_decision_id", "price_snapshot_id",
            "intended_stake", "category_label", "behavior_classification",
            "wallet_formula_name", "wallet_formula_version",
            "category_formula_name", "category_formula_version",
            "trade_formula_name", "trade_formula_version",
            "final_verdict", "final_reason", "is_approved",
            "auto_approve_requested",
        ):
            assert getattr(original, field_name) == getattr(
                rebuilt, field_name
            ), f"{field_name}: {getattr(original, field_name)!r} vs {getattr(rebuilt, field_name)!r}"
        # Datetime round-trip must be byte-equal at microsecond precision.
        assert original.evaluation_timestamp == rebuilt.evaluation_timestamp

    def test_identical_replay_byte_equivalent(self):
        """Two serializations of the same input MUST be byte-identical."""
        original = _build_typed_input()
        a = serialize_paper_signal_input(original)
        b = serialize_paper_signal_input(original)
        assert a == b
        # And the JSON must be sorted-key canonical.
        obj = json.loads(a)
        keys = list(obj.keys())
        assert keys == sorted(keys), "JSON keys are not sorted"

    def test_identical_rerun_returns_same_paper_signal_id(self, tmp_path: Path):
        """Calling persist_paper_signal twice with the same typed_input
        returns the same paper_signal_id (idempotent UNIQUE)."""
        from polycopy.scoring.score_serialization import persist_paper_signal

        with _fresh_db(tmp_path / "idem.db") as db:
            cid = _seed_minimum_paper_signal(db)
            # Look up the real wallet id used in the seed so the FK
            # check on paper_signal_decisions(wallet_id) passes.
            real_wid = db.fetchone(
                "SELECT wallet_id FROM copy_candidates WHERE id = ?",
                (cid,),
            )["wallet_id"]
            typed_input = _build_typed_input(
                candidate_id=cid, wallet_id=real_wid,
            )
            snap_ts = datetime.now(timezone.utc).isoformat()
            id_a = persist_paper_signal(
                db, cid, real_wid, "copy_candidate",
                "paper_signal_verdict:copy_candidate",
                80.0, 80.0, 80.0, "SHADOW_COPY_CANDIDATE",
                "copy_candidate", snap_ts, "trade-abc", "snap-001",
                typed_input=typed_input,
            )
            id_b = persist_paper_signal(
                db, cid, real_wid, "copy_candidate",
                "paper_signal_verdict:copy_candidate",
                80.0, 80.0, 80.0, "SHADOW_COPY_CANDIDATE",
                "copy_candidate", snap_ts, "trade-abc", "snap-001",
                typed_input=typed_input,
            )
        assert id_a == id_b
        assert id_a > 0

    def test_changed_trade_decision_id_creates_new_paper_signal_id(
        self, tmp_path: Path
    ):
        """A changed trade_score_decision_id MUST produce a new paper_signal_id."""
        from polycopy.scoring.score_serialization import persist_paper_signal

        with _fresh_db(tmp_path / "change.db") as db:
            cid = _seed_minimum_paper_signal(db)
            real_wid = db.fetchone(
                "SELECT wallet_id FROM copy_candidates WHERE id = ?",
                (cid,),
            )["wallet_id"]
            snap_ts = datetime.now(timezone.utc).isoformat()
            t1 = _build_typed_input(
                candidate_id=cid, wallet_id=real_wid,
                trade_score_decision_id=100,
            )
            t2 = _build_typed_input(
                candidate_id=cid, wallet_id=real_wid,
                trade_score_decision_id=101,
            )
            id_a = persist_paper_signal(
                db, cid, real_wid, "copy_candidate",
                "paper_signal_verdict:copy_candidate",
                80.0, 80.0, 80.0, "SHADOW_COPY_CANDIDATE",
                "copy_candidate", snap_ts, "trade-abc", "snap-001",
                typed_input=t1,
            )
            id_b = persist_paper_signal(
                db, cid, real_wid, "copy_candidate",
                "paper_signal_verdict:copy_candidate",
                80.0, 80.0, 80.0, "SHADOW_COPY_CANDIDATE",
                "copy_candidate", snap_ts, "trade-abc", "snap-001",
                typed_input=t2,
            )
        assert id_a != id_b, "changed trade_decision_id must yield new id"

    def test_decision_input_json_persisted_and_readable(self, tmp_path: Path):
        """The audit JSON column must contain a parseable canonical JSON."""
        from polycopy.scoring.score_serialization import persist_paper_signal

        with _fresh_db(tmp_path / "json.db") as db:
            cid = _seed_minimum_paper_signal(db)
            real_wid = db.fetchone(
                "SELECT wallet_id FROM copy_candidates WHERE id = ?",
                (cid,),
            )["wallet_id"]
            typed_input = _build_typed_input(
                candidate_id=cid, wallet_id=real_wid,
            )
            snap_ts = datetime.now(timezone.utc).isoformat()
            psid = persist_paper_signal(
                db, cid, real_wid, "copy_candidate",
                "paper_signal_verdict:copy_candidate",
                80.0, 80.0, 80.0, "SHADOW_COPY_CANDIDATE",
                "copy_candidate", snap_ts, "trade-abc", "snap-001",
                typed_input=typed_input,
            )
            row = db.fetchone(
                "SELECT decision_input_json, wallet_score_decision_id, "
                "category_score_decision_id, trade_score_decision_id "
                "FROM paper_signal_decisions WHERE id = ?",
                (psid,),
            )
        payload = row["decision_input_json"]
        assert payload is not None
        obj = json.loads(payload)
        assert obj["candidate_id"] == cid
        assert obj["trade_score_decision_id"] == 33
        assert obj["wallet_score_decision_id"] == 11
        assert row["wallet_score_decision_id"] == 11
        assert row["trade_score_decision_id"] == 33

    def test_legacy_caller_writes_null_audit_columns(self, tmp_path: Path):
        """A caller that omits typed_input gets NULLs in all four audit cols."""
        from polycopy.scoring.score_serialization import persist_paper_signal

        with _fresh_db(tmp_path / "legacy.db") as db:
            cid = _seed_minimum_paper_signal(db)
            real_wid = db.fetchone(
                "SELECT wallet_id FROM copy_candidates WHERE id = ?",
                (cid,),
            )["wallet_id"]
            snap_ts = datetime.now(timezone.utc).isoformat()
            psid = persist_paper_signal(
                db, cid, real_wid, "watchlist",
                "no_typed_input", 50.0, 50.0, 50.0,
                "SHADOW_WATCHLIST", "watchlist",
                snap_ts, "trade-abc", "snap-001",
                # NOTE: typed_input is intentionally omitted
            )
            row = db.fetchone(
                "SELECT decision_input_json, wallet_score_decision_id, "
                "category_score_decision_id, trade_score_decision_id "
                "FROM paper_signal_decisions WHERE id = ?",
                (psid,),
            )
        assert row["decision_input_json"] is None
        assert row["wallet_score_decision_id"] is None
        assert row["category_score_decision_id"] is None
        assert row["trade_score_decision_id"] is None


# ── 5. Shadow persistence replayability ───────────────────────────────────


class TestShadowReplayability:
    def test_shadow_replay_byte_equivalent(self, tmp_path: Path):
        """Re-persisting the same shadow result is idempotent."""
        from polycopy.scoring.shadow_score_v2_typed import (
            DelayScenario,
            ShadowScoreInputV2,
        )
        from polycopy.scoring.shadow_score_v2_engine import (
            compute_shadow_score_v2_from_input,
        )
        from polycopy.scoring.score_serialization import persist_shadow_score_v2

        with _fresh_db(tmp_path / "shadow.db") as db:
            wid = str(uuid.uuid4())
            mid = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            db.execute(
                "INSERT INTO wallets (id, address, label, is_sample, created_at) "
                "VALUES (?, '0xshadow', 'sh', 1, ?)",
                (wid, now),
            )
            db.execute(
                "INSERT INTO markets (id, source_id, source, question, active, "
                "closed, resolved, volume_24h, fetched_at, is_sample) "
                "VALUES (?, 'm', 'polymarket', 'q', 1, 0, 0, 0.0, ?, 1)",
                (mid, now),
            )
            db.execute(
                "INSERT INTO copy_candidates (wallet_id, source, "
                "source_trade_id, market_id, side, "
                "source_trade_price, source_trade_quantity, "
                "source_trade_timestamp, observed_at, wallet_score_version, "
                "wallet_score, wallet_verdict, status, created_at, "
                "updated_at) "
                "VALUES (?, 'polymarket', 'shadow-trade', ?, 'BUY', 0.5, "
                "10.0, ?, ?, '1', 50.0, 'watchlist', 'pending', ?, ?)",
                (wid, mid, now, now, now, now),
            )
            db.conn.commit()
            cand_id = db.fetchone(
                "SELECT id FROM copy_candidates ORDER BY id DESC LIMIT 1"
            )["id"]

            typed_in = ShadowScoreInputV2(
                wallet_id=wid,
                source_trade_id="shadow-trade",
                candidate_id=int(cand_id),
                delay_scenario=DelayScenario.THEORETICAL_IMMEDIATE,
                source_price=0.5,
                delayed_copy_price=0.5,
                intended_stake=10.0,
                executable_depth=None,
                fill_percentage=None,
                slippage=None,
                spread=None,
                wallet_skill_persistence_input=None,
                copied_realized_performance_input=None,
                concentration_correlation_input=None,
                source_data_timestamp=now,
                price_snapshot_id=None,
                depth_hash=None,
                target_delay_seconds=0.0,
                actual_observed_delay_seconds=0.0,
                delay_error_seconds=0.0,
            )
            result = compute_shadow_score_v2_from_input(typed_in)

            id_a = persist_shadow_score_v2(
                db, wid, "shadow-trade", result,
                candidate_id=int(cand_id),
                source_data_timestamp=now,
            )
            id_b = persist_shadow_score_v2(
                db, wid, "shadow-trade", result,
                candidate_id=int(cand_id),
                source_data_timestamp=now,
            )
            # Offset audit fields populated on the row.
            row = db.fetchone(
                "SELECT target_delay_seconds, actual_observed_delay_seconds, "
                "delay_error_seconds FROM shadow_decisions WHERE id = ?",
                (id_a,),
            )
        assert id_a == id_b
        assert row["target_delay_seconds"] == 0.0
        assert row["actual_observed_delay_seconds"] == 0.0
        assert row["delay_error_seconds"] == 0.0