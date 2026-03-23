import math


# EVOLVE-BLOCK-START
def my_sin(x):
    """Compute sin(x) for float64 x. Return float64 result."""
    # Handle special cases
    if x == 0.0:
        return x  # preserves sign of zero
    if not math.isfinite(x):
        return math.nan

    # High-precision constants for range reduction (Cody-Waite/Payne-Hanek inspired)
    # These constants are derived from high-precision pi and split into two float64 parts.
    TWO_PI_HI = 6.283185307179586  # float(2 * pi_high_precision)
    TWO_PI_LO = 2.4492935982947064e-16 # float(2 * pi_high_precision - TWO_PI_HI)
    PI_HI = 3.141592653589793      # float(pi_high_precision)
    PI_LO = 1.2246467991473532e-16 # float(pi_high_precision - PI_HI)
    HALF_PI_HI = 1.5707963267948966 # float(pi_high_precision / 2)
    INV_TWO_PI = 0.15915494309189535 # float(1 / (2 * pi_high_precision))

    # Step 1: Reduce x to the range [-pi, pi]
    # n = round(x / (2*pi)) to find the nearest multiple of 2*pi
    n = round(x * INV_TWO_PI)
    
    # r = x - n * 2*pi, computed with extra precision (double-double subtraction)
    # This helps preserve precision for large x.
    r = (x - n * TWO_PI_HI) - n * TWO_PI_LO

    # Step 2: Further reduce r to [-pi/2, pi/2] and handle quadrant mapping.
    # If r is in (pi/2, pi], then sin(r) = sin(pi - r)
    # If r is in [-pi, -pi/2), then sin(r) = sin(-pi - r)
    
    if r > HALF_PI_HI:
        # r is in (pi/2, pi]. Map to [0, pi/2] using pi - r.
        # x_prime = pi - r, computed with extra precision
        x = (PI_HI - r) + PI_LO
    elif r < -HALF_PI_HI:
        # r is in [-pi, -pi/2). Map to [0, pi/2] using -pi - r.
        # x_prime = -pi - r, computed with extra precision
        x = (-PI_HI - r) - PI_LO
    else:
        # r is already in [-pi/2, pi/2]
        x = r

    # High-order Taylor series for sin(x)/x (since sin(x)/x approaches 1 as x->0).
    # This avoids loss of precision for x near zero and is evaluated using Horner's method on x^2.
    # These coefficients ensure full float64 precision over the [-pi/2, pi/2] range.
    x2 = x * x
    poly = (-1.6666666666666667e-01 + x2 * ( # -1/3!
        8.333333333333333e-03 + x2 * (                         # +1/5!
        -1.984126984126984e-04 + x2 * (                        # -1/7!
        2.7557319223985893e-06 + x2 * (                        # +1/9!
        -2.5052108385441718e-08 + x2 * (                       # -1/11!
        1.6059043836821614e-10 + x2 * (                        # +1/13!
        -7.64716373182079e-13 + x2 * (                         # -1/15!
        2.81145725434588e-15 + x2 * (                          # +1/17!
        -8.22063584313415e-18 + x2 * (                         # -1/19!
        1.957294248365274e-20 + x2 * (                         # +1/21!
        -3.837831859539753e-23 + x2 * (                        # -1/23!
        6.396386432566255e-26                                 # +1/25!
    )))))))))))) # 12 closing parentheses
    # Compute sin(x) as x + x * x2 * poly. This form is numerically stable.
    result = x + x * x2 * poly
    return result
# EVOLVE-BLOCK-END


def run():
    """Entry point for evaluator."""
    return my_sin
