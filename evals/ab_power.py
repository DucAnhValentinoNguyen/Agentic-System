"""How big could an online A/B test of Twin be? Run: uv run python evals/ab_power.py [baseline] [weekly_visitors]

Baseline numbers come from the turns log (see docs/experiment.md): of the visitors who started booking, about a third
finished. Real traffic is small, which is the point of the exercise: it decides what can and cannot be tested online.
"""

import sys

from ab_stats import (
    false_positive_rate_with_peeking,
    minimum_detectable_effect,
    sample_size_per_arm,
    simulated_power,
    wilson,
)

base = float(sys.argv[1]) if len(sys.argv) > 1 else 0.31
weekly = int(sys.argv[2]) if len(sys.argv) > 2 else 12          # genuine visitors who start a booking per week (a guess: ask the logs)
print(f"baseline completion {base:.0%}; about {weekly} booking visitors a week\n")
print("lift to detect  ->  visitors per arm  ->  weeks of traffic")
for lift in (0.30, 0.20, 0.10, 0.05):
    n = sample_size_per_arm(base, lift)
    print(f"  +{lift:.0%} points       {n:>8}             {2 * n / weekly:>6.0f}")
print(f"\nWith 4 weeks of traffic ({weekly * 4 // 2} per arm) the smallest detectable lift is "
      f"+{minimum_detectable_effect(base, weekly * 4 // 2):.0%} points: only a huge effect can show up.")
print(f"Wilson 95% interval for 5 bookings of 16 starters: {tuple(round(x, 2) for x in wilson(5, 16))}")
print(f"Simulated power at +20 points with 30 per arm: {simulated_power(base, 0.20, 30):.0%} (target 80%)")
print(f"Peeking: an A/A test looked at 10 times and stopped at the first p<0.05 is 'significant' "
      f"{false_positive_rate_with_peeking(base, 200, 10):.0%} of the time (nominal 5%)")
