#!/usr/bin/env python3
"""
Sanity-check the evaluator against known generators, or validate the
best evolved program from a checkpoint directory.

Usage:
  python validate.py                     # run reference generators through evaluator
  python validate.py --best [CKPT_DIR]   # run evaluator on best_program.c

By default uses stage 2 (PractRand to 256 MB, ~15s per generator).
Use --full for the complete 64 GB PractRand test (~10 min per generator).

Expected ranking (quality scores) for reference generators:
  counter (bad)          ~0.1 or less
  xorshift64 (baseline)  ~0.2-0.4
  SplitMix64 (good)      ~0.85+
  xoshiro128** (small)   ~0.80+  (32-bit engine, two calls per uint64)
  xoshiro256** (best)    ~0.85+

If SplitMix64 doesn't score well above xorshift64, the evaluator needs tuning.
"""

import argparse
import glob
import os
import sys
import tempfile

# Ensure openevolve is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import evaluator as ev

# ---------------------------------------------------------------------------
# Reference generators — each is a complete C file matching initial_program.c
# ---------------------------------------------------------------------------

_HARNESS_TEMPLATE = r"""
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <string.h>

static inline uint64_t rotl64(uint64_t x, int k) {{
    return (x << k) | (x >> (64 - k));
}}
static inline uint64_t rotr64(uint64_t x, int k) {{
    return (x >> k) | (x << (64 - k));
}}

// --- EVOLVE-BLOCK-START ---
{evolve_block}
// --- EVOLVE-BLOCK-END ---

int main(int argc, char *argv[]) {{
    if (argc < 2) {{ fprintf(stderr, "Usage: %s <mode> [count]\n", argv[0]); return 1; }}
    rng_state_t state;
    rng_seed(&state, 0xDEADBEEFCAFE1234ULL);

    if (strcmp(argv[1], "binary") == 0) {{
        uint64_t count = 0;
        if (argc >= 3) count = (uint64_t)atoll(argv[2]) * 1000000ULL;
        uint64_t buf[512];
        uint64_t generated = 0;
        for (;;) {{
            for (int i = 0; i < 512; i++) buf[i] = rng_next(&state);
            fwrite(buf, sizeof(uint64_t), 512, stdout);
            generated += 512;
            if (count > 0 && generated >= count) break;
        }}
    }} else if (strcmp(argv[1], "bench") == 0) {{
        uint64_t n = 100000000ULL;
        if (argc >= 3) n = (uint64_t)atoll(argv[2]);
        volatile uint64_t sink = 0;
        struct timespec t0, t1;
        clock_gettime(CLOCK_MONOTONIC, &t0);
        for (uint64_t i = 0; i < n; i++) {{ sink ^= rng_next(&state); }}
        clock_gettime(CLOCK_MONOTONIC, &t1);
        double elapsed = (t1.tv_sec - t0.tv_sec) + (t1.tv_nsec - t0.tv_nsec) / 1e9;
        printf("%.2f\n", (double)n / elapsed);
    }} else if (strcmp(argv[1], "statesize") == 0) {{
        printf("%zu\n", sizeof(rng_state_t));
    }} else {{ fprintf(stderr, "Unknown mode: %s\n", argv[1]); return 1; }}
    return 0;
}}
"""

GENERATORS = {
    "counter (bad)": r"""
typedef struct { uint64_t s; } rng_state_t;
void rng_seed(rng_state_t *state, uint64_t seed) { state->s = seed; }
uint64_t rng_next(rng_state_t *state) { return state->s++; }
""",
    "xorshift64 (baseline)": r"""
typedef struct { uint64_t s; } rng_state_t;
void rng_seed(rng_state_t *state, uint64_t seed) {
    state->s = seed; if (state->s == 0) state->s = 1;
}
uint64_t rng_next(rng_state_t *state) {
    uint64_t x = state->s;
    x ^= x << 13; x ^= x >> 7; x ^= x << 17;
    state->s = x; return x;
}
""",
    "splitmix64 (good)": r"""
typedef struct { uint64_t s; } rng_state_t;
void rng_seed(rng_state_t *state, uint64_t seed) { state->s = seed; }
uint64_t rng_next(rng_state_t *state) {
    uint64_t z = (state->s += 0x9e3779b97f4a7c15ULL);
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    return z ^ (z >> 31);
}
""",
    "xoshiro256** (excellent)": r"""
typedef struct { uint64_t s[4]; } rng_state_t;
void rng_seed(rng_state_t *state, uint64_t seed) {
    uint64_t z = seed;
    for (int i = 0; i < 4; i++) {
        z += 0x9e3779b97f4a7c15ULL;
        uint64_t t = z;
        t = (t ^ (t >> 30)) * 0xbf58476d1ce4e5b9ULL;
        t = (t ^ (t >> 27)) * 0x94d049bb133111ebULL;
        state->s[i] = t ^ (t >> 31);
    }
}
uint64_t rng_next(rng_state_t *state) {
    const uint64_t result = rotl64(state->s[1] * 5, 7) * 9;
    const uint64_t t = state->s[1] << 17;
    state->s[2] ^= state->s[0];
    state->s[3] ^= state->s[1];
    state->s[1] ^= state->s[2];
    state->s[0] ^= state->s[3];
    state->s[2] ^= t;
    state->s[3] = rotl64(state->s[3], 45);
    return result;
}
""",
    "wyrand (speed king)": r"""
typedef struct { uint64_t s; } rng_state_t;
void rng_seed(rng_state_t *state, uint64_t seed) { state->s = seed; }
uint64_t rng_next(rng_state_t *state) {
    state->s += 0x2d358dccaa6c78a5ULL;
    __uint128_t r = (__uint128_t)state->s * (state->s ^ 0x8bb84b93962eacc9ULL);
    uint64_t lo = (uint64_t)r;
    uint64_t hi = (uint64_t)(r >> 64);
    return lo ^ hi;
}
""",
    "RomuDuoJr (fast 2-state)": r"""
typedef struct { uint64_t s[2]; } rng_state_t;
void rng_seed(rng_state_t *state, uint64_t seed) {
    /* splitmix64 seeding */
    uint64_t z = seed;
    for (int i = 0; i < 2; i++) {
        z += 0x9e3779b97f4a7c15ULL;
        uint64_t t = z;
        t = (t ^ (t >> 30)) * 0xbf58476d1ce4e5b9ULL;
        t = (t ^ (t >> 27)) * 0x94d049bb133111ebULL;
        state->s[i] = t ^ (t >> 31);
    }
    if (state->s[0] == 0 && state->s[1] == 0) state->s[0] = 1;
}
uint64_t rng_next(rng_state_t *state) {
    uint64_t xp = state->s[0];
    state->s[0] = 15241094284759029579ULL * state->s[1];
    state->s[1] = state->s[1] - xp;
    state->s[1] = rotl64(state->s[1], 27);
    return xp;
}
""",
    "lehmer128 (MCG)": r"""
typedef struct { __uint128_t s; } rng_state_t;
void rng_seed(rng_state_t *state, uint64_t seed) {
    state->s = ((__uint128_t)seed << 1) | 1; /* must be odd */
}
uint64_t rng_next(rng_state_t *state) {
    state->s *= 0xda942042e4dd58b5ULL;
    return (uint64_t)(state->s >> 64);
}
""",
    "squares64 (counter-based)": r"""
typedef struct { uint64_t ctr; uint64_t key; } rng_state_t;
void rng_seed(rng_state_t *state, uint64_t seed) {
    /* Key must be irregular (not too sparse/dense in bits).
       Use splitmix64 to derive a suitable key from the seed. */
    uint64_t z = seed + 0x9e3779b97f4a7c15ULL;
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    state->key = z ^ (z >> 31);
    if (state->key == 0) state->key = 0xc58f6b1bc4f85e29ULL;
    state->ctr = 0;
}
uint64_t rng_next(rng_state_t *state) {
    uint64_t ctr = ++(state->ctr);
    uint64_t key = state->key;
    uint64_t t, x, y, z;
    y = x = ctr * key; z = y + key;
    x = x*x + y; x = (x>>32) | (x<<32);        /* round 1 */
    x = x*x + z; x = (x>>32) | (x<<32);        /* round 2 */
    x = x*x + y; x = (x>>32) | (x<<32);        /* round 3 */
    t = x = x*x + z; x = (x>>32) | (x<<32);    /* round 4 */
    return t ^ ((x*x + y) >> 32);               /* round 5 */
}
""",
    "CWG64 (Collatz-Weyl)": r"""
typedef struct { uint64_t x; uint64_t a; uint64_t weyl; uint64_t s; } rng_state_t;
void rng_seed(rng_state_t *state, uint64_t seed) {
    /* Derive state from seed via splitmix64 */
    uint64_t z = seed;
    z += 0x9e3779b97f4a7c15ULL;
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    state->x = z ^ (z >> 31);
    z += 0x9e3779b97f4a7c15ULL;
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    state->a = z ^ (z >> 31);
    state->weyl = 0;
    z += 0x9e3779b97f4a7c15ULL;
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    state->s = (z ^ (z >> 31)) | 1; /* must be odd */
}
uint64_t rng_next(rng_state_t *state) {
    state->x = (state->x >> 1) * ((state->a += state->x) | 1)
               ^ (state->weyl += state->s);
    return state->a >> 48 ^ state->x;
}
""",
    "L64X64 (LXM)": r"""
typedef struct { uint64_t a; uint64_t s; uint64_t x; } rng_state_t;
void rng_seed(rng_state_t *state, uint64_t seed) {
    /* LXM: L = LCG component, X = xorshift component */
    uint64_t z = seed;
    z += 0x9e3779b97f4a7c15ULL;
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    state->s = z ^ (z >> 31);
    z += 0x9e3779b97f4a7c15ULL;
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    state->x = z ^ (z >> 31);
    if (state->x == 0) state->x = 1;
    state->a = state->s | 1; /* LCG addend must be odd */
}
uint64_t rng_next(rng_state_t *state) {
    /* Combine LCG and xorshift state for output */
    uint64_t s = state->s;
    uint64_t x = state->x;
    /* Output: starstar scrambler on s + x */
    uint64_t z = s + x;
    z = (z ^ (z >> 32)) * 0xdaba0b6eb09322e3ULL;
    z = (z ^ (z >> 32)) * 0xdaba0b6eb09322e3ULL;
    z = z ^ (z >> 32);
    /* Advance LCG: s = s * M + a */
    state->s = s * 0xd1342543de82ef95ULL + state->a;
    /* Advance xorshift */
    x ^= x << 23;
    x ^= x >> 17;
    x ^= x << 29;    /* <-- should be a rotation but keeping simple xorshift128 style */
    state->x = x;
    return z;
}
""",
    "xoshiro128** (small-state)": r"""
typedef struct { uint32_t s[4]; } rng_state_t;
static inline uint32_t rotl32(uint32_t x, int k) {
    return (x << k) | (x >> (32 - k));
}
void rng_seed(rng_state_t *state, uint64_t seed) {
    /* Use splitmix64 to fill 128 bits of state from a 64-bit seed */
    uint64_t z = seed;
    for (int i = 0; i < 4; i++) {
        z += 0x9e3779b97f4a7c15ULL;
        uint64_t t = z;
        t = (t ^ (t >> 30)) * 0xbf58476d1ce4e5b9ULL;
        t = (t ^ (t >> 27)) * 0x94d049bb133111ebULL;
        state->s[i] = (uint32_t)((t ^ (t >> 31)) & 0xFFFFFFFF);
    }
    /* Ensure not all-zero */
    if (state->s[0] == 0 && state->s[1] == 0 &&
        state->s[2] == 0 && state->s[3] == 0)
        state->s[0] = 1;
}
uint64_t rng_next(rng_state_t *state) {
    /* Generate two 32-bit outputs and pack into one uint64_t */
    uint64_t hi, lo;
    /* First 32-bit output */
    lo = (uint64_t)(rotl32(state->s[1] * 5, 7) * 9);
    uint32_t t = state->s[1] << 9;
    state->s[2] ^= state->s[0];
    state->s[3] ^= state->s[1];
    state->s[1] ^= state->s[2];
    state->s[0] ^= state->s[3];
    state->s[2] ^= t;
    state->s[3] = rotl32(state->s[3], 11);
    /* Second 32-bit output */
    hi = (uint64_t)(rotl32(state->s[1] * 5, 7) * 9);
    t = state->s[1] << 9;
    state->s[2] ^= state->s[0];
    state->s[3] ^= state->s[1];
    state->s[1] ^= state->s[2];
    state->s[0] ^= state->s[3];
    state->s[2] ^= t;
    state->s[3] = rotl32(state->s[3], 11);
    return (hi << 32) | lo;
}
""",
}


def _find_best_program(ckpt_dir: str | None) -> str:
    """Find the best_program.c in the given or latest checkpoint directory."""
    if ckpt_dir is None:
        # Auto-detect: find the latest checkpoint directory
        base = os.path.join(os.path.dirname(__file__), "openevolve_output", "checkpoints")
        if not os.path.isdir(base):
            sys.exit(f"No checkpoints directory found at {base}")
        ckpt_dirs = sorted(glob.glob(os.path.join(base, "checkpoint_*")))
        if not ckpt_dirs:
            sys.exit(f"No checkpoint_* directories found in {base}")
        ckpt_dir = ckpt_dirs[-1]

    best = os.path.join(ckpt_dir, "best_program.c")
    if not os.path.isfile(best):
        sys.exit(f"best_program.c not found in {ckpt_dir}")
    return best


# ---------------------------------------------------------------------------
# Reference generator validation (original mode)
# ---------------------------------------------------------------------------

def _run_reference_validation(full: bool = False) -> None:
    eval_fn = ev.evaluate if full else ev.evaluate_stage2
    label = "64 GB" if full else "256 MB"

    print("=" * 70)
    print(f"PRNG Evaluator Validation (PractRand to {label})")
    print("=" * 70)

    results: dict[str, dict] = {}

    for name, evolve_block in GENERATORS.items():
        print(f"\n--- {name} ---")
        source = _HARNESS_TEMPLATE.format(evolve_block=evolve_block)

        fd, path = tempfile.mkstemp(suffix=".c")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(source)
            result = eval_fn(path)
            m = result.metrics
            results[name] = m

            print(f"  combined_score = {m.get('combined_score', 0):.4f}")
            print(f"  quality        = {m.get('quality', 0):.4f}")
            print(f"  throughput     = {m.get('throughput', 0):.4f}")
            print(f"  ops/sec        = {m.get('ops_per_sec', 0):.0f}")
            print(f"  state_bits     = {m.get('state_bits', 0):.0f}")

            # Show test details from artifacts
            td = result.artifacts.get("test_details", "")
            for line in td.split("\n"):
                if line.strip():
                    print(f"    {line}")
        finally:
            os.unlink(path)

    # --- Checks ---
    print("\n" + "=" * 70)
    print("Validation Summary")
    print("=" * 70)

    def q(name: str) -> float:
        return results.get(name, {}).get("quality", 0.0)

    checks = [
        ("Counter quality < 0.15",             q("counter (bad)") < 0.15),
        ("Xorshift64 quality in [0.15, 0.5]",  0.15 <= q("xorshift64 (baseline)") <= 0.5),
        ("SplitMix64 quality > 0.75",          q("splitmix64 (good)") > 0.75),
        ("Xoshiro256** quality > 0.75",         q("xoshiro256** (excellent)") > 0.75),
        ("Xoshiro128** quality > 0.70",         q("xoshiro128** (small-state)") > 0.70),
        ("wyrand quality > 0.75",               q("wyrand (speed king)") > 0.75),
        ("RomuDuoJr quality > 0.70",            q("RomuDuoJr (fast 2-state)") > 0.70),
        ("SplitMix64 > xorshift64",            q("splitmix64 (good)") > q("xorshift64 (baseline)")),
        ("Xoshiro256** > xorshift64",           q("xoshiro256** (excellent)") > q("xorshift64 (baseline)")),
        ("Xoshiro128** > xorshift64",           q("xoshiro128** (small-state)") > q("xorshift64 (baseline)")),
        ("Counter < xorshift64",               q("counter (bad)") < q("xorshift64 (baseline)")),
    ]

    all_pass = True
    for desc, ok in checks:
        tag = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False
        print(f"  [{tag}] {desc}")

    print()
    if all_pass:
        print("All checks passed — evaluator has good dynamic range.")
    else:
        print("WARNING: some checks failed — evaluator may need tuning.")

    sys.exit(0 if all_pass else 1)


def main() -> None:
    parser = argparse.ArgumentParser(description="PRNG evaluator validation")
    parser.add_argument(
        "--best", nargs="?", const=None, default=False,
        metavar="CKPT_DIR",
        help="Run evaluator on best_program.c from a checkpoint dir. "
             "If no dir given, uses the latest checkpoint.",
    )
    parser.add_argument(
        "--full", action="store_true",
        help="Run full 64 GB PractRand test (~10 min per generator). "
             "Default uses stage 2 (256 MB, ~15s per generator).",
    )
    args = parser.parse_args()

    if args.best is not False:
        source = _find_best_program(args.best)
        eval_fn = ev.evaluate if args.full else ev.evaluate_stage2
        label = "64 GB" if args.full else "256 MB"

        print("=" * 70)
        print(f"Evaluator results for: {source} (PractRand to {label})")
        print("=" * 70)
        result = eval_fn(source)
        m = result.metrics
        print(f"  combined_score = {m.get('combined_score', 0):.4f}")
        print(f"  quality        = {m.get('quality', 0):.4f}")
        print(f"  throughput     = {m.get('throughput', 0):.4f}")
        print(f"  ops/sec        = {m.get('ops_per_sec', 0):.0f}")
        print(f"  state_bits     = {m.get('state_bits', 0):.0f}")

        td = result.artifacts.get("test_details", "")
        for line in td.split("\n"):
            if line.strip():
                print(f"    {line}")
    else:
        _run_reference_validation(full=args.full)


if __name__ == "__main__":
    main()
