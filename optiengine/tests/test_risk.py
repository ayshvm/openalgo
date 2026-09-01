"""
Tests for the risk layer.

These run with no broker, no network and no market data — that is the point of
keeping Guardian pure. Run with:  python3 -m unittest optiengine.tests.test_risk
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, timedelta

from optiengine.risk.guardian import Guardian, ProposedEntry
from optiengine.risk.killswitch import KillSwitch
from optiengine.risk.limits import (DEFAULT_CONFIG, RiskConfigError, RiskLimits,
                                    _parse_risk_block_fallback, load_limits)


def limits(**over) -> RiskLimits:
    base = dict(max_capital=100_000_000, deploy_fraction=0.6,
                daily_loss_cap_pct=1.5, per_structure_max_loss_pct=0.5,
                max_orders_per_second=8, max_lots_liquidity=200)
    base.update(over)
    return RiskLimits(**base)


class FakeClock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def advance(self, dt): self.t += dt


class Flat:
    """Flattener that banks a fixed P&L, or raises for chosen underlyings."""
    def __init__(self, pnl=0.0, fail_on=()): self.pnl, self.fail_on = pnl, set(fail_on)
    def flatten(self, s):
        if s.underlying in self.fail_on:
            raise RuntimeError("broker rejected close")
        return self.pnl


class TestLimits(unittest.TestCase):
    def test_loads_the_real_shipped_config(self):
        lim = load_limits(DEFAULT_CONFIG)
        self.assertEqual(lim.max_capital, 100_000_000)
        self.assertEqual(lim.deployable_capital, 60_000_000)
        self.assertAlmostEqual(lim.daily_loss_cap(), 900_000)
        self.assertAlmostEqual(lim.per_structure_max_loss(), 300_000)

    def test_fallback_parser_agrees_with_the_shipped_config(self):
        with open(DEFAULT_CONFIG, encoding="utf-8") as fh:
            raw = _parse_risk_block_fallback(fh.read())
        self.assertEqual(int(raw["max_capital"]), 100_000_000)
        self.assertEqual(float(raw["deploy_fraction"]), 0.6)
        self.assertEqual(int(raw["max_lots_liquidity"]), 200)
        self.assertNotIn("strategies", raw)  # must not leak the other top-level block

    def test_rejects_structure_cap_larger_than_daily_cap(self):
        # Otherwise one structure can blow the daily cap and the cap means nothing.
        with self.assertRaises(RiskConfigError):
            limits(per_structure_max_loss_pct=2.0).validate()

    def test_rejects_sebi_order_rate_threshold(self):
        with self.assertRaises(RiskConfigError):
            limits(max_orders_per_second=10).validate()

    def test_rejects_over_deployment(self):
        with self.assertRaises(RiskConfigError):
            limits(deploy_fraction=1.5).validate()

    def test_unknown_key_is_an_error_not_a_shrug(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.yaml")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("risk:\n  max_capital: 1000\n  daily_loss_cap_pctt: 1.5\n")
            with self.assertRaises(RiskConfigError):
                load_limits(p)

    def test_missing_block_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.yaml")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("strategies: []\n")
            with self.assertRaises(RiskConfigError):
                load_limits(p)


class TestGuardianEntry(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.g = Guardian(limits(), clock=self.clock)

    def entry(self, **over):
        base = dict(underlying="NIFTY", lots=10, n_orders=4,
                    margin_required=1_000_000, structural_max_loss=250_000)
        base.update(over)
        return ProposedEntry(**base)

    def test_allows_a_sane_structure(self):
        self.assertTrue(self.g.check_entry(self.entry()))

    def test_blocks_margin_past_deploy_fraction(self):
        self.g.open_structure(self.entry(margin_required=59_000_000,
                                         structural_max_loss=100_000))
        v = self.g.check_entry(self.entry(margin_required=2_000_000))
        self.assertFalse(v.allowed)
        self.assertTrue(any("capital" in r for r in v.reasons))

    def test_blocks_past_the_liquidity_lot_cap(self):
        # 200-lot cap is per instrument and counts what is already on the book.
        self.g.open_structure(self.entry(lots=195, margin_required=1_000))
        v = self.g.check_entry(self.entry(lots=10))
        self.assertFalse(v.allowed)
        self.assertTrue(any("liquidity" in r for r in v.reasons))

    def test_lot_cap_is_per_instrument(self):
        self.g.open_structure(self.entry(underlying="BANKNIFTY", lots=195,
                                         margin_required=1_000))
        self.assertTrue(self.g.check_entry(self.entry(underlying="NIFTY", lots=10)))

    def test_blocks_structure_larger_than_per_structure_cap(self):
        v = self.g.check_entry(self.entry(structural_max_loss=300_001))
        self.assertFalse(v.allowed)
        self.assertTrue(any("per-structure" in r for r in v.reasons))

    def test_blocks_entry_once_daily_cap_is_already_hit(self):
        self.g.realized_pnl_today = -900_000
        v = self.g.check_entry(self.entry())
        self.assertFalse(v.allowed)
        self.assertTrue(any("already reached" in r for r in v.reasons))

    def test_blocks_entry_whose_worst_case_would_breach_the_daily_cap(self):
        self.g.realized_pnl_today = -700_000     # cap is -900,000
        v = self.g.check_entry(self.entry(structural_max_loss=250_000))
        self.assertFalse(v.allowed)
        self.assertTrue(any("worst case" in r for r in v.reasons))

    def test_order_rate_throttle_and_recovery(self):
        self.g.record_fill(6)                     # 6 of 8 used this second
        v = self.g.check_entry(self.entry(n_orders=4))
        self.assertFalse(v.allowed)
        self.assertTrue(any("order rate" in r for r in v.reasons))
        self.clock.advance(1.01)                  # window rolls off
        self.assertTrue(self.g.check_entry(self.entry(n_orders=4)))

    def test_halted_guardian_refuses_everything(self):
        self.g.halt("operator pulled the plug")
        self.assertFalse(self.g.check_entry(self.entry()))

    def test_reports_every_reason_not_just_the_first(self):
        v = self.g.check_entry(self.entry(lots=500, margin_required=90_000_000,
                                          structural_max_loss=5_000_000))
        self.assertFalse(v.allowed)
        self.assertGreaterEqual(len(v.reasons), 3)

    def test_rejects_nonsense_input(self):
        self.assertFalse(self.g.check_entry(self.entry(lots=0)))
        self.assertFalse(self.g.check_entry(self.entry(margin_required=-1)))

    def test_booking_a_denied_structure_raises(self):
        with self.assertRaises(RuntimeError):
            self.g.open_structure(self.entry(structural_max_loss=10_000_000))

    def test_peak_deployed_basis_is_stricter_early(self):
        g = Guardian(limits(loss_cap_basis="peak_deployed"), clock=FakeClock())
        # Nothing deployed yet, so the basis comes from this entry's own margin:
        # 1,000,000 * 0.5% = 5,000 — far below a 250,000 structural loss.
        v = g.check_entry(self.entry())
        self.assertFalse(v.allowed)
        self.assertTrue(any("per-structure" in r for r in v.reasons))


class TestGuardianMonitoring(unittest.TestCase):
    def setUp(self):
        self.g = Guardian(limits(), clock=FakeClock())
        self.s = self.g.open_structure(ProposedEntry("NIFTY", 10, 4, 1_000_000, 250_000))

    def test_no_breach_inside_the_cap(self):
        self.assertIsNone(self.g.mark_to_market({id(self.s): -100_000}))

    def test_daily_cap_breach_halts_and_demands_flatten(self):
        b = self.g.mark_to_market({id(self.s): -900_000})
        self.assertIsNotNone(b)
        self.assertEqual(b.kind, "daily_loss_cap")
        self.assertTrue(b.flatten)
        self.assertTrue(self.g.halted)

    def test_realized_and_unrealized_are_summed_for_the_cap(self):
        self.g.realized_pnl_today = -500_000
        self.assertIsNotNone(self.g.mark_to_market({id(self.s): -400_000}))

    def test_margin_growth_after_entry_is_flagged_without_flattening(self):
        self.s.margin = 61_000_000            # broker re-rated margin intraday
        b = self.g.mark_to_market()
        self.assertEqual(b.kind, "capital_deploy")
        self.assertFalse(b.flatten)           # reduce size, don't panic-close

    def test_mark_for_unknown_structure_fails_closed(self):
        b = self.g.mark_to_market({999999: -1.0})
        self.assertIsNotNone(b)
        self.assertTrue(self.g.halted)


class TestKillSwitch(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "halt.json")
        self.g = Guardian(limits(), clock=FakeClock())

    def test_trip_flattens_everything_and_banks_pnl(self):
        self.g.open_structure(ProposedEntry("NIFTY", 10, 4, 1_000_000, 250_000))
        self.g.open_structure(ProposedEntry("NIFTY", 5, 4, 500_000, 100_000))
        ks = KillSwitch(self.g, Flat(pnl=-50_000), self.path, today=date(2026, 8, 31))
        r = ks.trip("daily cap")
        self.assertTrue(r.clean)
        self.assertEqual(r.closed, 2)
        self.assertEqual(r.realized_pnl, -100_000)
        self.assertEqual(self.g.open_structures, [])
        self.assertTrue(self.g.halted)

    def test_halt_survives_a_restart_on_the_same_day(self):
        today = date(2026, 8, 31)
        KillSwitch(self.g, Flat(), self.path, today=today).trip("cap breached")
        fresh = Guardian(limits(), clock=FakeClock())
        KillSwitch(fresh, Flat(), self.path, today=today)
        self.assertTrue(fresh.halted)         # a crash-loop cannot clear the halt

    def test_halt_does_not_leak_into_the_next_session(self):
        today = date(2026, 8, 31)
        KillSwitch(self.g, Flat(), self.path, today=today).trip("cap breached")
        fresh = Guardian(limits(), clock=FakeClock())
        KillSwitch(fresh, Flat(), self.path, today=today + timedelta(days=1))
        self.assertFalse(fresh.halted)

    def test_failed_leg_is_reported_and_halt_stands(self):
        self.g.open_structure(ProposedEntry("NIFTY", 10, 4, 1_000_000, 250_000))
        self.g.open_structure(ProposedEntry("BANKNIFTY", 5, 4, 500_000, 100_000))
        ks = KillSwitch(self.g, Flat(fail_on=["BANKNIFTY"]), self.path,
                        today=date(2026, 8, 31))
        r = ks.trip("cap breached")
        self.assertFalse(r.clean)
        self.assertEqual(r.closed, 1)
        self.assertEqual(len(self.g.open_structures), 1)   # the naked leg is left visible
        self.assertTrue(self.g.halted)

    def test_state_file_is_written_atomically_and_readably(self):
        KillSwitch(self.g, Flat(), self.path, today=date(2026, 8, 31)).trip("why")
        with open(self.path, encoding="utf-8") as fh:
            state = json.load(fh)
        self.assertEqual(state, {"date": "2026-08-31", "halted": True, "reason": "why"})
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_clear_is_explicit(self):
        ks = KillSwitch(self.g, Flat(), self.path, today=date(2026, 8, 31))
        ks.trip("cap")
        ks.clear()
        self.assertFalse(self.g.halted)
        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
