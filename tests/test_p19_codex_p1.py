"""P19 regressions for Codex P1 paper exposure and sell accounting fixes."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi.testclient import TestClient

from polycopy.api.app import app

MARKET_X = "00000000-0000-0000-0000-000000000101"
MARKET_Y = "00000000-0000-0000-0000-000000000102"
WALLET_A = "00000000-0000-0000-0000-000000000201"
WALLET_B = "00000000-0000-0000-0000-000000000202"
NOW = datetime.now(timezone.utc).isoformat()


def _reset(monkeypatch, tmp_path, **env: object):
    monkeypatch.setenv("POLYCOPY_ENABLE_DEMO_DATA", "false")
    monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p19.sqlite"))
    monkeypatch.setenv("POLYCOPY_ORDER_KILL_SWITCH", "false")
    monkeypatch.setenv("POLYCOPY_ORDER_PREVIEW_MAX_AGE_SECONDS", "86400")
    monkeypatch.setenv("POLYCOPY_STALENESS_SECONDS", "0")
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))

    import polycopy.config.settings as settings_module
    import polycopy.db.database as database_module
    from polycopy.api.app import _bidask_provider, _idempotency_store

    if database_module._db is not None:
        database_module._db.close()
    database_module._db = None
    settings_module._settings = None
    _idempotency_store._db = None  # noqa: SLF001
    _idempotency_store._ensured_table = False  # noqa: SLF001
    _bidask_provider.clear()
    return database_module, settings_module, _idempotency_store


def _db():
    from polycopy.db.database import get_database

    return get_database()


def _seed_wallet_market(wallet_id: str, market_id: str) -> None:
    db = _db()
    db.execute(
        "INSERT OR IGNORE INTO wallets (id, address, label, is_sample, created_at) VALUES (?, ?, ?, 0, ?)",
        (wallet_id, f"0x{wallet_id[-6:]}", "test-wallet", NOW),
    )
    db.execute(
        "INSERT OR IGNORE INTO markets (id, source_id, source, question, fetched_at, is_sample) VALUES (?, ?, 'test', 'Q', ?, 0)",
        (market_id, f"src-{market_id}", NOW),
    )
    db.conn.commit()


def _seed_order(
    *,
    order_id: str | None = None,
    wallet_id: str = WALLET_A,
    market_id: str = MARKET_X,
    side: str = "buy",
    outcome: str = "Yes",
    quantity: float = 10.0,
    price: float = 0.5,
    status: str = "pending",
    filled_quantity: float = 0.0,
) -> str:
    order_id = order_id or str(uuid4())
    _seed_wallet_market(wallet_id, market_id)
    db = _db()
    db.execute(
        """
        INSERT INTO orders
            (id, market_id, wallet_id, side, order_type, outcome, quantity, price,
             status, filled_quantity, created_at, updated_at, is_sample)
        VALUES (?, ?, ?, ?, 'limit', ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (order_id, market_id, wallet_id, side, outcome, quantity, price, status, filled_quantity, NOW, NOW),
    )
    db.conn.commit()
    return order_id


def _seed_position(*, wallet_id: str = WALLET_A, market_id: str = MARKET_X, outcome: str = "Yes", quantity: float, avg: float, realized: float = 0.0) -> None:
    _seed_wallet_market(wallet_id, market_id)
    db = _db()
    position_id = str(uuid5(NAMESPACE_URL, f"polycopy-position:{market_id}:{wallet_id}:{outcome}"))
    db.execute(
        """
        INSERT OR REPLACE INTO positions
            (id, market_id, wallet_id, outcome, quantity, avg_entry_price,
             current_price, realized_pnl, opened_at, updated_at, is_sample)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (position_id, market_id, wallet_id, outcome, quantity, avg, avg, realized, NOW, NOW),
    )
    db.conn.commit()


def _seed_balance(wallet_id: str = WALLET_A, amount: float = 100.0) -> None:
    _seed_wallet_market(wallet_id, MARKET_X)
    _db().execute(
        "INSERT INTO wallet_balances (wallet_id, currency, amount, as_of, is_sample) VALUES (?, 'USDC', ?, ?, 0)",
        (wallet_id, amount, NOW),
    )
    _db().conn.commit()


def _balance(wallet_id: str = WALLET_A) -> float:
    row = _db().fetchone("SELECT amount FROM wallet_balances WHERE wallet_id = ? AND currency = 'USDC' ORDER BY id DESC LIMIT 1", (wallet_id,))
    assert row is not None
    return float(row["amount"])


def _position(wallet_id: str = WALLET_A, market_id: str = MARKET_X, outcome: str = "Yes"):
    return _db().fetchone(
        "SELECT * FROM positions WHERE wallet_id = ? AND market_id = ? AND outcome = ?",
        (wallet_id, market_id, outcome),
    )


def _counts() -> dict[str, int]:
    db = _db()
    return {
        "filled_orders": int(db.fetchone("SELECT COUNT(*) AS n FROM orders WHERE status = 'filled'")["n"]),
        "positions": int(db.fetchone("SELECT COUNT(*) AS n FROM positions")["n"]),
        "decisions": int(db.fetchone("SELECT COUNT(*) AS n FROM decision_log")["n"]),
    }


class TestOutcomeExposureAggregation:
    def test_two_wallet_outcome_cap_blocks_without_mutation(self, monkeypatch, tmp_path):
        _reset(monkeypatch, tmp_path, POLYCOPY_MAX_EXPOSURE_PER_OUTCOME=100, POLYCOPY_MAX_EXPOSURE_PER_MARKET=0, POLYCOPY_MAX_EXPOSURE_PER_WALLET=0, POLYCOPY_MAX_EXPOSURE_GLOBAL=0)
        _seed_position(wallet_id=WALLET_A, market_id=MARKET_X, outcome="Yes", quantity=95, avg=1.0)
        order_id = _seed_order(wallet_id=WALLET_B, market_id=MARKET_X, outcome="Yes", quantity=10, price=1.0)
        before = _counts()

        with TestClient(app) as client:
            resp = client.post("/paper/approve", json={"order_id": order_id})

        assert resp.status_code == 409
        assert "per_outcome" in resp.json()["detail"]
        assert _counts() == before
        order = _db().fetchone("SELECT status, filled_quantity FROM orders WHERE id = ?", (order_id,))
        assert order["status"] == "pending"
        assert float(order["filled_quantity"]) == 0.0

    def test_different_outcome_does_not_count_against_outcome_cap(self, monkeypatch, tmp_path):
        _reset(monkeypatch, tmp_path, POLYCOPY_MAX_EXPOSURE_PER_OUTCOME=100, POLYCOPY_MAX_EXPOSURE_PER_MARKET=0, POLYCOPY_MAX_EXPOSURE_GLOBAL=0)
        _seed_position(wallet_id=WALLET_A, market_id=MARKET_X, outcome="Yes", quantity=95, avg=1.0)
        order_id = _seed_order(wallet_id=WALLET_B, market_id=MARKET_X, outcome="No", quantity=10, price=0.4)

        with TestClient(app) as client:
            resp = client.post("/paper/approve", json={"order_id": order_id})

        assert resp.status_code == 200
        assert _position(WALLET_B, MARKET_X, "No") is not None

    def test_different_market_does_not_count_against_outcome_cap(self, monkeypatch, tmp_path):
        _reset(monkeypatch, tmp_path, POLYCOPY_MAX_EXPOSURE_PER_OUTCOME=100, POLYCOPY_MAX_EXPOSURE_PER_MARKET=0, POLYCOPY_MAX_EXPOSURE_GLOBAL=0)
        _seed_position(wallet_id=WALLET_A, market_id=MARKET_X, outcome="Yes", quantity=95, avg=1.0)
        order_id = _seed_order(wallet_id=WALLET_B, market_id=MARKET_Y, outcome="Yes", quantity=10, price=0.4)

        with TestClient(app) as client:
            resp = client.post("/paper/approve", json={"order_id": order_id})

        assert resp.status_code == 200
        assert _position(WALLET_B, MARKET_Y, "Yes") is not None

    def test_per_wallet_limit_remains_separate(self, monkeypatch, tmp_path):
        _reset(monkeypatch, tmp_path, POLYCOPY_MAX_EXPOSURE_PER_WALLET=100, POLYCOPY_MAX_EXPOSURE_PER_OUTCOME=0, POLYCOPY_MAX_EXPOSURE_PER_MARKET=0, POLYCOPY_MAX_EXPOSURE_GLOBAL=0)
        _seed_position(wallet_id=WALLET_A, market_id=MARKET_X, outcome="Yes", quantity=95, avg=1.0)
        order_id = _seed_order(wallet_id=WALLET_A, market_id=MARKET_Y, outcome="No", quantity=10, price=1.0)

        with TestClient(app) as client:
            resp = client.post("/paper/approve", json={"order_id": order_id})

        assert resp.status_code == 409
        assert "per_wallet" in resp.json()["detail"]


class TestSellAccounting:
    def test_partial_profitable_sell_reduces_qty_realizes_pnl_and_exposure(self, monkeypatch, tmp_path):
        _reset(monkeypatch, tmp_path, POLYCOPY_MAX_EXPOSURE_PER_WALLET=60, POLYCOPY_MAX_EXPOSURE_PER_OUTCOME=0, POLYCOPY_MAX_EXPOSURE_PER_MARKET=0, POLYCOPY_MAX_EXPOSURE_GLOBAL=0)
        _seed_position(quantity=100, avg=0.4)
        _seed_balance(amount=100.0)
        sell_id = _seed_order(side="sell", quantity=25, price=0.7)

        with TestClient(app) as client:
            sell = client.post("/paper/approve", json={"order_id": sell_id})
        assert sell.status_code == 200
        pos = _position()
        assert float(pos["quantity"]) == 75.0
        assert float(pos["avg_entry_price"]) == 0.4
        assert round(float(pos["realized_pnl"]), 6) == 7.5
        assert float(pos["current_price"]) == 0.7
        assert round(_balance(), 6) == 117.4825
        decision = _db().fetchone("SELECT metrics FROM decision_log WHERE order_id = ?", (sell_id,))
        assert decision is not None
        metrics = decision["metrics"]
        assert '"fee": 0.0175' in metrics
        assert '"realized_pnl": 7.5' in metrics

        # Exposure fell to 30 (=75*0.4), so a buy that would have been blocked at 65 now passes at 55.
        buy_id = _seed_order(side="buy", quantity=50, price=0.5)
        with TestClient(app) as client:
            buy = client.post("/paper/approve", json={"order_id": buy_id})
        assert buy.status_code == 200

    def test_full_losing_sell_closes_to_zero_and_no_open_exposure(self, monkeypatch, tmp_path):
        _reset(monkeypatch, tmp_path)
        _seed_position(quantity=10, avg=0.8)
        sell_id = _seed_order(side="sell", quantity=10, price=0.3)

        with TestClient(app) as client:
            resp = client.post("/paper/approve", json={"order_id": sell_id})

        assert resp.status_code == 200
        pos = _position()
        assert float(pos["quantity"]) == 0.0
        assert round(float(pos["realized_pnl"]), 6) == -5.0
        assert client.get("/positions").json()["total_cost_basis"] == 0.0

    def test_oversell_rejects_without_mutation(self, monkeypatch, tmp_path):
        _reset(monkeypatch, tmp_path)
        _seed_position(quantity=10, avg=0.5)
        sell_id = _seed_order(side="sell", quantity=11, price=0.6)
        before = _counts()
        before_pos = dict(_position())

        with TestClient(app) as client:
            resp = client.post("/paper/approve", json={"order_id": sell_id})

        assert resp.status_code == 409
        assert _counts() == before
        assert dict(_position()) == before_pos

    def test_sell_without_position_rejects_without_negative_or_long_position(self, monkeypatch, tmp_path):
        _reset(monkeypatch, tmp_path)
        sell_id = _seed_order(side="sell", quantity=5, price=0.6)

        with TestClient(app) as client:
            resp = client.post("/paper/approve", json={"order_id": sell_id})

        assert resp.status_code == 409
        assert _position() is None
        assert _counts()["decisions"] == 0

    def test_sell_restart_and_idempotency_replay_does_not_double_apply(self, monkeypatch, tmp_path):
        db_mod, settings_mod, idem = _reset(monkeypatch, tmp_path)
        _seed_position(quantity=20, avg=0.5)
        _seed_balance(amount=50.0)
        sell_id = _seed_order(side="sell", quantity=5, price=0.8)

        with TestClient(app) as client:
            first = client.post("/paper/approve", json={"order_id": sell_id, "idempotency_key": "sell-once"})
        assert first.status_code == 200

        db_mod._db.close()
        db_mod._db = None
        settings_mod._settings = None
        idem._db = None  # noqa: SLF001
        idem._ensured_table = False  # noqa: SLF001

        with TestClient(app) as client:
            replay = client.post("/paper/approve", json={"order_id": sell_id, "idempotency_key": "sell-once"})
        assert replay.status_code == 200
        pos = _position()
        assert float(pos["quantity"]) == 15.0
        assert round(float(pos["realized_pnl"]), 6) == 1.5
        assert round(_balance(), 6) == 53.996
        assert _counts()["filled_orders"] == 1
        assert _counts()["decisions"] == 1

    def test_risk_state_after_sell_reflects_reduced_open_position(self, monkeypatch, tmp_path):
        _reset(monkeypatch, tmp_path, POLYCOPY_MAX_EXPOSURE_PER_MARKET=70, POLYCOPY_MAX_EXPOSURE_PER_WALLET=0, POLYCOPY_MAX_EXPOSURE_PER_OUTCOME=0, POLYCOPY_MAX_EXPOSURE_GLOBAL=0)
        _seed_position(quantity=100, avg=0.6)
        sell_id = _seed_order(side="sell", quantity=50, price=0.6)
        with TestClient(app) as client:
            assert client.post("/paper/approve", json={"order_id": sell_id}).status_code == 200
            buy_id = _seed_order(side="buy", quantity=60, price=0.5)
            # Market exposure is now 30, not the original 60, so 30 + 30 <= 70.
            resp = client.post("/paper/approve", json={"order_id": buy_id})
        assert resp.status_code == 200
