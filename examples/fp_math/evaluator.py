"""
Evaluator for floating-point sin() implementations.

Measures three axes:
  1. Precision (ULP error) - weight 0.5
  2. Correct rounding - weight 0.3
  3. Speed - weight 0.2
"""

import math
import struct
import time
import random
import importlib.util
import sys
import traceback

import mpmath


# ---------------------------------------------------------------------------
# ULP helpers
# ---------------------------------------------------------------------------

def ulp_error(result, expected):
    """Compute ULP error between result and expected float64 values.

    For |expected| >= 2^-970 (well above subnormals), uses standard relative
    ULP: |result - expected| / ULP(expected).

    For |expected| < 2^-970 (near zero), measures the absolute error in ULPs
    of the smallest normal (2^-1022), since near zero the absolute error matters
    more than relative precision.
    """
    if math.isnan(expected):
        return 0.0 if math.isnan(result) else float("inf")
    if math.isinf(expected):
        return 0.0 if result == expected else float("inf")
    if math.isnan(result) or math.isinf(result):
        return float("inf")
    if result == expected:
        return 0.0

    diff = abs(result - expected)
    aexp = abs(expected)

    # Near-zero threshold: use absolute error scaled by ULP of smallest normal
    NEAR_ZERO = 2.0 ** -970
    if aexp < NEAR_ZERO:
        # 1 ULP of smallest normal = 2^-1074
        one_ulp = math.ldexp(1.0, -1074)
        return diff / one_ulp if one_ulp > 0 else (0.0 if diff == 0 else float("inf"))

    # Standard ULP error relative to expected
    _, exp = math.frexp(aexp)
    one_ulp = math.ldexp(1.0, exp - 53)
    return diff / one_ulp


# ---------------------------------------------------------------------------
# Test-point generation
# ---------------------------------------------------------------------------

def generate_test_points(n_total=10000, seed=42):
    """Generate a comprehensive set of test points for sin()."""
    rng = random.Random(seed)
    points = []

    # Uniform grid over [-2pi, 2pi]
    n_grid = 5000
    for i in range(n_grid):
        t = -2 * math.pi + 4 * math.pi * i / (n_grid - 1)
        points.append(t)

    # Near-zero values
    for _ in range(1000):
        points.append(rng.uniform(-1e-10, 1e-10))

    # Large values (tests range reduction)
    for _ in range(1000):
        sign = rng.choice([-1, 1])
        points.append(sign * 10 ** rng.uniform(3, 15))

    # Special values
    specials = [
        0.0, -0.0,
        math.pi / 6, -math.pi / 6,
        math.pi / 4, -math.pi / 4,
        math.pi / 3, -math.pi / 3,
        math.pi / 2, -math.pi / 2,
        math.pi, -math.pi,
        2 * math.pi, -2 * math.pi,
        1e-300, -1e-300,  # tiny denormal-ish
        5e-324, -5e-324,  # smallest subnormal
    ]
    points.extend(specials)

    # Random values in [-1e6, 1e6]
    remaining = n_total - len(points)
    if remaining < 0:
        remaining = 0
    for _ in range(remaining):
        points.append(rng.uniform(-1e6, 1e6))

    return points


def generate_quick_test_points(n=200, seed=99):
    """Small set for stage-1 quick filter. Kept to moderate range."""
    rng = random.Random(seed)
    pts = []
    # Grid over [-2pi, 2pi]
    for i in range(100):
        pts.append(-2 * math.pi + 4 * math.pi * i / 99)
    # Near zero
    for _ in range(30):
        pts.append(rng.uniform(-1e-8, 1e-8))
    # Moderate values (not extreme - save that for stage 2)
    for _ in range(30):
        pts.append(rng.choice([-1, 1]) * rng.uniform(10, 1000))
    # Random
    for _ in range(40):
        pts.append(rng.uniform(-100, 100))
    return pts


# ---------------------------------------------------------------------------
# Reference values via mpmath
# ---------------------------------------------------------------------------

def reference_sin(x):
    """Compute correctly-rounded float64 sin(x) using mpmath."""
    mpmath.mp.dps = 100  # 100 decimal digits
    return float(mpmath.sin(mpmath.mpf(x)))


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def precision_score_from_max_ulp(max_ulp):
    """Score in [0, 1]. Higher is better. 1.0 means perfectly rounded."""
    if max_ulp == float("inf"):
        return 0.0
    return 1.0 / (1.0 + math.log2(1.0 + max_ulp))


def speed_score_from_times(impl_time, baseline_time):
    """Score in [0, 1]. 1.0 means as fast or faster than math.sin."""
    if impl_time <= 0:
        return 1.0
    return min(1.0, baseline_time / impl_time)


# ---------------------------------------------------------------------------
# Core evaluation logic
# ---------------------------------------------------------------------------

def evaluate_precision_and_rounding(fn, points):
    """
    Returns (max_ulp, mean_ulp, correctly_rounded_frac, errors).
    errors is a list of (x, result, expected, ulp) for worst cases.
    """
    ulps = []
    correctly_rounded = 0
    worst_cases = []

    for x in points:
        try:
            result = fn(x)
        except Exception:
            ulps.append(float("inf"))
            continue

        if not isinstance(result, (int, float)):
            ulps.append(float("inf"))
            continue

        expected = reference_sin(x)
        u = ulp_error(float(result), expected)
        ulps.append(u)

        if float(result) == expected:
            correctly_rounded += 1

        if u > 10:
            worst_cases.append({"x": x, "result": float(result), "expected": expected, "ulp": u})

    if not ulps:
        return float("inf"), float("inf"), 0.0, []

    max_ulp = max(ulps)
    # Filter out inf for mean computation
    finite_ulps = [u for u in ulps if u != float("inf")]
    mean_ulp = sum(finite_ulps) / len(finite_ulps) if finite_ulps else float("inf")
    frac = correctly_rounded / len(points) if points else 0.0

    # Keep only top-10 worst cases
    worst_cases.sort(key=lambda w: w["ulp"], reverse=True)
    return max_ulp, mean_ulp, frac, worst_cases[:10]


def evaluate_speed(fn, n_calls=100000, repeats=3):
    """Time the function, return (impl_time, baseline_time) in seconds per call."""
    # Build a fixed set of inputs
    rng = random.Random(123)
    inputs = [rng.uniform(-10, 10) for _ in range(n_calls)]

    # Baseline: math.sin
    best_baseline = float("inf")
    for _ in range(repeats):
        t0 = time.perf_counter()
        for v in inputs:
            math.sin(v)
        elapsed = time.perf_counter() - t0
        best_baseline = min(best_baseline, elapsed)

    # Implementation
    best_impl = float("inf")
    for _ in range(repeats):
        t0 = time.perf_counter()
        for v in inputs:
            fn(v)
        elapsed = time.perf_counter() - t0
        best_impl = min(best_impl, elapsed)

    return best_impl / n_calls, best_baseline / n_calls


# ---------------------------------------------------------------------------
# Stage 1: Quick filter
# ---------------------------------------------------------------------------

def stage1_evaluate(fn):
    """Quick sanity check. Returns (pass, score, artifacts)."""
    artifacts = {}
    points = generate_quick_test_points()

    max_ulp, mean_ulp, rounding_frac, worst = evaluate_precision_and_rounding(fn, points)
    artifacts["stage1_max_ulp"] = max_ulp
    artifacts["stage1_mean_ulp"] = mean_ulp
    artifacts["stage1_rounding_frac"] = rounding_frac

    if max_ulp == float("inf"):
        artifacts["stage1_error"] = "Produced NaN/Inf results"
        return False, 0.0, artifacts

    # Rough score for stage 1
    prec = precision_score_from_max_ulp(max_ulp)
    score = 0.5 * prec + 0.3 * rounding_frac + 0.2 * 0.5  # assume average speed
    artifacts["stage1_score"] = score
    return True, score, artifacts


# ---------------------------------------------------------------------------
# Stage 2: Full evaluation
# ---------------------------------------------------------------------------

def stage2_evaluate(fn):
    """Full evaluation. Returns (score, artifacts)."""
    artifacts = {}

    # Precision & rounding
    points = generate_test_points()
    max_ulp, mean_ulp, rounding_frac, worst = evaluate_precision_and_rounding(fn, points)

    prec = precision_score_from_max_ulp(max_ulp)
    artifacts["max_ulp"] = max_ulp
    artifacts["mean_ulp"] = mean_ulp
    artifacts["precision_score"] = prec
    artifacts["rounding_score"] = rounding_frac
    artifacts["worst_cases"] = worst[:5]

    # Speed
    impl_time, baseline_time = evaluate_speed(fn)
    spd = speed_score_from_times(impl_time, baseline_time)
    artifacts["impl_time_per_call_ns"] = impl_time * 1e9
    artifacts["baseline_time_per_call_ns"] = baseline_time * 1e9
    artifacts["speed_score"] = spd

    # Combined
    combined = 0.5 * prec + 0.3 * rounding_frac + 0.2 * spd
    artifacts["combined_score"] = combined

    return combined, artifacts


# ---------------------------------------------------------------------------
# Main evaluator entry point (called by OpenEvolve)
# ---------------------------------------------------------------------------

def evaluate(program_path):
    """
    OpenEvolve evaluator entry point.

    Args:
        program_path: path to the evolved program file.

    Returns:
        dict with "score" (float) and "artifacts" (dict).
    """
    artifacts = {}

    # Load the evolved module
    try:
        spec = importlib.util.spec_from_file_location("evolved_program", program_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:
        artifacts["error"] = f"Failed to load program: {e}\n{traceback.format_exc()}"
        return {"score": 0.0, "artifacts": artifacts}

    # Get the sin function
    try:
        fn = module.run()
    except Exception as e:
        artifacts["error"] = f"run() failed: {e}\n{traceback.format_exc()}"
        return {"score": 0.0, "artifacts": artifacts}

    if not callable(fn):
        artifacts["error"] = "run() did not return a callable"
        return {"score": 0.0, "artifacts": artifacts}

    # Stage 1: quick filter
    try:
        passed, s1_score, s1_artifacts = stage1_evaluate(fn)
        artifacts.update(s1_artifacts)
    except Exception as e:
        artifacts["error"] = f"Stage 1 exception: {e}\n{traceback.format_exc()}"
        return {"score": 0.0, "artifacts": artifacts}

    if not passed:
        artifacts["stage"] = "rejected_at_stage1"
        return {"score": s1_score, "artifacts": artifacts}

    # Stage 2: full evaluation
    try:
        score, s2_artifacts = stage2_evaluate(fn)
        artifacts.update(s2_artifacts)
        artifacts["stage"] = "stage2_complete"
    except Exception as e:
        artifacts["error"] = f"Stage 2 exception: {e}\n{traceback.format_exc()}"
        return {"score": s1_score, "artifacts": artifacts}

    return {"score": score, "artifacts": artifacts}


# ---------------------------------------------------------------------------
# Allow running standalone for testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) > 1:
        result = evaluate(sys.argv[1])
    else:
        # Default: evaluate the initial program
        import os
        default_path = os.path.join(os.path.dirname(__file__), "initial_program.py")
        result = evaluate(default_path)

    print(f"Score: {result['score']:.4f}")
    for k, v in result["artifacts"].items():
        if k == "worst_cases":
            print(f"  {k}:")
            for w in v:
                print(f"    x={w['x']:.6e}  result={w['result']:.17e}  expected={w['expected']:.17e}  ulp={w['ulp']}")
        else:
            print(f"  {k}: {v}")
