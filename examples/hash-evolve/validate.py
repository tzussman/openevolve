#!/usr/bin/env python3
"""
Evaluator calibration script.

Compiles and runs each reference hash through the evaluator to verify:
- The bad hash scores near 0
- FNV-1a scores mediocre (~0.4-0.6)
- MurmurHash3 fmix scores high
- Simplified wyhash scores highest on both quality and throughput
- The scoring system has good dynamic range

Also runs a sensitivity analysis on score component weights.
"""

import os
import re
import subprocess
import sys
import tempfile

# Add parent directories to path for importing evaluator
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from evaluator import (
    compile_program,
    count_evolve_block_lines,
    run_mode,
    parse_float,
    parse_int,
    parse_floats,
    stage1_correctness,
    stage2_quality,
    stage3_extended_distribution,
    stage4_throughput,
    stage5_seed_independence,
)

REFERENCE_SOURCE = os.path.join(SCRIPT_DIR, "reference_hashes.c")

# We need to create temporary C files that use the reference hash functions
# with the same test harness interface as initial_program.c

HASH_TEMPLATE = """\
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <math.h>

static inline uint64_t rotl64(uint64_t x, int k) {{
    return (x << k) | (x >> (64 - k));
}}

static inline uint64_t rotr64(uint64_t x, int k) {{
    return (x >> k) | (x << (64 - k));
}}

static inline uint64_t read64(const uint8_t *p) {{
    uint64_t v;
    memcpy(&v, p, 8);
    return v;
}}

static inline uint32_t read32(const uint8_t *p) {{
    uint32_t v;
    memcpy(&v, p, 4);
    return v;
}}

{hash_implementation}
"""

# Reference hash implementations to embed in the template
HASH_BAD = """
uint64_t hash_function(const uint8_t *key, size_t len, uint64_t seed) {
    if (len == 0) return seed;
    return seed ^ ((uint64_t)key[0] * 31);
}
"""

HASH_FNV1A = """
uint64_t hash_function(const uint8_t *key, size_t len, uint64_t seed) {
    uint64_t h = 0xCBF29CE484222325ULL ^ seed;
    for (size_t i = 0; i < len; i++) {
        h ^= (uint64_t)key[i];
        h *= 0x100000001B3ULL;
    }
    return h;
}
"""

HASH_MURMUR_FMIX = """
static inline uint64_t fmix64(uint64_t h) {
    h ^= h >> 33;
    h *= 0xFF51AFD7ED558CCDULL;
    h ^= h >> 33;
    h *= 0xC4CEB9FE1A85EC53ULL;
    h ^= h >> 33;
    return h;
}

uint64_t hash_function(const uint8_t *key, size_t len, uint64_t seed) {
    uint64_t h = seed ^ (len * 0x9E3779B97F4A7C15ULL);
    const uint8_t *p = key;
    const uint8_t *end = key + len;

    while (p + 8 <= end) {
        uint64_t k = read64(p);
        k *= 0x87C37B91114253D5ULL;
        k = rotl64(k, 31);
        h ^= k;
        h = rotl64(h, 27);
        h = h * 5 + 0x52DCE729;
        p += 8;
    }

    uint64_t tail = 0;
    size_t remaining = end - p;
    for (size_t i = 0; i < remaining; i++) {
        tail |= (uint64_t)p[i] << (i * 8);
    }
    if (remaining > 0) {
        tail *= 0x87C37B91114253D5ULL;
        tail = rotl64(tail, 31);
        h ^= tail;
    }

    return fmix64(h);
}
"""

HASH_WYHASH = """
static inline uint64_t wymix(uint64_t a, uint64_t b) {
    __uint128_t r = (__uint128_t)a * b;
    return (uint64_t)(r >> 64) ^ (uint64_t)r;
}

uint64_t hash_function(const uint8_t *key, size_t len, uint64_t seed) {
    const uint64_t s0 = 0xA0761D6478BD642FULL;
    const uint64_t s1 = 0xE7037ED1A0B428DBULL;
    const uint64_t s2 = 0x8EBC6AF09C88C6E3ULL;
    const uint64_t s3 = 0x589965CC75374CC3ULL;

    seed ^= s0;
    uint64_t a, b;
    const uint8_t *p = key;

    if (len <= 16) {
        if (len >= 4) {
            a = (uint64_t)(read32(p)) | ((uint64_t)read32(p + ((len >> 3) << 2)) << 32);
            b = (uint64_t)(read32(p + len - 4)) | ((uint64_t)read32(p + len - 4 - ((len >> 3) << 2)) << 32);
        } else if (len > 0) {
            a = ((uint64_t)p[0] << 16) | ((uint64_t)p[len >> 1] << 8) | (uint64_t)p[len - 1];
            b = 0;
        } else {
            a = 0;
            b = 0;
        }
    } else {
        size_t i = len;
        if (i > 48) {
            uint64_t see1 = seed, see2 = seed;
            do {
                seed = wymix(read64(p) ^ s1, read64(p + 8) ^ seed);
                see1 = wymix(read64(p + 16) ^ s2, read64(p + 24) ^ see1);
                see2 = wymix(read64(p + 32) ^ s3, read64(p + 40) ^ see2);
                p += 48;
                i -= 48;
            } while (i > 48);
            seed ^= see1 ^ see2;
        }
        while (i > 16) {
            seed = wymix(read64(p) ^ s1, read64(p + 8) ^ seed);
            p += 16;
            i -= 16;
        }
        a = read64(p + i - 16);
        b = read64(p + i - 8);
    }

    return wymix(s1 ^ len, wymix(a ^ s1, b ^ seed));
}
"""

HASH_XXH64 = """
static const uint64_t XXH_PRIME64_1 = 0x9E3779B185EBCA87ULL;
static const uint64_t XXH_PRIME64_2 = 0xC2B2AE3D27D4EB4FULL;
static const uint64_t XXH_PRIME64_3 = 0x165667B19E3779F9ULL;
static const uint64_t XXH_PRIME64_4 = 0x85EBCA77C2B2AE63ULL;
static const uint64_t XXH_PRIME64_5 = 0x27D4EB2F165667C5ULL;

static inline uint64_t xxh64_round(uint64_t acc, uint64_t input) {
    acc += input * XXH_PRIME64_2;
    acc = rotl64(acc, 31);
    acc *= XXH_PRIME64_1;
    return acc;
}

static inline uint64_t xxh64_merge_round(uint64_t acc, uint64_t val) {
    val = xxh64_round(0, val);
    acc ^= val;
    acc = acc * XXH_PRIME64_1 + XXH_PRIME64_4;
    return acc;
}

static inline uint64_t xxh64_avalanche(uint64_t h) {
    h ^= h >> 33;
    h *= XXH_PRIME64_2;
    h ^= h >> 29;
    h *= XXH_PRIME64_3;
    h ^= h >> 32;
    return h;
}

uint64_t hash_function(const uint8_t *key, size_t len, uint64_t seed) {
    const uint8_t *p = key;
    const uint8_t *end = key + len;
    uint64_t h;

    if (len >= 32) {
        uint64_t v1 = seed + XXH_PRIME64_1 + XXH_PRIME64_2;
        uint64_t v2 = seed + XXH_PRIME64_2;
        uint64_t v3 = seed + 0;
        uint64_t v4 = seed - XXH_PRIME64_1;

        do {
            v1 = xxh64_round(v1, read64(p)); p += 8;
            v2 = xxh64_round(v2, read64(p)); p += 8;
            v3 = xxh64_round(v3, read64(p)); p += 8;
            v4 = xxh64_round(v4, read64(p)); p += 8;
        } while (p <= end - 32);

        h = rotl64(v1, 1) + rotl64(v2, 7) + rotl64(v3, 12) + rotl64(v4, 18);
        h = xxh64_merge_round(h, v1);
        h = xxh64_merge_round(h, v2);
        h = xxh64_merge_round(h, v3);
        h = xxh64_merge_round(h, v4);
    } else {
        h = seed + XXH_PRIME64_5;
    }

    h += (uint64_t)len;

    while (p + 8 <= end) {
        uint64_t k = xxh64_round(0, read64(p));
        h ^= k;
        h = rotl64(h, 27) * XXH_PRIME64_1 + XXH_PRIME64_4;
        p += 8;
    }

    if (p + 4 <= end) {
        h ^= (uint64_t)read32(p) * XXH_PRIME64_1;
        h = rotl64(h, 23) * XXH_PRIME64_2 + XXH_PRIME64_3;
        p += 4;
    }

    while (p < end) {
        h ^= (uint64_t)(*p) * XXH_PRIME64_5;
        h = rotl64(h, 11) * XXH_PRIME64_1;
        p++;
    }

    return xxh64_avalanche(h);
}
"""

REFERENCE_HASHES = {
    "bad": HASH_BAD,
    "fnv1a": HASH_FNV1A,
    "murmur_fmix": HASH_MURMUR_FMIX,
    "wyhash": HASH_WYHASH,
    "xxh64": HASH_XXH64,
}

EXPECTED_RANGES = {
    "bad": {"score_max": 0.15, "quality_max": 0.15},
    "fnv1a": {"score_min": 0.2, "score_max": 0.7},
    "murmur_fmix": {"score_min": 0.5},
    "wyhash": {"score_min": 0.6},
    "xxh64": {"score_min": 0.5},
}


def evaluate_reference(name: str, implementation: str) -> dict | None:
    """Create a temp C file with the reference hash and evaluate it."""
    source_code = HASH_TEMPLATE.format(hash_implementation=implementation)

    with tempfile.TemporaryDirectory() as tmpdir:
        source_path = os.path.join(tmpdir, f"{name}.c")
        binary_path = os.path.join(tmpdir, f"{name}")

        with open(source_path, "w") as f:
            f.write(source_code)

        # Compile
        ok, err = compile_program(source_path, binary_path)
        if not ok:
            print(f"  COMPILE FAILED: {err}")
            return None

        # Run all stages
        results = {}

        s1_score, s1_details = stage1_correctness(binary_path)
        results["stage1"] = s1_score
        results["collisions"] = s1_details.get("collisions", -1)
        results["chi_squared"] = s1_details.get("chi_squared", -1)

        if s1_score > 0:
            s2_score, s2_details = stage2_quality(binary_path)
            results["stage2"] = s2_score
            results["avalanche"] = s2_details.get("avalanche", 0)
            results["diffusion"] = s2_details.get("diffusion", 0)
            results["bit_independence"] = s2_details.get("bit_independence", 0)

            s3_score, s3_details = stage3_extended_distribution(binary_path)
            results["stage3"] = s3_score
            results["extdist_passed"] = s3_details.get("extdist_passed", "?")

            s4_score, s4_details = stage4_throughput(binary_path)
            results["stage4"] = s4_score
            results["throughput_gbps"] = s4_details.get("bench_weighted_avg_gbps", 0)
            for klen in [4, 8, 16, 32, 64, 128, 256, 1024]:
                results[f"bench_{klen}B_gbps"] = s4_details.get(f"bench_{klen}B_gbps", 0)

            s5_score, s5_details = stage5_seed_independence(binary_path)
            results["stage5"] = s5_score
            results["seed_independence"] = s5_details.get("seed_independence", 0)

            quality = (
                0.30 * s1_score
                + 0.35 * s2_score
                + 0.20 * s3_score
                + 0.15 * s5_score
            )
            throughput = s4_score
            combined = 0.6 * quality + 0.4 * throughput
        else:
            results["stage2"] = 0
            results["stage3"] = 0
            results["stage4"] = 0
            results["stage5"] = 0
            results["avalanche"] = 0
            results["diffusion"] = 0
            results["bit_independence"] = 0
            results["throughput_gbps"] = 0
            results["seed_independence"] = 0
            for klen in [4, 8, 16, 32, 64, 128, 256, 1024]:
                results[f"bench_{klen}B_gbps"] = 0
            quality = 0
            throughput = 0
            combined = 0

        results["quality"] = quality
        results["throughput"] = throughput
        results["combined_score"] = combined

        return results


def sensitivity_analysis(results: dict):
    """Analyze how much each component contributes to score differentiation."""
    print("\n" + "=" * 70)
    print("SENSITIVITY ANALYSIS")
    print("=" * 70)

    # Get quality sub-scores for hashes that passed stage 1
    passing = {k: v for k, v in results.items() if v and v.get("stage1", 0) > 0}
    if len(passing) < 2:
        print("Not enough passing hashes for sensitivity analysis")
        return

    # Compute variance of each stage score across reference hashes
    stages = ["stage1", "stage2", "stage3", "stage4", "stage5"]
    stage_names = [
        "Correctness",
        "Avalanche/Diffusion",
        "Extended Dist",
        "Throughput",
        "Seed Independence",
    ]
    weights = [0.30, 0.35, 0.20, 0.40, 0.15]  # quality weights + throughput weight

    print(f"\n{'Stage':<25} {'Mean':>8} {'StdDev':>8} {'Range':>8} {'Weight':>8} {'Contribution':>12}")
    print("-" * 70)

    for stage, sname, w in zip(stages, stage_names, weights):
        values = [v[stage] for v in passing.values()]
        mean = sum(values) / len(values)
        variance = sum((x - mean) ** 2 for x in values) / len(values)
        stddev = variance**0.5
        val_range = max(values) - min(values)
        contribution = val_range * w

        print(
            f"  {sname:<23} {mean:>8.3f} {stddev:>8.3f} {val_range:>8.3f} {w:>8.2f} {contribution:>12.3f}"
        )

    # Check if any component dominates
    print("\nRecommendation:", end=" ")
    ranges = {}
    for stage, sname, w in zip(stages, stage_names, weights):
        values = [v[stage] for v in passing.values()]
        ranges[sname] = (max(values) - min(values)) * w

    max_contrib = max(ranges.values())
    min_contrib = min(v for v in ranges.values() if v > 0)
    if max_contrib > 5 * min_contrib:
        dominant = max(ranges, key=ranges.get)
        print(
            f"'{dominant}' dominates the score. Consider reducing its weight."
        )
    else:
        print("Score components are reasonably balanced.")


def evaluate_c_file(c_file_path: str) -> dict | None:
    """Compile and evaluate an arbitrary C program with hash_function."""
    with tempfile.TemporaryDirectory() as tmpdir:
        binary_path = os.path.join(tmpdir, "custom_hash")

        ok, err = compile_program(c_file_path, binary_path)
        if not ok:
            print(f"  COMPILE FAILED: {err}")
            return None

        results = {}

        s1_score, s1_details = stage1_correctness(binary_path)
        results["stage1"] = s1_score
        results["collisions"] = s1_details.get("collisions", -1)
        results["chi_squared"] = s1_details.get("chi_squared", -1)

        if s1_score > 0:
            s2_score, s2_details = stage2_quality(binary_path)
            results["stage2"] = s2_score
            results["avalanche"] = s2_details.get("avalanche", 0)
            results["diffusion"] = s2_details.get("diffusion", 0)
            results["bit_independence"] = s2_details.get("bit_independence", 0)

            s3_score, s3_details = stage3_extended_distribution(binary_path)
            results["stage3"] = s3_score
            results["extdist_passed"] = s3_details.get("extdist_passed", "?")

            s4_score, s4_details = stage4_throughput(binary_path)
            results["stage4"] = s4_score
            results["throughput_gbps"] = s4_details.get("bench_weighted_avg_gbps", 0)
            for klen in [4, 8, 16, 32, 64, 128, 256, 1024]:
                results[f"bench_{klen}B_gbps"] = s4_details.get(f"bench_{klen}B_gbps", 0)

            s5_score, s5_details = stage5_seed_independence(binary_path)
            results["stage5"] = s5_score
            results["seed_independence"] = s5_details.get("seed_independence", 0)

            quality = (
                0.30 * s1_score
                + 0.35 * s2_score
                + 0.20 * s3_score
                + 0.15 * s5_score
            )
            throughput = s4_score
            combined = 0.6 * quality + 0.4 * throughput
        else:
            results["stage2"] = 0
            results["stage3"] = 0
            results["stage4"] = 0
            results["stage5"] = 0
            results["avalanche"] = 0
            results["diffusion"] = 0
            results["bit_independence"] = 0
            results["throughput_gbps"] = 0
            results["seed_independence"] = 0
            for klen in [4, 8, 16, 32, 64, 128, 256, 1024]:
                results[f"bench_{klen}B_gbps"] = 0
            quality = 0
            throughput = 0
            combined = 0

        results["quality"] = quality
        results["throughput"] = throughput
        results["combined_score"] = combined

        return results


def print_results(name: str, results: dict):
    """Print detailed results for a hash evaluation."""
    print(f"  Combined score: {results['combined_score']:.3f}")
    print(f"  Quality:        {results['quality']:.3f}")
    print(f"  Throughput:     {results['throughput']:.3f} ({results['throughput_gbps']:.2f} GB/s weighted avg)")
    print(f"  Avalanche:      {results['avalanche']:.2f} (ideal: 32.0)")
    print(f"  Diffusion:      {results['diffusion']:.2f} (ideal: 32.0)")
    print(f"  Bit indep:      {results['bit_independence']:.4f} (ideal: 0.0)")
    print(f"  Collisions:     {results['collisions']}")
    print(f"  Chi-squared:    {results['chi_squared']:.1f}")
    print(f"  Seed indep:     {results['seed_independence']:.2f} (ideal: 32.0)")
    print(f"  Ext dist:       {results.get('extdist_passed', 'N/A')}")
    print(f"  Throughput per key length:")
    for klen in [4, 8, 16, 32, 64, 128, 256, 1024]:
        gbps = results.get(f"bench_{klen}B_gbps", 0)
        print(f"    {klen:>5}B: {gbps:>8.2f} GB/s")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Hash evaluator calibration / validation")
    parser.add_argument(
        "c_program",
        nargs="?",
        default=None,
        help="Optional path to a C program with hash_function to evaluate",
    )
    args = parser.parse_args()

    if args.c_program:
        c_path = os.path.abspath(args.c_program)
        print("=" * 70)
        print(f"EVALUATING: {c_path}")
        print("=" * 70)
        results = evaluate_c_file(c_path)
        if results is None:
            print("  FAILED to evaluate")
            sys.exit(1)
        print_results(os.path.basename(c_path), results)
        sys.exit(0)

    print("=" * 70)
    print("HASH EVALUATOR CALIBRATION")
    print("=" * 70)

    all_results = {}

    for name, impl in REFERENCE_HASHES.items():
        print(f"\n{'=' * 50}")
        print(f"Evaluating: {name}")
        print("=" * 50)

        results = evaluate_reference(name, impl)
        all_results[name] = results

        if results is None:
            print("  FAILED to evaluate")
            continue

        print_results(name, results)

        # Check against expectations
        expected = EXPECTED_RANGES.get(name, {})
        issues = []
        score = results["combined_score"]
        if "score_min" in expected and score < expected["score_min"]:
            issues.append(f"score {score:.3f} < expected min {expected['score_min']}")
        if "score_max" in expected and score > expected["score_max"]:
            issues.append(f"score {score:.3f} > expected max {expected['score_max']}")

        if issues:
            print(f"  WARNING: {'; '.join(issues)}")
        else:
            print("  Calibration: OK")

    # Summary table
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"{'Hash':<15} {'Combined':>10} {'Quality':>10} {'Throughput':>10} {'Avalanche':>10} {'Collisions':>10}")
    print("-" * 70)
    for name in REFERENCE_HASHES:
        r = all_results.get(name)
        if r:
            print(
                f"  {name:<13} {r['combined_score']:>10.3f} {r['quality']:>10.3f} "
                f"{r['throughput']:>10.3f} {r['avalanche']:>10.2f} {r['collisions']:>10d}"
            )
        else:
            print(f"  {name:<13} {'FAILED':>10}")

    # Dynamic range check
    scores = [
        all_results[n]["combined_score"]
        for n in REFERENCE_HASHES
        if all_results.get(n)
    ]
    if scores:
        score_range = max(scores) - min(scores)
        print(f"\nScore range: {min(scores):.3f} — {max(scores):.3f} (span: {score_range:.3f})")
        if score_range < 0.3:
            print("WARNING: Dynamic range is narrow. Consider adjusting weights.")
        elif score_range > 0.8:
            print("Good dynamic range for evolution.")
        else:
            print("Acceptable dynamic range.")

    sensitivity_analysis(all_results)


if __name__ == "__main__":
    main()
