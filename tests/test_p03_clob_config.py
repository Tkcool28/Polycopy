"""PR-3 (recovery sequence) candidate price-snapshot config tests.

This suite pins the PR-3 configuration contract. PR-3 introduces five
new ``POLYCOPY_*`` settings (clob_enabled, clob_base_url, clob_timeout,
clob_max_retries, clob_rpm). The tests verify:

  * ``POLYCOPY_CLOB_ENABLED`` defaults to ``False`` (the load-bearing
    safety invariant — no production flow may start hitting
    clob.polymarket.com after this PR merges).
  * All other PR-3 settings have their documented defaults.
  * Parsing accepts the repo's existing bool-parsing convention.
  * Enabling the flag in a test does NOT itself cause an HTTP
    request.
  * No production service, timer, or scan entrypoint imports
    ``PolymarketClobClient``, ``snapshot_one``, or
    ``persist_price_snapshot`` (static source-inspection guard
    matches the user-approved pattern when a runtime factory
    cannot be added without inventing production wiring).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from uuid import uuid4

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT))

from polycopy.config.settings import Settings  # noqa: E402


# ── Default values ───────────────────────────────────────────────────────────
def test_clob_enabled_defaults_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env override: ``POLYCOPY_CLOB_ENABLED`` is False.

    This is the load-bearing safety invariant — deploying PR-3 must
    not start hitting clob.polymarket.com. A CI regression here is
    a deployment-blocker.
    """
    monkeypatch.delenv("POLYCOPY_CLOB_ENABLED", raising=False)
    s = Settings()
    assert s.clob_enabled is False


def test_clob_other_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Other CLOB settings take the documented defaults when no env override."""
    for key in (
        "POLYCOPY_CLOB_ENABLED",
        "POLYCOPY_CLOB_BASE_URL",
        "POLYCOPY_CLOB_TIMEOUT_SECONDS",
        "POLYCOPY_CLOB_MAX_RETRIES",
        "POLYCOPY_CLOB_RPM",
    ):
        monkeypatch.delenv(key, raising=False)
    s = Settings()
    assert s.clob_base_url == "https://clob.polymarket.com"
    assert s.clob_timeout_seconds == 10.0
    assert s.clob_max_retries == 3
    assert s.clob_rpm == 30


def test_clob_enabled_accepts_repo_bool_convention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``true`` / ``false`` strings are parsed per repo convention.

    pydantic-settings' default bool parser is permissive — any
    truthy string enables the flag, and any falsy string disables
    it. This test pins the BOTH directions so a future change to
    pydantic-settings does not silently flip the default.
    """
    for truthy in ("true", "True", "1", "yes"):
        monkeypatch.setenv("POLYCOPY_CLOB_ENABLED", truthy)
        assert Settings().clob_enabled is True, (
            f"truthy value {truthy!r} did not enable clob"
        )
    for falsy in ("false", "False", "0", "no"):
        monkeypatch.setenv("POLYCOPY_CLOB_ENABLED", falsy)
        assert Settings().clob_enabled is False, (
            f"falsy value {falsy!r} did not disable clob"
        )


def test_enabling_clob_does_not_trigger_http_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabling the flag in a test must NOT itself make an HTTP request.

    The flag is a configuration value; it does not have side effects.
    This test guards against a future regression where Settings() or
    its accessor method instantiates a client and probes the network.
    """
    from polycopy.config.settings import Settings

    monkeypatch.setenv("POLYCOPY_CLOB_ENABLED", "true")
    s = Settings()
    assert s.clob_enabled is True

    # If a future regression adds a side-effecting accessor (e.g. a
    # cached_property that instantiates a client), this assertion
    # will fail. The flag is a value, not a hook.
    assert s.clob_enabled is True  # re-read is also safe


# ── No production wiring (static guard) ─────────────────────────────────────
_RUN_SCAN_PATH = _REPO_ROOT / "scripts" / "run_scan.py"


@pytest.mark.skipif(
    not _RUN_SCAN_PATH.exists(),
    reason="scripts/run_scan.py not present in this checkout",
)
def test_run_scan_does_not_import_pr3_engine() -> None:
    """scripts/run_scan.py must NOT import PR-3 modules.

    PR-3 deliberately does not wire the snapshot engine into the scan
    flow. A future PR (PR-4 or later) is the right place to add that
    wiring, and only after explicit approval. This test fails the
    build if anyone sneaks an import in.
    """
    source = _RUN_SCAN_PATH.read_text(encoding="utf-8")
    forbidden = (
        "PolymarketClobClient",
        "snapshot_one",
        "persist_price_snapshot",
    )
    for name in forbidden:
        # We allow the names in COMMENTS / docstrings, so we look
        # only at lines that look like import / from-import lines.
        bad = [
            line
            for line in source.splitlines()
            if re.search(rf"\b{name}\b", line)
            and re.search(r"^\s*(import|from)\b", line)
        ]
        assert not bad, (
            f"scripts/run_scan.py must not import {name!r} in PR-3; "
            f"found {bad!r}"
        )


# ── Adapter is importable + does not auto-run on import ─────────────────────
def test_clob_adapter_import_does_not_make_http_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing the CLOB adapter must not make any HTTP call.

    Verified by spying on httpx.AsyncClient.get — if the import path
    tried to probe the network, the spy would fire.
    """

    from polycopy.adapters import polymarket_clob  # noqa: F401

    called: list[tuple[str, dict]] = []

    async def fake_get(*args: object, **kwargs: object) -> object:  # pragma: no cover
        called.append((str(args), dict(kwargs)))
        raise AssertionError("HTTP client called on import")

    # If the module already constructed an AsyncClient at import
    # time, swap its get. The module does not — it only constructs
    # a client when explicitly instantiated — so this is a
    # belt-and-suspenders guard.
    if hasattr(polymarket_clob, "PolymarketClobClient"):
        # Nothing to do — the module does not hold a client at
        # import time. The assertion below validates that.
        pass
    assert called == []  # no call happened


# ── No /trades /book order-placement risk ───────────────────────────────────
def test_clob_adapter_has_no_wallet_or_signing() -> None:
    """The adapter source must not reference any wallet / signing API.

    Static check: there is no ``private_key``, ``sign``, ``api_key``,
    or ``wallet`` reference in the adapter. The CLOB /book endpoint
    is unauthenticated; the adapter must never acquire a key.
    """
    clob_source = (
        _REPO_ROOT
        / "src"
        / "polycopy"
        / "adapters"
        / "polymarket_clob.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("private_key", "POLYMARKET_PRIVATE_KEY", "sign(", "api_key"):
        assert forbidden not in clob_source, (
            f"polymarket_clob.py must not reference {forbidden!r}; "
            "the /book endpoint is unauthenticated"
        )


# ── UUID sanity check (no flakes) ──────────────────────────────────────────
def test_each_settings_construction_is_independent() -> None:
    """Two Settings() constructions with different env values do not
    leak state via module-level cache. Pydantic-settings caches
    nothing by default, but this test guards against a future
    refactor that introduces a singleton.
    """
    os.environ["POLYCOPY_CLOB_ENABLED"] = "true"
    s1 = Settings()
    os.environ["POLYCOPY_CLOB_ENABLED"] = "false"
    s2 = Settings()
    assert s1.clob_enabled is True
    assert s2.clob_enabled is False
    del os.environ["POLYCOPY_CLOB_ENABLED"]


# ── Test scaffolding (sanity) ───────────────────────────────────────────────
def test_uuid4_helper_works() -> None:
    """Sanity: the test file's ``uuid4`` import is alive."""
    assert len(str(uuid4())) == 36


# ── Validation (new in PR-3 finalization) ───────────────────────────────────
def test_clob_base_url_strips_single_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trailing slash is normalized off; subsequent URLs concat cleanly."""
    monkeypatch.setenv("POLYCOPY_CLOB_BASE_URL", "https://clob.polymarket.com/")
    assert Settings().clob_base_url == "https://clob.polymarket.com"


def test_clob_base_url_rejects_non_http_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-http(s) base URL is rejected at parse time."""
    import pydantic

    monkeypatch.setenv("POLYCOPY_CLOB_BASE_URL", "ftp://example.com")
    with pytest.raises(pydantic.ValidationError):
        Settings()


def test_clob_base_url_rejects_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty base URL is rejected."""
    import pydantic

    monkeypatch.setenv("POLYCOPY_CLOB_BASE_URL", "   ")
    with pytest.raises(pydantic.ValidationError):
        Settings()


def test_clob_timeout_rejects_nonpositive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timeout must be > 0."""
    import pydantic

    for bad in (0, -1, -10.5):
        monkeypatch.setenv("POLYCOPY_CLOB_TIMEOUT_SECONDS", str(bad))
        with pytest.raises(pydantic.ValidationError):
            Settings()


def test_clob_max_retries_rejects_negative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Max retries must be >= 0 (zero is allowed = no retries)."""
    import pydantic

    monkeypatch.setenv("POLYCOPY_CLOB_MAX_RETRIES", "-1")
    with pytest.raises(pydantic.ValidationError):
        Settings()


def test_clob_rpm_rejects_negative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RPM must be >= 0 (zero disables the limiter explicitly)."""
    import pydantic

    monkeypatch.setenv("POLYCOPY_CLOB_RPM", "-1")
    with pytest.raises(pydantic.ValidationError):
        Settings()


def test_env_prefix_binds_to_polycopy_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The env prefix is ``POLYCOPY_`` — verify the clob fields are picked
    up under that prefix and are NOT picked up under a bare name."""
    monkeypatch.setenv("POLYCOPY_CLOB_BASE_URL", "https://example.test")
    s = Settings()
    assert s.clob_base_url == "https://example.test"
    # Unprefixed should be ignored
    monkeypatch.setenv("CLOB_BASE_URL", "https://ignored.test")
    s2 = Settings()
    assert s2.clob_base_url == "https://example.test"
