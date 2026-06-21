"""Runs the offline Test Clock validation harness as a regression test, so the
six-layer diffs stay green in CI. The harness itself lives in harness/ and is the
deliverable; this just asserts it reports zero failures."""

from harness.revenue_testclock_harness import run_offline


def test_harness_all_layers_pass():
    assert run_offline() == 0
