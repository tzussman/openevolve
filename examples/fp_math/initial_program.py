import math


# EVOLVE-BLOCK-START
def my_sin(x):
    """Compute sin(x) for float64 x. Return float64 result."""
    # Handle special cases
    if x == 0.0:
        return x  # preserves sign of zero
    if not math.isfinite(x):
        return math.nan

    # Range reduction to [-pi/2, pi/2] using math.remainder for precision
    # remainder(x, 2*pi) gives x mod 2*pi in [-pi, pi]
    x = math.remainder(x, 2.0 * math.pi)

    # Further reduce to [-pi/2, pi/2]
    if x > math.pi / 2:
        x = math.pi - x
    elif x < -math.pi / 2:
        x = -math.pi - x

    # Taylor series: sin(x) = x - x^3/6 + x^5/120 - x^7/5040 + ...
    # Using Horner's method for efficiency
    x2 = x * x
    # 11 terms for reasonable precision
    result = x * (1.0 + x2 * (-1.0/6.0 + x2 * (1.0/120.0 + x2 * (-1.0/5040.0
             + x2 * (1.0/362880.0 + x2 * (-1.0/39916800.0
             + x2 * (1.0/6227020800.0)))))))
    return result
# EVOLVE-BLOCK-END


def run():
    """Entry point for evaluator."""
    return my_sin
