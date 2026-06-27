"""Tests for P04 risk gates, exposure limits, paper modes, kill switch."""


from polycopy.risk.gates import (
    ExposureLimits,
    GateResult,
    GateVerdict,
    OrderKillSwitch,
    PaperMode,
    RiskGate,
)


class TestPaperMode:
    def test_default_is_paper_manual(self):
        assert PaperMode.PAPER_MANUAL.value == "paper_manual"

    def test_all_modes(self):
        assert PaperMode.RESEARCH_ONLY.value == "research_only"
        assert PaperMode.PAPER_MANUAL.value == "paper_manual"
        assert PaperMode.PAPER_AUTO.value == "paper_auto"

    def test_string_compatible(self):
        assert PaperMode.PAPER_MANUAL == "paper_manual"


class TestOrderKillSwitch:
    def test_default_inactive(self):
        ks = OrderKillSwitch()
        assert ks.is_active is False

    def test_check_passes_when_inactive(self):
        ks = OrderKillSwitch()
        result = ks.check()
        assert result.is_passed is True
        assert result.gate_name == "order_kill_switch"

    def test_engage_blocks(self):
        ks = OrderKillSwitch()
        ks.engage(engaged_by="test-operator")  # correct spelling
        assert ks.is_active is True
        result = ks.check()
        assert result.is_blocked is True
        assert "test-operator" in result.reason

    def test_disengage_unblocks(self):
        ks = OrderKillSwitch()
        ks.engage()
        ks.disengage()
        assert ks.is_active is False
        result = ks.check()
        assert result.is_passed is True

    def test_engage_logs_critical(self, caplog):
        ks = OrderKillSwitch()
        with caplog.at_level("CRITICAL"):
            ks.engage(engaged_by="test")
        assert "KILL SWITCH ENGAGED" in caplog.text


class TestExposureLimits:
    def test_no_limits_passes(self):
        lim = ExposureLimits()
        result = lim.check(
            order_notional=1000.0,
            market_exposure=0.0,
            wallet_exposure=0.0,
            outcome_exposure=0.0,
            global_exposure=0.0,
        )
        assert result.is_passed is True

    def test_order_size_limit(self):
        lim = ExposureLimits(max_order_size=100.0)
        result = lim.check(
            order_notional=150.0,
            market_exposure=0.0,
            wallet_exposure=0.0,
            outcome_exposure=0.0,
            global_exposure=0.0,
        )
        assert result.is_blocked is True
        assert "order_size" in result.gate_name

    def test_per_market_limit(self):
        lim = ExposureLimits(max_per_market=500.0)
        result = lim.check(
            order_notional=100.0,
            market_exposure=450.0,
            wallet_exposure=0.0,
            outcome_exposure=0.0,
            global_exposure=0.0,
        )
        assert result.is_blocked is True
        assert "per_market" in result.gate_name

    def test_per_market_limit_passes_under(self):
        lim = ExposureLimits(max_per_market=500.0)
        result = lim.check(
            order_notional=49.0,
            market_exposure=450.0,
            wallet_exposure=0.0,
            outcome_exposure=0.0,
            global_exposure=0.0,
        )
        assert result.is_passed is True

    def test_per_wallet_limit(self):
        lim = ExposureLimits(max_per_wallet=200.0)
        result = lim.check(
            order_notional=50.0,
            market_exposure=0.0,
            wallet_exposure=180.0,
            outcome_exposure=0.0,
            global_exposure=0.0,
        )
        assert result.is_blocked is True
        assert "per_wallet" in result.gate_name

    def test_per_outcome_limit(self):
        lim = ExposureLimits(max_per_outcome=300.0)
        result = lim.check(
            order_notional=50.0,
            market_exposure=0.0,
            wallet_exposure=0.0,
            outcome_exposure=280.0,
            global_exposure=0.0,
        )
        assert result.is_blocked is True
        assert "per_outcome" in result.gate_name

    def test_global_limit(self):
        lim = ExposureLimits(max_global=1000.0)
        result = lim.check(
            order_notional=100.0,
            market_exposure=0.0,
            wallet_exposure=0.0,
            outcome_exposure=0.0,
            global_exposure=950.0,
        )
        assert result.is_blocked is True
        assert "global" in result.gate_name

    def test_multiple_limits_first_fails(self):
        lim = ExposureLimits(max_order_size=10.0, max_per_market=10000.0)
        result = lim.check(
            order_notional=50.0,
            market_exposure=0.0,
            wallet_exposure=0.0,
            outcome_exposure=0.0,
            global_exposure=0.0,
        )
        assert result.is_blocked is True
        # Order size should be checked first
        assert "order_size" in result.gate_name

    def test_zero_means_unlimited(self):
        lim = ExposureLimits(
            max_order_size=0.0,
            max_per_market=0.0,
            max_per_wallet=0.0,
            max_per_outcome=0.0,
            max_global=0.0,
        )
        result = lim.check(
            order_notional=999999.0,
            market_exposure=999999.0,
            wallet_exposure=999999.0,
            outcome_exposure=999999.0,
            global_exposure=999999.0,
        )
        assert result.is_passed is True


class TestRiskGate:
    def test_passes_when_all_clear(self):
        ks = OrderKillSwitch()
        lim = ExposureLimits()
        gate = RiskGate(ks, PaperMode.PAPER_AUTO, lim)
        result = gate.check(order_notional=100.0)
        assert result.is_passed is True

    def test_kill_switch_blocks(self):
        ks = OrderKillSwitch()
        ks.engage()
        lim = ExposureLimits()
        gate = RiskGate(ks, PaperMode.PAPER_AUTO, lim)
        result = gate.check(order_notional=100.0)
        assert result.is_blocked is True

    def test_research_only_blocks(self):
        ks = OrderKillSwitch()
        lim = ExposureLimits()
        gate = RiskGate(ks, PaperMode.RESEARCH_ONLY, lim)
        result = gate.check(order_notional=100.0)
        assert result.is_blocked is True
        assert "research_only" in result.gate_name

    def test_paper_manual_passes_risk_gate(self):
        """Paper_manual passes the risk gate — review delay is separate."""
        ks = OrderKillSwitch()
        lim = ExposureLimits()
        gate = RiskGate(ks, PaperMode.PAPER_MANUAL, lim)
        result = gate.check(order_notional=100.0)
        assert result.is_passed is True

    def test_requires_manual_confirm_in_manual_mode(self):
        ks = OrderKillSwitch()
        lim = ExposureLimits()
        gate = RiskGate(ks, PaperMode.PAPER_MANUAL, lim)
        assert gate.requires_manual_confirm is True

    def test_no_manual_confirm_in_auto_mode(self):
        ks = OrderKillSwitch()
        lim = ExposureLimits()
        gate = RiskGate(ks, PaperMode.PAPER_AUTO, lim)
        assert gate.requires_manual_confirm is False

    def test_exposure_limit_blocks(self):
        ks = OrderKillSwitch()
        lim = ExposureLimits(max_order_size=50.0)
        gate = RiskGate(ks, PaperMode.PAPER_AUTO, lim)
        result = gate.check(order_notional=100.0)
        assert result.is_blocked is True


class TestGateResult:
    def test_is_passed(self):
        r = GateResult(GateVerdict.PASS, "test", "ok")
        assert r.is_passed is True
        assert r.is_blocked is False

    def test_is_blocked(self):
        r = GateResult(GateVerdict.BLOCKED, "test", "blocked")
        assert r.is_blocked is True
        assert r.is_passed is False

    def test_needs_review_is_blocked(self):
        r = GateResult(GateVerdict.NEEDS_REVIEW, "test", "review")
        assert r.is_blocked is True
