"""Tests for versioned config with fail-closed validation."""

import os
from unittest.mock import patch

import pytest

from polycopy.config.settings import BrokerMode, Settings, get_settings


class TestSettings:
    """Test Settings defaults and validation."""

    def test_defaults(self):
        s = Settings()
        assert s.broker_mode == BrokerMode.PAPER
        assert s.config_version == 1
        assert s.db_path.name == "polycopy.db"
        assert s.log_level == "INFO"
        assert s.snapshot_hash_algo == "sha256"
        assert s.http_timeout_seconds == 10.0

    def test_log_level_uppercased(self):
        s = Settings(log_level="debug")
        assert s.log_level == "DEBUG"

    def test_invalid_log_level(self):
        with pytest.raises(ValueError, match="log_level"):
            Settings(log_level="VERBOSE")

    def test_invalid_hash_algo(self):
        with pytest.raises(ValueError, match="snapshot_hash_algo"):
            Settings(snapshot_hash_algo="md999")

    def test_fail_closed_private_key_in_paper_mode(self):
        """Must reject private key when broker_mode=paper."""
        with pytest.raises(ValueError, match="polymarket_private_key"):
            Settings(broker_mode=BrokerMode.PAPER, polymarket_private_key="0xDEADBEEF")

    def test_polymarket_mode_allows_private_key(self):
        """When broker_mode=polymarket, private key is allowed."""
        s = Settings(broker_mode=BrokerMode.POLYMARKET, polymarket_private_key="0xDEADBEEF")
        assert s.polymarket_private_key == "0xDEADBEEF"

    def test_env_override(self):
        """Settings reads from POLYCOPY_ prefixed env vars."""
        with patch.dict(os.environ, {"POLYCOPY_BROKER_MODE": "polymarket", "POLYCOPY_LOG_LEVEL": "DEBUG"}):
            s = Settings()
            assert s.broker_mode == BrokerMode.POLYMARKET
            assert s.log_level == "DEBUG"

    def test_get_settings_caches(self):
        """get_settings returns the same instance on repeated calls."""
        s1 = get_settings(reload=True)
        s2 = get_settings()
        assert s1 is s2
