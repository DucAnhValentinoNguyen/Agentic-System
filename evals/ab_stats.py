"""The statistics behind an A/B test, in plain Python (no scipy), so every line can be explained in an interview.

Everything here is for binary outcomes (booked / did not, answer supported / not, thumbs up / not) because that is what
Twin produces. Unit of analysis = the visitor (session), not the message: messages of one visitor are not independent.
"""

import math
import random
from statistics import NormalDist

N = NormalDist()


def wilson(k: int, n: int, level: float = 0.95) -> tuple[float, float]:
    """Wilson score interval for a proportion. Better than the textbook p +- 1.96 se when n is small or p near 0/1."""
    if n == 0:
        return 0.0, 1.0
    z, p = N.inv_cdf(0.5 + level / 2), k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return centre - half, centre + half


def two_proportion_z(k1: int, n1: int, k2: int, n2: int) -> dict:
    """Two-sided z test of H0: p1 == p2, with a 95% interval for the difference p2 - p1 (unpooled)."""
    p1, p2 = k1 / n1, k2 / n2
    pooled = (k1 + k2) / (n1 + n2)
    se0 = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2)) or 1e-12
    z = (p2 - p1) / se0
    se = math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
    return {"diff": p2 - p1, "z": z, "p": 2 * (1 - N.cdf(abs(z))),
            "ci": (p2 - p1 - 1.96 * se, p2 - p1 + 1.96 * se)}


def sample_size_per_arm(p_base: float, mde: float, alpha: float = 0.05, power: float = 0.8) -> int:
    """Visitors needed in EACH arm to detect an absolute lift `mde` over baseline `p_base` (two-sided z test)."""
    p2 = p_base + mde
    za, zb = N.inv_cdf(1 - alpha / 2), N.inv_cdf(power)
    pbar = (p_base + p2) / 2
    num = (za * math.sqrt(2 * pbar * (1 - pbar)) + zb * math.sqrt(p_base * (1 - p_base) + p2 * (1 - p2))) ** 2
    return math.ceil(num / mde**2)


def minimum_detectable_effect(p_base: float, n_per_arm: int, alpha: float = 0.05, power: float = 0.8) -> float:
    """The smallest absolute lift this many visitors per arm can detect (bisection on sample_size_per_arm)."""
    lo, hi = 1e-4, 1 - p_base - 1e-4
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (lo, mid) if sample_size_per_arm(p_base, mid, alpha, power) <= n_per_arm else (mid, hi)
    return hi


def srm_p(n_a: int, n_b: int, expected_a: float = 0.5) -> float:
    """Sample ratio mismatch: p-value that the observed split is what the randomiser should give. A tiny p means the
    experiment is broken (bots, a crash in one arm, a redirect), and the results must not be read."""
    n = n_a + n_b
    exp_a, exp_b = n * expected_a, n * (1 - expected_a)
    chi2 = (n_a - exp_a) ** 2 / exp_a + (n_b - exp_b) ** 2 / exp_b
    return 2 * (1 - N.cdf(math.sqrt(chi2)))              # chi-square with 1 df is a squared standard normal


def mcnemar_exact(only_a: int, only_b: int) -> float:
    """Paired binary outcomes (same question answered by A and by B): two-sided exact test on the discordant pairs."""
    n, k = only_a + only_b, min(only_a, only_b)
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def cluster_bootstrap_diff(a: dict[str, list[int]], b: dict[str, list[int]], reps: int = 4000, seed: int = 0) -> dict:
    """Difference in mean outcome (b - a), resampling whole visitors, so several messages of one visitor do not
    pretend to be independent evidence. Inputs: {visitor_id: [0/1 outcomes]}."""
    rng = random.Random(seed)
    ka, kb = list(a), list(b)

    def mean(groups: dict[str, list[int]], keys: list[str]) -> float:
        flat = [x for k in keys for x in groups[k]]
        return sum(flat) / len(flat)

    diffs = sorted(mean(b, [rng.choice(kb) for _ in kb]) - mean(a, [rng.choice(ka) for _ in ka]) for _ in range(reps))
    return {"diff": mean(b, kb) - mean(a, ka), "ci": (diffs[int(0.025 * reps)], diffs[int(0.975 * reps) - 1])}


def holm(pvalues: list[float]) -> list[float]:
    """Holm-Bonferroni adjusted p-values: the honest answer when you looked at several metrics or several variants."""
    order = sorted(range(len(pvalues)), key=lambda i: pvalues[i])
    adj, running = [0.0] * len(pvalues), 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (len(pvalues) - rank) * pvalues[i]))
        adj[i] = running
    return adj


def simulated_power(p_base: float, lift: float, n_per_arm: int, reps: int = 2000, seed: int = 0) -> float:
    """Power by simulation: how often a true lift is declared significant at 5% with this many visitors per arm."""
    rng, hits = random.Random(seed), 0
    for _ in range(reps):
        ka = sum(rng.random() < p_base for _ in range(n_per_arm))
        kb = sum(rng.random() < p_base + lift for _ in range(n_per_arm))
        hits += two_proportion_z(ka, n_per_arm, kb, n_per_arm)["p"] < 0.05
    return hits / reps


def false_positive_rate_with_peeking(p: float, n_per_arm: int, looks: int, reps: int = 2000, seed: int = 0) -> float:
    """A/A tests (no true difference) checked `looks` times along the way and stopped at the first p < 0.05:
    how often do we wrongly 'find' an effect? Shows why you fix the sample size in advance."""
    rng, wrong = random.Random(seed), 0
    for _ in range(reps):
        a = [rng.random() < p for _ in range(n_per_arm)]
        b = [rng.random() < p for _ in range(n_per_arm)]
        for look in range(1, looks + 1):
            n = n_per_arm * look // looks
            ka, kb = sum(a[:n]), sum(b[:n])
            if 0 < ka + kb < 2 * n and two_proportion_z(ka, n, kb, n)["p"] < 0.05:
                wrong += 1
                break
    return wrong / reps
