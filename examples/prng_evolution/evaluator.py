"""
Evaluator for the PRNG evolution example.

Compiles an evolved C source, runs PractRand for quality assessment,
and benchmarks throughput.

Cascade:
  Stage 1  — Compile + sanity check                      (~1 s)
  Stage 2  — Basic stats + PractRand to 256 MB            (~15 s)
  Stage 3  — PractRand to 64 GB + throughput benchmark    (~10 min)

Quality is determined by how far the generator gets through PractRand
before failing.  This is the gold-standard for PRNG testing.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import tempfile
import traceback
from typing import Dict, List, Tuple

import numpy as np

from openevolve.evaluation_result import EvaluationResult

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
COMPILE_TIMEOUT = 15        # seconds
SANITY_TIMEOUT = 10         # seconds — generating 1M values for sanity
BENCH_TIMEOUT = 30          # seconds
PRACTRAND_STAGE2_MAX = "256MB"   # stage 2 — quick PractRand filter
PRACTRAND_STAGE2_TIMEOUT = 120   # seconds
PRACTRAND_STAGE3_MAX = "64GB"    # stage 3 — full PractRand
PRACTRAND_STAGE3_TIMEOUT = 900   # seconds (~10 min for fast generators)

GCC_FLAGS = ["-O2", "-march=native", "-lm"]

# Throughput normalisation: 1.0 ≙ 1.5 G ops/s
# (RomuDuoJr ≈ 1.22 Gop/s, wyrand ≈ 1.16 Gop/s → score ~0.8)
THROUGHPUT_CEIL = 1_500_000_000.0

# Final score weighting
QUALITY_WEIGHT = 0.7
THROUGHPUT_WEIGHT = 0.3

# PractRand size progression (bytes) — each step is 2× the previous
# 2^8 (256B) through 2^36 (64GB) = 29 levels
PRACTRAND_LEVELS = [
    "256B", "512B", "1KB", "2KB", "4KB", "8KB", "16KB", "32KB",
    "64KB", "128KB", "256KB", "512KB", "1MB", "2MB", "4MB", "8MB",
    "16MB", "32MB", "64MB", "128MB", "256MB", "512MB", "1GB", "2GB",
    "4GB", "8GB", "16GB", "32GB", "64GB",
]
TOTAL_LEVELS = len(PRACTRAND_LEVELS)  # 29

# Reference throughputs for artifacts (so the LLM knows the competition)
REFERENCE_THROUGHPUT = "RomuDuoJr=1.22G, wyrand=1.16G, evolved_best=1.04G"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compile(source_path: str, binary_path: str) -> Tuple[bool, str]:
    """Compile C source.  Returns (success, stderr_text)."""
    try:
        r = subprocess.run(
            ["gcc"] + GCC_FLAGS + ["-o", binary_path, source_path],
            capture_output=True, text=True, timeout=COMPILE_TIMEOUT,
        )
        return r.returncode == 0, r.stderr
    except subprocess.TimeoutExpired:
        return False, "compilation timed out"


def _run_binary_bytes(binary: str, count_millions: int | None = None,
                      byte_limit: int = 8_000_000,
                      timeout: int = SANITY_TIMEOUT) -> bytes:
    """Run binary in 'binary' mode, return raw bytes."""
    cmd = [binary, "binary"]
    if count_millions is not None:
        cmd.append(str(count_millions))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        data = proc.stdout.read(byte_limit)  # type: ignore[union-attr]
        proc.kill()
        proc.wait()
        return data
    except Exception:
        proc.kill()
        proc.wait()
        raise


# ---------------------------------------------------------------------------
# Stage 1 – Sanity
# ---------------------------------------------------------------------------

def _stage1(binary: str) -> Tuple[float, str]:
    """Quick sanity: compile OK, outputs non-trivial data, no short cycle."""
    lines: List[str] = []

    # Generate ~1M outputs (8 MB)
    data = _run_binary_bytes(binary, count_millions=1, byte_limit=8_000_000, timeout=SANITY_TIMEOUT)
    vals = np.frombuffer(data[:len(data) // 8 * 8], dtype="<u8")

    if len(vals) < 1000:
        return 0.0, "too few outputs"

    if np.all(vals == vals[0]):
        return 0.0, "all outputs identical"

    if np.all(vals == 0):
        return 0.0, "all outputs zero"

    # Bit-span check
    combined = int(np.bitwise_or.reduce(vals))
    bit_span = bin(combined).count("1")
    if bit_span < 50:
        lines.append(f"bit span only {bit_span}/64")

    # Short-cycle check: first 10k must be unique
    first_10k = vals[:10_000]
    n_unique = len(np.unique(first_10k))
    if n_unique < 10_000:
        return 0.0, f"short cycle: {n_unique}/10000 unique"

    score = 1.0 if bit_span >= 60 else 0.5
    lines.append(f"bit_span={bit_span}, unique_10k={n_unique}")
    return score, "; ".join(lines)


# ---------------------------------------------------------------------------
# Basic statistical tests (fast, give partial credit to mediocre generators)
# ---------------------------------------------------------------------------

def _basic_stats(binary: str) -> Tuple[float, str]:
    """Fast byte-level tests on 8 MB of output.

    Provides a 0-1 score that acts as a floor for generators that fail
    PractRand early.  Good generators get ~1.0, bad ones get ~0.0-0.2,
    mediocre generators (like plain xorshift) get ~0.6-0.8.
    """
    from scipy import stats as sp_stats

    data = _run_binary_bytes(binary, byte_limit=8_000_000, timeout=SANITY_TIMEOUT)
    vals = np.frombuffer(data[:len(data) // 8 * 8], dtype="<u8")
    if len(vals) < 10_000:
        return 0.0, "too few values for basic stats"

    scores: List[float] = []
    details: List[str] = []

    # 1. Byte chi-squared on each of 8 byte positions
    raw = vals.view(np.uint8)
    byte_pass = 0
    for byte_idx in range(8):
        stream = raw[byte_idx::8][:len(vals)]
        observed = np.bincount(stream, minlength=256).astype(np.float64)
        expected = np.full(256, len(stream) / 256.0)
        _, p = sp_stats.chisquare(observed, f_exp=expected)
        if 0.01 <= p <= 0.99:
            byte_pass += 1
    byte_score = byte_pass / 8.0
    scores.append(byte_score)
    details.append(f"byte_chi2={byte_score:.2f}({byte_pass}/8)")

    # 2. Bit frequency: fraction of 64 bit positions within normal range
    n = len(vals)
    bit_pass = 0
    for bit in range(64):
        ones = int(np.count_nonzero(vals & np.uint64(1 << bit)))
        p_hat = ones / n
        z = abs(p_hat - 0.5) / (0.5 / math.sqrt(n))
        p = 2.0 * (1.0 - sp_stats.norm.cdf(z))
        if p >= 0.001:
            bit_pass += 1
    bit_score = bit_pass / 64.0
    scores.append(bit_score)
    details.append(f"bit_freq={bit_score:.2f}({bit_pass}/64)")

    # 3. Serial correlation (lag-1) on full uint64 stream
    fv = vals[:100_000].astype(np.float64)
    std = fv.std()
    if std > 0:
        fv_norm = (fv - fv.mean()) / std
        corr = float(np.corrcoef(fv_norm[:-1], fv_norm[1:])[0, 1])
        serial_score = max(0.0, 1.0 - abs(corr) * 100.0)
    else:
        serial_score = 0.0
    scores.append(serial_score)
    details.append(f"serial={serial_score:.2f}")

    # 4. Lower-byte pair uniformity (catches xorshift weakness partially)
    n_pairs = min(len(vals) - 1, 500_000)
    lo = (vals[:n_pairs + 1] & np.uint64(0xFF)).astype(np.int32)
    pair_idx = lo[:-1] * 256 + lo[1:]
    obs = np.bincount(pair_idx, minlength=65536).astype(np.float64)
    exp = float(n_pairs) / 65536.0
    if exp >= 2.0:
        _, p = sp_stats.chisquare(obs, f_exp=np.full(65536, exp))
        lbp_score = 1.0 if (0.01 <= p <= 0.99) else 0.0
    else:
        lbp_score = 0.5
    scores.append(lbp_score)
    details.append(f"lo_byte_pairs={'pass' if lbp_score > 0.5 else 'FAIL'}")

    total = float(np.mean(scores))
    return total, " ".join(details)


# ---------------------------------------------------------------------------
# PractRand quality scoring
# ---------------------------------------------------------------------------

def _run_practrand(binary: str, max_size: str = PRACTRAND_STAGE3_MAX,
                   timeout: int = PRACTRAND_STAGE3_TIMEOUT) -> Tuple[float, int, str]:
    """Run PractRand and determine quality score.

    Score = fraction of levels passed (0 to 1).
    Parses PractRand's progressive output to find the last passing level.

    Returns (quality_score, levels_passed, details_string).
    """
    gen_proc = None
    prt_proc = None
    try:
        gen_proc = subprocess.Popen(
            [binary, "binary"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        prt_proc = subprocess.Popen(
            ["RNG_test", "stdin64", "-tlmax", max_size],
            stdin=gen_proc.stdout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )
        gen_proc.stdout.close()  # type: ignore[union-attr]

        output, _ = prt_proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if prt_proc:
            prt_proc.kill()
            prt_proc.wait()
            try:
                output = prt_proc.stdout.read()  # type: ignore[union-attr]
            except Exception:
                output = ""
        else:
            output = ""
    except Exception as exc:
        return 0.0, 0, f"PractRand error: {exc}"
    finally:
        for p in (gen_proc, prt_proc):
            if p:
                try:
                    p.kill()
                    p.wait()
                except Exception:
                    pass

    if not output:
        return 0.0, 0, "PractRand produced no output"

    # Parse output to find the highest level that passed.
    # PractRand prints lines like:
    #   length= 64 kilobytes (2^16 bytes), time= 0.1 seconds
    #     ...
    #   length= 128 kilobytes (2^17 bytes), time= 0.2 seconds
    # When a test fails it adds lines like "FAIL" or "VERY SUSPICIOUS".
    # If it reaches the max without failure, all levels passed.

    # Find all "length=" lines and check if they have failures after them
    level_pattern = re.compile(
        r"length=\s*([\d.]+)\s*(byte|kilobyte|megabyte|gigabyte|terabyte)s?\s*\(2\^(\d+)\s*bytes\)",
        re.IGNORECASE,
    )
    fail_pattern = re.compile(r"\bFAIL\b", re.IGNORECASE)
    suspicious_pattern = re.compile(r"\bVERY SUSPICIOUS\b", re.IGNORECASE)

    # Split output into sections per length checkpoint
    sections = re.split(r"(?=\s*length=)", output)

    max_passing_power = -1  # 2^N bytes that passed
    last_tested_power = -1
    failure_detail = ""

    for section in sections:
        m = level_pattern.search(section)
        if not m:
            continue
        power = int(m.group(3))
        last_tested_power = max(last_tested_power, power)

        if fail_pattern.search(section) or suspicious_pattern.search(section):
            # This level failed
            failure_detail = section.strip()
            break
        else:
            max_passing_power = power

    # Convert passing power to a fraction of total levels
    # PractRand levels go from 2^8 (256B) up to 2^36 (64GB) = 29 levels
    if max_passing_power < 8:
        # Didn't pass even 256 bytes
        levels_passed = 0
        quality = 0.0
    else:
        # Level index: 2^8=level 0, 2^9=level 1, ..., 2^36=level 28
        levels_passed = max_passing_power - 8 + 1  # +1 because passing level 0 = 1/29
        quality = min(1.0, levels_passed / TOTAL_LEVELS)

    # Build readable summary
    if max_passing_power < 8:
        summary = "FAILED at first checkpoint (256 bytes)"
    else:
        size_names = {8: "256B", 9: "512B", 10: "1KB", 11: "2KB", 12: "4KB",
                      13: "8KB", 14: "16KB", 15: "32KB", 16: "64KB",
                      17: "128KB", 18: "256KB", 19: "512KB", 20: "1MB",
                      21: "2MB", 22: "4MB", 23: "8MB", 24: "16MB",
                      25: "32MB", 26: "64MB", 27: "128MB", 28: "256MB",
                      29: "512MB", 30: "1GB", 31: "2GB", 32: "4GB",
                      33: "8GB", 34: "16GB", 35: "32GB", 36: "64GB"}
        passed_name = size_names.get(max_passing_power, f"2^{max_passing_power}B")
        summary = f"Passed up to {passed_name}"
        if last_tested_power > max_passing_power:
            failed_name = size_names.get(last_tested_power, f"2^{last_tested_power}B")
            summary += f", failed at {failed_name}"
        elif max_passing_power >= 36:
            summary += " (reached maximum test length)"

    details = f"PractRand: {summary} (levels={levels_passed}/{TOTAL_LEVELS}, quality={quality:.3f})"
    if failure_detail:
        # Extract just the FAIL lines for artifact brevity
        fail_lines = [l.strip() for l in failure_detail.split("\n")
                      if "FAIL" in l or "SUSPICIOUS" in l]
        if fail_lines:
            details += "\n  " + "\n  ".join(fail_lines[:5])

    return quality, levels_passed, details


# ---------------------------------------------------------------------------
# Throughput
# ---------------------------------------------------------------------------

def _get_throughput(binary: str) -> Tuple[float, float, int, str]:
    """Benchmark + statesize.  Returns (throughput_score, ops_sec, state_bits, details)."""
    details: List[str] = []
    ops_sec = 0.0
    state_bits = 0

    try:
        r = subprocess.run([binary, "bench"], capture_output=True, text=True,
                           timeout=BENCH_TIMEOUT)
        if r.returncode == 0:
            ops_sec = float(r.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError):
        details.append("bench timed out or invalid output")

    try:
        r = subprocess.run([binary, "statesize"], capture_output=True, text=True,
                           timeout=5)
        if r.returncode == 0:
            state_bits = int(r.stdout.strip()) * 8
    except (subprocess.TimeoutExpired, ValueError):
        details.append("statesize failed")

    throughput_score = min(1.0, ops_sec / THROUGHPUT_CEIL)
    details.append(f"ops/sec={ops_sec:.0f}  throughput_score={throughput_score:.3f}  state_bits={state_bits}")
    return throughput_score, ops_sec, state_bits, "; ".join(details)


# ---------------------------------------------------------------------------
# Public API for OpenEvolve
# ---------------------------------------------------------------------------

def evaluate_stage1(program_path: str) -> EvaluationResult:
    """Stage 1: compile + sanity.  Fast filter for broken candidates (~1s)."""
    binary_path = ""
    try:
        binary_path = tempfile.mktemp(suffix=".bin")
        ok, compile_err = _compile(program_path, binary_path)
        if not ok:
            return EvaluationResult(
                metrics={"combined_score": 0.0, "quality": 0.0,
                         "throughput": 0.0, "state_bits": 0},
                artifacts={"compile_errors": compile_err,
                           "test_details": "compilation failed",
                           "suggestion": "Fix C syntax errors or missing includes."},
            )
        score, detail = _stage1(binary_path)
        return EvaluationResult(
            metrics={"combined_score": float(score * 0.1),  # low ceiling — stage1 alone is not enough
                     "quality": 0.0, "throughput": 0.0, "state_bits": 0},
            artifacts={"compile_errors": "", "test_details": f"[stage1] {detail}"},
        )
    except Exception as exc:
        return EvaluationResult(
            metrics={"combined_score": 0.0, "quality": 0.0,
                     "throughput": 0.0, "state_bits": 0},
            artifacts={"compile_errors": "", "test_details": f"stage1 crash: {exc}",
                       "traceback": traceback.format_exc()},
        )
    finally:
        if binary_path and os.path.exists(binary_path):
            os.unlink(binary_path)


def evaluate_stage2(program_path: str) -> EvaluationResult:
    """Stage 2: basic stats + PractRand to 256 MB.  Medium filter (~15s)."""
    binary_path = ""
    all_details: List[str] = []
    try:
        binary_path = tempfile.mktemp(suffix=".bin")
        ok, compile_err = _compile(program_path, binary_path)
        if not ok:
            return EvaluationResult(
                metrics={"combined_score": 0.0, "quality": 0.0,
                         "throughput": 0.0, "state_bits": 0},
                artifacts={"compile_errors": compile_err,
                           "test_details": "compilation failed",
                           "suggestion": "Fix C syntax errors or missing includes."},
            )

        # --- Sanity ---
        s1_score, s1_detail = _stage1(binary_path)
        all_details.append(f"[sanity] score={s1_score:.3f}: {s1_detail}")
        if s1_score <= 0.0:
            return EvaluationResult(
                metrics={"combined_score": 0.0, "quality": 0.0,
                         "throughput": 0.0, "state_bits": 0},
                artifacts={"compile_errors": "",
                           "test_details": "\n".join(all_details),
                           "suggestion": "Generator fails basic sanity: zero output, identical values, or short cycle."},
            )

        # --- Basic stats ---
        basic_score, basic_detail = _basic_stats(binary_path)
        all_details.append(f"[basic_stats] score={basic_score:.3f}: {basic_detail}")

        # --- PractRand to 256 MB (quick filter) ---
        pr_score, pr_levels, pr_detail = _run_practrand(
            binary_path, max_size=PRACTRAND_STAGE2_MAX,
            timeout=PRACTRAND_STAGE2_TIMEOUT,
        )
        all_details.append(f"[practrand_256MB] {pr_detail}")

        # Blend quality from basic stats and PractRand-256MB
        quality = 0.4 * basic_score + 0.6 * pr_score

        # Quick throughput estimate
        tp_score, ops_sec, state_bits, tp_detail = _get_throughput(binary_path)
        all_details.append(f"[throughput] {tp_detail}")

        combined = QUALITY_WEIGHT * quality + THROUGHPUT_WEIGHT * tp_score

        return EvaluationResult(
            metrics={
                "combined_score": float(combined),
                "quality": float(quality),
                "throughput": float(tp_score),
                "state_bits": float(state_bits),
                "ops_per_sec": float(ops_sec),
                "practrand_score": float(pr_score),
                "practrand_levels_passed": float(pr_levels),
                "basic_stats": float(basic_score),
            },
            artifacts={
                "compile_errors": "",
                "test_details": "\n".join(all_details),
                "throughput_ops_per_sec": f"{ops_sec:.0f}",
                "reference_throughput": REFERENCE_THROUGHPUT,
                "note": "Stage 2 — PractRand limited to 256 MB. Full 64 GB test in stage 3.",
            },
        )

    except Exception as exc:
        return EvaluationResult(
            metrics={"combined_score": 0.0, "quality": 0.0,
                     "throughput": 0.0, "state_bits": 0},
            artifacts={
                "compile_errors": "",
                "test_details": f"stage2 crash: {exc}\n" + "\n".join(all_details),
                "traceback": traceback.format_exc(),
            },
        )
    finally:
        if binary_path and os.path.exists(binary_path):
            os.unlink(binary_path)


def evaluate_stage3(program_path: str) -> EvaluationResult:
    """Stage 3: full PractRand to 64 GB + throughput.  Final evaluation (~10 min)."""
    return evaluate(program_path)


def evaluate(program_path: str) -> EvaluationResult:
    """Full evaluation: sanity + basic stats + PractRand to 64 GB + throughput."""
    binary_path = ""
    all_details: List[str] = []
    try:
        binary_path = tempfile.mktemp(suffix=".bin")
        ok, compile_err = _compile(program_path, binary_path)
        if not ok:
            return EvaluationResult(
                metrics={"combined_score": 0.0, "quality": 0.0,
                         "throughput": 0.0, "state_bits": 0},
                artifacts={"compile_errors": compile_err,
                           "test_details": "compilation failed",
                           "suggestion": "Fix C syntax errors or missing includes."},
            )

        # --- Sanity ---
        s1_score, s1_detail = _stage1(binary_path)
        all_details.append(f"[sanity] score={s1_score:.3f}: {s1_detail}")
        if s1_score <= 0.0:
            return EvaluationResult(
                metrics={"combined_score": 0.0, "quality": 0.0,
                         "throughput": 0.0, "state_bits": 0},
                artifacts={"compile_errors": "",
                           "test_details": "\n".join(all_details),
                           "suggestion": "Generator fails basic sanity: zero output, identical values, or short cycle."},
            )

        # --- Basic stats (fast, gives gradient for mediocre generators) ---
        basic_score, basic_detail = _basic_stats(binary_path)
        all_details.append(f"[basic_stats] score={basic_score:.3f}: {basic_detail}")

        # --- PractRand to 64 GB (authoritative quality test) ---
        pr_score, pr_levels, pr_detail = _run_practrand(
            binary_path, max_size=PRACTRAND_STAGE3_MAX,
            timeout=PRACTRAND_STAGE3_TIMEOUT,
        )
        all_details.append(f"[practrand_64GB] {pr_detail}")

        # --- Blend: basic stats provide a floor, PractRand provides the ceiling.
        # quality = 0.4 * basic_stats + 0.6 * practrand
        quality = 0.4 * basic_score + 0.6 * pr_score

        # --- Throughput ---
        tp_score, ops_sec, state_bits, tp_detail = _get_throughput(binary_path)
        all_details.append(f"[throughput] {tp_detail}")

        # --- Combined ---
        combined = QUALITY_WEIGHT * quality + THROUGHPUT_WEIGHT * tp_score

        return EvaluationResult(
            metrics={
                "combined_score": float(combined),
                "quality": float(quality),
                "throughput": float(tp_score),
                "state_bits": float(state_bits),
                "ops_per_sec": float(ops_sec),
                "practrand_score": float(pr_score),
                "practrand_levels_passed": float(pr_levels),
                "basic_stats": float(basic_score),
            },
            artifacts={
                "compile_errors": "",
                "test_details": "\n".join(all_details),
                "throughput_ops_per_sec": f"{ops_sec:.0f}",
                "reference_throughput": REFERENCE_THROUGHPUT,
            },
        )

    except Exception as exc:
        return EvaluationResult(
            metrics={"combined_score": 0.0, "quality": 0.0,
                     "throughput": 0.0, "state_bits": 0},
            artifacts={
                "compile_errors": "",
                "test_details": f"evaluator crash: {exc}\n" + "\n".join(all_details),
                "traceback": traceback.format_exc(),
            },
        )
    finally:
        if binary_path and os.path.exists(binary_path):
            os.unlink(binary_path)
