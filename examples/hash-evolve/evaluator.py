"""
Evaluator for hash function evolution.

Compiles evolved C code and runs it through multiple quality and performance
tests, returning a composite score for OpenEvolve.
"""

import math
import os
import subprocess
import sys
import tempfile
import time


def compile_program(source_path: str, binary_path: str) -> tuple[bool, str]:
    """Compile with gcc -O2 -march=native -lm. Return (success, error_message).

    The evolved program (source_path) contains only the hash function.
    It is injected into the test harness via gcc's -include flag.
    """
    harness_path = os.path.join(os.path.dirname(__file__), "test_harness.c")
    try:
        result = subprocess.run(
            [
                "gcc", "-O2", "-march=native",
                "-include", source_path,
                "-o", binary_path,
                harness_path,
                "-lm",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0, result.stderr
    except subprocess.TimeoutExpired:
        return False, "Compilation timed out (15s)"
    except Exception as e:
        return False, str(e)


def run_mode(binary_path: str, mode: str, timeout: int = 30) -> tuple[bool, str]:
    """Run the hash binary in a given test mode. Return (success, stdout)."""
    try:
        result = subprocess.run(
            [binary_path, mode],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return False, f"Exit code {result.returncode}: {result.stderr}"
        return True, result.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, f"Timeout ({timeout}s) in mode: {mode}"
    except Exception as e:
        return False, str(e)


def parse_float(output: str) -> float | None:
    """Parse a single float from output."""
    try:
        return float(output.strip().split("\n")[0])
    except (ValueError, IndexError):
        return None


def parse_int(output: str) -> int | None:
    """Parse a single int from output."""
    try:
        return int(output.strip().split("\n")[0])
    except (ValueError, IndexError):
        return None


def parse_floats(output: str) -> list[float]:
    """Parse multiple floats from output, one per line."""
    results = []
    for line in output.strip().split("\n"):
        try:
            results.append(float(line.strip()))
        except ValueError:
            continue
    return results


def count_evolve_block_lines(source_path: str) -> int:
    """Count lines in the EVOLVE-BLOCK."""
    in_block = False
    count = 0
    try:
        with open(source_path, "r") as f:
            for line in f:
                if "EVOLVE-BLOCK-START" in line:
                    in_block = True
                    continue
                if "EVOLVE-BLOCK-END" in line:
                    break
                if in_block:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("//"):
                        count += 1
    except Exception:
        pass
    return count


def sanity_check(binary_path: str) -> tuple[bool, str]:
    """Basic sanity: hash of different inputs should differ."""
    # We compile a small test that checks hash("") != hash("a") != hash("b")
    # Instead, we just run collision mode as a quick check and verify the binary works
    ok, output = run_mode(binary_path, "collision", timeout=30)
    if not ok:
        return False, f"Sanity check failed (collision mode): {output}"

    collisions = parse_int(output)
    if collisions is None:
        return False, f"Could not parse collision output: {output}"

    # Also verify distribution mode produces valid output
    ok, output = run_mode(binary_path, "distribution", timeout=30)
    if not ok:
        return False, f"Sanity check failed (distribution mode): {output}"

    chi_sq = parse_float(output)
    if chi_sq is None:
        return False, f"Could not parse distribution output: {output}"

    return True, ""


# ============================================================
# Evaluation stages
# ============================================================


def stage1_correctness(binary_path: str) -> tuple[float, dict]:
    """Stage 1: Compilation already passed. Check collisions and basic distribution."""
    details = {}

    # Collision test
    ok, output = run_mode(binary_path, "collision", timeout=30)
    if not ok:
        details["collision_error"] = output
        return 0.0, details

    collisions = parse_int(output)
    if collisions is None:
        details["collision_error"] = f"Parse error: {output}"
        return 0.0, details
    details["collisions"] = collisions

    if collisions > 0:
        # Any collisions among 100k sequential keys is very bad for a 64-bit hash
        details["collision_note"] = (
            f"{collisions} collisions detected among 100k keys"
        )
        return 0.0, details

    # Distribution test
    ok, output = run_mode(binary_path, "distribution", timeout=30)
    if not ok:
        details["distribution_error"] = output
        return 0.0, details

    chi_sq = parse_float(output)
    if chi_sq is None:
        details["distribution_error"] = f"Parse error: {output}"
        return 0.0, details
    details["chi_squared"] = chi_sq

    # For 65536 buckets with 1M keys (~15.26 expected per bucket),
    # the expected chi-squared is ~65535 (df = num_buckets - 1).
    # If it's more than 2x that, it's a bad sign.
    expected_chi_sq = 65535.0
    if chi_sq > expected_chi_sq * 2:
        details["distribution_note"] = (
            f"Chi-squared {chi_sq:.0f} >> expected {expected_chi_sq:.0f}"
        )
        return 0.0, details

    return 1.0, details


def stage2_quality(binary_path: str) -> tuple[float, dict]:
    """Stage 2: Avalanche, diffusion, and bit independence."""
    details = {}
    scores = []

    # Avalanche test (ideal: 32.0)
    ok, output = run_mode(binary_path, "avalanche", timeout=30)
    if not ok:
        details["avalanche_error"] = output
        return 0.0, details

    avalanche = parse_float(output)
    if avalanche is None:
        details["avalanche_error"] = f"Parse error: {output}"
        return 0.0, details
    details["avalanche"] = avalanche

    aval_score = 1.0 - abs(avalanche - 32.0) / 32.0
    aval_score = max(0.0, min(1.0, aval_score))
    details["avalanche_score"] = aval_score
    scores.append(aval_score)

    # Diffusion test (ideal: 32.0)
    ok, output = run_mode(binary_path, "diffusion", timeout=30)
    if not ok:
        details["diffusion_error"] = output
        return 0.0, details

    diffusion = parse_float(output)
    if diffusion is None:
        details["diffusion_error"] = f"Parse error: {output}"
        return 0.0, details
    details["diffusion"] = diffusion

    diff_score = 1.0 - abs(diffusion - 32.0) / 32.0
    diff_score = max(0.0, min(1.0, diff_score))
    details["diffusion_score"] = diff_score
    scores.append(diff_score)

    # Bit independence test (ideal: 0.0)
    ok, output = run_mode(binary_path, "bitindep", timeout=30)
    if not ok:
        details["bitindep_error"] = output
        return 0.0, details

    bitindep = parse_float(output)
    if bitindep is None:
        details["bitindep_error"] = f"Parse error: {output}"
        return 0.0, details
    details["bit_independence"] = bitindep

    # Score: 1.0 if correlation < 0.005, linear down to 0.0 at correlation 0.05
    bi_score = 1.0 - min(bitindep / 0.05, 1.0)
    bi_score = max(0.0, bi_score)
    details["bitindep_score"] = bi_score
    scores.append(bi_score)

    # Weighted average: avalanche and diffusion matter most
    stage_score = 0.4 * aval_score + 0.35 * diff_score + 0.25 * bi_score
    return stage_score, details


def stage3_extended_distribution(binary_path: str) -> tuple[float, dict]:
    """Stage 3: Extended distribution tests with various key patterns."""
    details = {}

    ok, output = run_mode(binary_path, "extdist", timeout=30)
    if not ok:
        details["extdist_error"] = output
        return 0.0, details

    chi_values = parse_floats(output)
    if len(chi_values) < 5:
        details["extdist_error"] = f"Expected 5 values, got {len(chi_values)}: {output}"
        return 0.0, details

    pattern_names = [
        "last_byte_diff",
        "first_byte_diff",
        "varying_length",
        "all_zeros_lengths",
        "two_byte_keys",
    ]

    # For patterns with 256 keys in 65536 buckets:
    # expected chi-sq ≈ 65535 - 256 + 256 = ~65535 (most buckets empty)
    # Actually for sparse case (256 keys, 65536 buckets), expected chi-sq ≈ 65280
    # For 65536 keys in 65536 buckets, expected ≈ 65535

    # Simpler approach: check that chi-sq is reasonable relative to key count
    # For N keys in B buckets, expected chi-sq ≈ B - N + N*(N-1)/B when N < B
    # We just check that the distribution isn't degenerate
    key_counts = [256, 256, 256, 256, 65536]
    num_buckets = 65536

    passed = 0
    total = len(chi_values)

    for i, (chi_sq, name, nkeys) in enumerate(
        zip(chi_values, pattern_names, key_counts)
    ):
        details[f"extdist_{name}"] = chi_sq

        if nkeys < num_buckets:
            # Sparse case: most buckets empty. Expected chi-sq ≈ B - N
            # Bad hash would put everything in very few buckets
            expected = num_buckets - nkeys
            threshold = expected + nkeys * 5  # generous threshold
        else:
            # Dense case: expected ≈ num_buckets - 1
            expected = num_buckets - 1
            threshold = expected * 2

        if chi_sq <= threshold:
            passed += 1
            details[f"extdist_{name}_pass"] = True
        else:
            details[f"extdist_{name}_pass"] = False

    score = passed / total
    details["extdist_passed"] = f"{passed}/{total}"
    return score, details


def stage4_throughput(binary_path: str) -> tuple[float, dict]:
    """Stage 4: Throughput benchmark."""
    details = {}

    ok, output = run_mode(binary_path, "bench", timeout=60)
    if not ok:
        details["bench_error"] = output
        return 0.0, details

    gbps_values = parse_floats(output)
    key_lengths = [4, 8, 16, 32, 64, 128, 256, 1024]

    if len(gbps_values) < len(key_lengths):
        details["bench_error"] = (
            f"Expected {len(key_lengths)} values, got {len(gbps_values)}: {output}"
        )
        return 0.0, details

    # Weights: short keys matter more
    weights = [3.0, 3.0, 2.0, 1.0, 1.0, 0.5, 0.5, 0.5]

    weighted_sum = 0.0
    weight_total = 0.0
    for i, (gbps, klen, w) in enumerate(zip(gbps_values, key_lengths, weights)):
        details[f"bench_{klen}B_gbps"] = gbps
        weighted_sum += gbps * w
        weight_total += w

    weighted_avg = weighted_sum / weight_total if weight_total > 0 else 0.0
    details["bench_weighted_avg_gbps"] = weighted_avg

    # Normalize: 8 GB/s weighted avg = 1.0 (calibrated for typical x86)
    # Adjust this if your machine is significantly faster/slower
    reference_gbps = 8.0
    score = min(weighted_avg / reference_gbps, 1.0)
    score = max(0.0, score)
    details["throughput_score"] = score

    return score, details


def stage5_seed_independence(binary_path: str) -> tuple[float, dict]:
    """Stage 5: Seed independence test."""
    details = {}

    ok, output = run_mode(binary_path, "seedindep", timeout=30)
    if not ok:
        details["seedindep_error"] = output
        return 0.0, details

    avg_flips = parse_float(output)
    if avg_flips is None:
        details["seedindep_error"] = f"Parse error: {output}"
        return 0.0, details
    details["seed_independence"] = avg_flips

    # Ideal: 32.0 (half of 64 bits differ between seeds)
    score = 1.0 - abs(avg_flips - 32.0) / 32.0
    score = max(0.0, min(1.0, score))
    details["seed_independence_score"] = score

    return score, details


# ============================================================
# Main evaluator entry point
# ============================================================


def evaluate(program_path: str) -> dict:
    """
    Main evaluation function called by OpenEvolve.
    Returns a dict with scores and metrics.
    """
    artifacts = {}
    test_details = []

    # Count code size
    code_size = count_evolve_block_lines(program_path)
    artifacts["code_size"] = code_size

    # Create temp directory for compilation
    with tempfile.TemporaryDirectory() as tmpdir:
        binary_path = os.path.join(tmpdir, "hash_test")

        # Compile
        compile_ok, compile_err = compile_program(program_path, binary_path)
        if not compile_ok:
            test_details.append(f"COMPILE FAILED: {compile_err}")
            return {
                "score": 0.0,
                "quality": 0.0,
                "throughput": 0.0,
                "code_size": code_size,
                "combined_score": 0.0,
                "avalanche": 0.0,
                "collisions": -1,
                "compile_errors": compile_err,
                "test_details": "\n".join(test_details),
            }

        artifacts["compile_errors"] = ""
        test_details.append("Compilation: OK")

        # Sanity check
        sane, sanity_msg = sanity_check(binary_path)
        if not sane:
            test_details.append(f"SANITY CHECK FAILED: {sanity_msg}")
            return {
                "score": 0.0,
                "quality": 0.0,
                "throughput": 0.0,
                "code_size": code_size,
                "combined_score": 0.0,
                "avalanche": 0.0,
                "collisions": -1,
                "compile_errors": "",
                "test_details": "\n".join(test_details),
            }

        test_details.append("Sanity check: OK")

        # Stage 1: Correctness
        s1_score, s1_details = stage1_correctness(binary_path)
        artifacts.update(s1_details)
        test_details.append(
            f"Stage 1 (correctness): {s1_score:.2f} — "
            f"collisions={s1_details.get('collisions', '?')}, "
            f"chi_sq={s1_details.get('chi_squared', '?')}"
        )

        if s1_score == 0.0:
            test_details.append("FAILED Stage 1 — skipping remaining stages")
            return {
                "score": 0.0,
                "quality": 0.0,
                "throughput": 0.0,
                "code_size": code_size,
                "combined_score": 0.0,
                "avalanche": 0.0,
                "collisions": s1_details.get("collisions", -1),
                "compile_errors": "",
                "test_details": "\n".join(test_details),
            }

        # Stage 2: Quality
        s2_score, s2_details = stage2_quality(binary_path)
        artifacts.update(s2_details)
        test_details.append(
            f"Stage 2 (quality): {s2_score:.3f} — "
            f"avalanche={s2_details.get('avalanche', '?'):.2f}, "
            f"diffusion={s2_details.get('diffusion', '?'):.2f}, "
            f"bitindep={s2_details.get('bit_independence', '?'):.4f}"
        )

        # Stage 3: Extended distribution
        s3_score, s3_details = stage3_extended_distribution(binary_path)
        artifacts.update(s3_details)
        test_details.append(
            f"Stage 3 (extended dist): {s3_score:.2f} — "
            f"passed={s3_details.get('extdist_passed', '?')}"
        )

        # Stage 4: Throughput
        s4_score, s4_details = stage4_throughput(binary_path)
        artifacts.update(s4_details)
        test_details.append(
            f"Stage 4 (throughput): {s4_score:.3f} — "
            f"weighted_avg={s4_details.get('bench_weighted_avg_gbps', 0):.2f} GB/s"
        )

        # Stage 5: Seed independence
        s5_score, s5_details = stage5_seed_independence(binary_path)
        artifacts.update(s5_details)
        test_details.append(
            f"Stage 5 (seed indep): {s5_score:.3f} — "
            f"avg_flips={s5_details.get('seed_independence', '?')}"
        )

    # Compute composite scores
    # Quality: weighted combination of stages 1-3 and 5
    quality = 0.30 * s1_score + 0.35 * s2_score + 0.20 * s3_score + 0.15 * s5_score
    throughput = s4_score

    # Combined score: 60% quality, 40% throughput
    combined = 0.6 * quality + 0.4 * throughput

    test_details.append(f"\nFinal: quality={quality:.3f}, throughput={throughput:.3f}")
    test_details.append(f"Combined score: {combined:.3f}")

    return {
        "score": combined,
        "quality": quality,
        "throughput": throughput,
        "code_size": code_size,
        "combined_score": combined,
        "avalanche": s2_details.get("avalanche", 0.0),
        "collisions": s1_details.get("collisions", 0),
        "compile_errors": "",
        "test_details": "\n".join(test_details),
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <source.c>")
        sys.exit(1)

    results = evaluate(sys.argv[1])
    print("\n=== Evaluation Results ===")
    for key, value in sorted(results.items()):
        if key == "test_details":
            print(f"\n--- Test Details ---\n{value}")
        else:
            print(f"  {key}: {value}")
