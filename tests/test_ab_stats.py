"""The A/B statistics, checked against textbook values."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "evals"))
import ab_stats as s


def test_wilson_interval_matches_a_known_value_and_stays_inside_0_1():
    lo, hi = s.wilson(5, 16)
    assert abs(lo - 0.142) < 0.005 and abs(hi - 0.559) < 0.005
    assert s.wilson(0, 10)[0] == 0 or s.wilson(0, 10)[0] < 1e-9
    assert 0 <= s.wilson(10, 10)[0] and s.wilson(10, 10)[1] <= 1 + 1e-9


def test_sample_size_matches_the_classic_formula():
    # 10% -> 12% needs about 3,800 per arm at alpha .05, power .8 (standard calculators give ~3,841)
    assert 3700 < s.sample_size_per_arm(0.10, 0.02) < 3950
    assert s.sample_size_per_arm(0.5, 0.05) > s.sample_size_per_arm(0.5, 0.10)       # smaller effect, more visitors


def test_mde_is_the_inverse_of_sample_size():
    n = s.sample_size_per_arm(0.3, 0.08)
    assert abs(s.minimum_detectable_effect(0.3, n) - 0.08) < 0.002


def test_two_proportion_z_known_case_and_no_difference_case():
    r = s.two_proportion_z(100, 1000, 130, 1000)
    assert r["diff"] == 0.03 and 0.03 < r["p"] < 0.06 and r["ci"][0] < 0 + 0.04
    assert s.two_proportion_z(50, 100, 50, 100)["p"] > 0.99


def test_srm_flags_a_broken_split_but_not_a_normal_one():
    assert s.srm_p(5000, 5100) > 0.3
    assert s.srm_p(5000, 5600) < 0.001


def test_mcnemar_exact_on_the_context_experiment_is_not_significant_at_five_percent():
    # 13/18 vs 18/18 with every old success kept: 5 pairs better, 0 worse: p = 2/32
    assert abs(s.mcnemar_exact(0, 5) - 0.0625) < 1e-9
    assert s.mcnemar_exact(0, 6) < 0.05 and s.mcnemar_exact(3, 3) == 1.0


def test_holm_is_never_smaller_than_the_raw_p_and_keeps_order():
    raw = [0.01, 0.04, 0.03]
    adj = s.holm(raw)
    assert all(a >= r for a, r in zip(adj, raw, strict=True)) and adj[0] == 0.03


def test_cluster_bootstrap_widens_when_visitors_send_several_messages():
    a = {f"a{i}": [1, 1, 1] if i % 2 else [0, 0, 0] for i in range(10)}      # outcomes identical within a visitor
    b = {f"b{i}": [1, 1, 1] if i % 3 else [0, 0, 0] for i in range(10)}
    r = s.cluster_bootstrap_diff(a, b, reps=500)
    assert r["ci"][0] < r["diff"] < r["ci"][1] and (r["ci"][1] - r["ci"][0]) > 0.3


def test_peeking_inflates_false_positives_and_power_is_low_for_small_n():
    assert s.false_positive_rate_with_peeking(0.3, 100, looks=10, reps=300) > 0.10
    assert s.simulated_power(0.3, 0.1, 30, reps=300) < 0.3
