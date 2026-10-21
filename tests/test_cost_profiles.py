"""Cost profiles (investment-policy §6, 2026-10-19): named baselines and mandate wiring."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from quant_research.backtest.costs import (
    AUTHORITATIVE_PROFILE,
    COST_PROFILES,
    RETAIL_TAKER_PROFILE,
    cost_config,
    profile_prefix,
)
from quant_research.backtest.engine import EngineConfig
from quant_research.research.mandate import Mandate
from tests.conftest import make_mandate


def test_authoritative_profile_numerics():
    c = cost_config(AUTHORITATIVE_PROFILE)
    assert c.fee_rate == 0.002 and c.slippage_bps == 2.0
    assert c.source.startswith(profile_prefix(AUTHORITATIVE_PROFILE))
    assert "NOT a maker" in c.source or "not a maker" in c.source.lower()


def test_retail_taker_profile_matches_prior_baseline():
    c = cost_config(RETAIL_TAKER_PROFILE)
    assert c.fee_rate == 0.006 and c.slippage_bps == 5.0
    assert c.source.startswith(profile_prefix(RETAIL_TAKER_PROFILE))


def test_engine_default_is_authoritative_allin():
    cfg = EngineConfig()
    assert cfg.costs.fee_rate == 0.002 and cfg.costs.slippage_bps == 2.0
    assert AUTHORITATIVE_PROFILE in cfg.costs.source


def test_costs_x2_scales_authoritative_baseline():
    base = EngineConfig()
    x2 = base.sensitivity("costs_x2")
    assert x2.costs.fee_rate == 0.004 and x2.costs.slippage_bps == 4.0


def test_mandate_accepts_matching_cost_profile(dataset):
    costs = cost_config(AUTHORITATIVE_PROFILE)
    m = make_mandate(dataset, cost_profile=AUTHORITATIVE_PROFILE,
                     engine_config=EngineConfig(costs=costs).model_dump(mode="json"))
    assert m.cost_profile == AUTHORITATIVE_PROFILE
    assert m.engine_config.costs.fee_rate == 0.002


def test_mandate_rejects_mismatched_cost_profile(dataset):
    with pytest.raises(ValidationError, match="cost_profile"):
        make_mandate(dataset, cost_profile=AUTHORITATIVE_PROFILE,
                     engine_config=EngineConfig(costs=cost_config(RETAIL_TAKER_PROFILE)).model_dump(mode="json"))


def test_legacy_mandate_without_cost_profile_keeps_stable_id(dataset):
    m = make_mandate(dataset)  # no cost_profile key
    body = m.model_dump(mode="json")
    assert body.get("cost_profile") is None
    # Round-trip: id must not change when the optional field is absent/None
    assert Mandate.model_validate(body).mandate_id() == m.mandate_id()
    # Body without the key at all (as stored before the field existed)
    legacy = {k: v for k, v in body.items() if k != "cost_profile"}
    assert Mandate.model_validate(legacy).mandate_id() == m.mandate_id()


def test_cost_profile_changes_mandate_id(dataset):
    a = make_mandate(dataset, cost_profile=AUTHORITATIVE_PROFILE,
                     engine_config=EngineConfig(costs=cost_config(AUTHORITATIVE_PROFILE)).model_dump(mode="json"))
    b = make_mandate(dataset, cost_profile=RETAIL_TAKER_PROFILE,
                     engine_config=EngineConfig(costs=cost_config(RETAIL_TAKER_PROFILE)).model_dump(mode="json"))
    assert a.mandate_id() != b.mandate_id()


def test_known_profiles_are_exactly_two():
    assert set(COST_PROFILES) == {AUTHORITATIVE_PROFILE, RETAIL_TAKER_PROFILE}
