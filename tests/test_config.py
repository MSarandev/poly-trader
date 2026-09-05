"""PolyTraderConfig: from_env, with_overrides, validation, url normalisation."""
from __future__ import annotations

import pytest

from polytrader import PolyTraderConfig
from polytrader.config import (
    DEFAULT_CLOB_API_URL,
    DEFAULT_GAMMA_API_URL,
    POLYGON_CHAIN_ID,
    POLYGON_USDC_ADDRESS,
)
from polytrader.errors import ConfigError


def test_from_env_reads_prefixed_vars_with_types():
    env = {
        "POLYTRADER_BRIDGE_URL": "http://bridge.local:3000",
        "POLYTRADER_GAMMA_API_URL": "https://g.example",
        "POLYTRADER_CLOB_API_URL": "https://c.example",
        "POLYTRADER_REQUEST_TIMEOUT_S": "7.5",
        "POLYTRADER_MAX_RETRIES": "4",
        "POLYTRADER_RETRY_BACKOFF_S": "0.5",
        "POLYTRADER_DEFAULT_ORDER_TYPE": "FOK",
        "POLYTRADER_DEFAULT_TICK_SIZE": "0.001",
        "POLYTRADER_ALLOW_WITHDRAW": "true",
        "POLYTRADER_CHAIN_ID": "137",
        "POLYTRADER_DEPOSIT_ADDRESS": "0x" + "4" * 40,
    }
    cfg = PolyTraderConfig.from_env(env=env)
    assert cfg.bridge_url == "http://bridge.local:3000"
    assert cfg.gamma_api_url == "https://g.example"
    assert cfg.clob_api_url == "https://c.example"
    assert cfg.request_timeout_s == 7.5 and isinstance(cfg.request_timeout_s, float)
    assert cfg.max_retries == 4 and isinstance(cfg.max_retries, int)
    assert cfg.retry_backoff_s == 0.5
    assert cfg.default_order_type == "FOK"
    assert cfg.default_tick_size == "0.001"
    assert cfg.allow_withdraw is True
    assert cfg.chain_id == 137 and isinstance(cfg.chain_id, int)
    assert cfg.deposit_address == "0x" + "4" * 40


def test_from_env_custom_prefix():
    env = {"POLY_BRIDGE_URL": "http://x:1"}
    cfg = PolyTraderConfig.from_env(prefix="POLY_", env=env)
    assert cfg.bridge_url == "http://x:1"


def test_from_env_falls_back_to_defaults():
    cfg = PolyTraderConfig.from_env(env={})
    assert cfg.bridge_url == "http://localhost:3000"
    assert cfg.gamma_api_url == DEFAULT_GAMMA_API_URL
    assert cfg.clob_api_url == DEFAULT_CLOB_API_URL
    assert cfg.request_timeout_s == 10.0
    assert cfg.max_retries == 2
    assert cfg.default_order_type == "FAK"
    assert cfg.allow_withdraw is False
    assert cfg.chain_id == POLYGON_CHAIN_ID
    assert cfg.usdc_address == POLYGON_USDC_ADDRESS


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("1", True), ("yes", True), ("on", True), ("TRUE", True),
    ("false", False), ("0", False), ("no", False), ("off", False), ("", False),
])
def test_from_env_bool_parsing(raw, expected):
    cfg = PolyTraderConfig.from_env(env={"POLYTRADER_ALLOW_WITHDRAW": raw})
    assert cfg.allow_withdraw is expected


def test_with_overrides_replaces_fields():
    cfg = PolyTraderConfig(bridge_url="http://a").with_overrides(max_retries=9)
    assert cfg.max_retries == 9
    assert cfg.bridge_url == "http://a"


def test_with_overrides_unknown_field_raises():
    with pytest.raises(ConfigError):
        PolyTraderConfig().with_overrides(nonsense=1)


def test_urls_are_normalised_stripping_trailing_slash():
    cfg = PolyTraderConfig(
        bridge_url="http://b/",
        gamma_api_url="https://g/",
        clob_api_url="https://c/",
    )
    assert cfg.bridge_url == "http://b"
    assert cfg.gamma_api_url == "https://g"
    assert cfg.clob_api_url == "https://c"


def test_validation_rejects_bad_values():
    with pytest.raises(ConfigError):
        PolyTraderConfig(bridge_url="")
    with pytest.raises(ConfigError):
        PolyTraderConfig(request_timeout_s=0)
    with pytest.raises(ConfigError):
        PolyTraderConfig(max_retries=-1)
    with pytest.raises(ConfigError):
        PolyTraderConfig(retry_backoff_s=-0.1)
