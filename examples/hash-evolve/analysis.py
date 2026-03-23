#!/usr/bin/env python3
"""
Post-evolution analysis for evolved hash functions.

Loads top programs from an OpenEvolve checkpoint, evaluates them,
generates visualizations, and optionally runs SMHasher.

Usage:
    python analysis.py <checkpoint_dir> [--top N] [--smhasher]
"""

import argparse
import json
import math
import os
import pickle
import shutil
import subprocess
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from evaluator import (
    compile_program,
    run_mode,
    parse_float,
    parse_int,
    parse_floats,
    stage1_correctness,
    stage2_quality,
    stage3_extended_distribution,
    stage4_throughput,
    stage5_seed_independence,
    count_evolve_block_lines,
)


def load_checkpoint_programs(checkpoint_dir: str) -> list[dict]:
    """Load programs from an OpenEvolve checkpoint directory."""
    programs = []

    # Try loading from the database pickle
    db_path = os.path.join(checkpoint_dir, "database.pkl")
    if os.path.exists(db_path):
        with open(db_path, "rb") as f:
            db = pickle.load(f)

        # Extract programs from the database
        if hasattr(db, "get_top_programs"):
            top = db.get_top_programs(100)
            for prog in top:
                programs.append(
                    {
                        "source_path": getattr(prog, "source_path", None),
                        "source_code": getattr(prog, "source_code", None),
                        "score": getattr(prog, "score", 0.0),
                        "metrics": getattr(prog, "metrics", {}),
                    }
                )
        elif hasattr(db, "programs"):
            # Direct access to programs dict/list
            all_progs = db.programs if isinstance(db.programs, list) else list(db.programs.values())
            sorted_progs = sorted(
                all_progs, key=lambda p: getattr(p, "score", 0.0), reverse=True
            )
            for prog in sorted_progs[:100]:
                programs.append(
                    {
                        "source_path": getattr(prog, "source_path", None),
                        "source_code": getattr(prog, "source_code", None),
                        "score": getattr(prog, "score", 0.0),
                        "metrics": getattr(prog, "metrics", {}),
                    }
                )

    # Also check for source files directly in the checkpoint
    if not programs:
        for fname in sorted(os.listdir(checkpoint_dir)):
            if fname.endswith(".c"):
                fpath = os.path.join(checkpoint_dir, fname)
                with open(fpath, "r") as f:
                    source = f.read()
                programs.append(
                    {
                        "source_path": fpath,
                        "source_code": source,
                        "score": 0.0,
                        "metrics": {},
                    }
                )

    return programs


def evaluate_program_detailed(source_path: str) -> dict | None:
    """Run full evaluation on a program and return detailed metrics."""
    with tempfile.TemporaryDirectory() as tmpdir:
        binary_path = os.path.join(tmpdir, "hash_test")
        ok, err = compile_program(source_path, binary_path)
        if not ok:
            return {"compile_error": err}

        results = {}

        # Stage 1
        s1_score, s1_d = stage1_correctness(binary_path)
        results["s1_score"] = s1_score
        results["collisions"] = s1_d.get("collisions", -1)
        results["chi_squared"] = s1_d.get("chi_squared", -1)

        if s1_score == 0:
            return results

        # Stage 2
        s2_score, s2_d = stage2_quality(binary_path)
        results["s2_score"] = s2_score
        results["avalanche"] = s2_d.get("avalanche", 0)
        results["diffusion"] = s2_d.get("diffusion", 0)
        results["bit_independence"] = s2_d.get("bit_independence", 0)

        # Stage 3
        s3_score, s3_d = stage3_extended_distribution(binary_path)
        results["s3_score"] = s3_score
        results["extdist_passed"] = s3_d.get("extdist_passed", "?")

        # Stage 4 - throughput
        s4_score, s4_d = stage4_throughput(binary_path)
        results["s4_score"] = s4_score
        results["throughput_gbps"] = s4_d.get("bench_weighted_avg_gbps", 0)

        # Individual key-length throughput
        key_lengths = [4, 8, 16, 32, 64, 128, 256, 1024]
        for kl in key_lengths:
            results[f"gbps_{kl}B"] = s4_d.get(f"bench_{kl}B_gbps", 0)

        # Stage 5
        s5_score, s5_d = stage5_seed_independence(binary_path)
        results["s5_score"] = s5_score
        results["seed_independence"] = s5_d.get("seed_independence", 0)

        # Composite
        quality = 0.30 * s1_score + 0.35 * s2_score + 0.20 * s3_score + 0.15 * s5_score
        throughput = s4_score
        combined = 0.6 * quality + 0.4 * throughput

        results["quality"] = quality
        results["throughput"] = throughput
        results["combined_score"] = combined
        results["code_size"] = count_evolve_block_lines(source_path)

        return results


def describe_structure(source_path: str) -> str:
    """Heuristic description of the hash function's structure."""
    try:
        with open(source_path, "r") as f:
            code = f.read()
    except Exception:
        return "unknown"

    features = []

    # Check for 128-bit multiply / MUM
    if "__uint128_t" in code or "wymix" in code.lower() or "mum" in code.lower():
        features.append("MUM-based")

    # Check for multiple accumulators
    acc_count = 0
    for var in ["h0", "h1", "h2", "h3", "acc0", "acc1", "see1", "see2"]:
        if var in code:
            acc_count += 1
    if acc_count >= 2:
        features.append(f"{acc_count}-accumulator")
    elif acc_count == 0:
        features.append("single-acc")

    # Check for rotation-based mixing
    if "rotl64" in code or "rotr64" in code:
        features.append("rotate")

    # Check for multiply-xorshift finalizer
    if code.count("h *= ") >= 2 or code.count("h ^= h >>") >= 2:
        features.append("mul-xorshift")

    if not features:
        features.append("simple")

    return " + ".join(features)


def generate_visualizations(
    results: list[dict], output_dir: str
):
    """Generate matplotlib visualizations."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("matplotlib/numpy not available — skipping visualizations")
        return

    os.makedirs(output_dir, exist_ok=True)

    # Filter to valid results
    valid = [r for r in results if r.get("quality") is not None and r.get("combined_score", 0) > 0]
    if not valid:
        print("No valid results for visualization")
        return

    # --- Plot 1: Quality vs Throughput scatter ---
    fig, ax = plt.subplots(figsize=(10, 8))
    qualities = [r["quality"] for r in valid]
    throughputs = [r["throughput"] for r in valid]
    code_sizes = [r.get("code_size", 20) for r in valid]

    scatter = ax.scatter(
        throughputs,
        qualities,
        c=code_sizes,
        cmap="viridis",
        s=80,
        alpha=0.7,
        edgecolors="black",
        linewidths=0.5,
    )
    plt.colorbar(scatter, label="Code size (lines)")

    # Pareto front
    pareto_points = []
    sorted_by_throughput = sorted(zip(throughputs, qualities), key=lambda x: -x[0])
    best_quality = -1
    for t, q in sorted_by_throughput:
        if q > best_quality:
            pareto_points.append((t, q))
            best_quality = q

    if pareto_points:
        pt, pq = zip(*sorted(pareto_points))
        ax.plot(pt, pq, "r--", linewidth=2, label="Pareto front", alpha=0.8)

    ax.set_xlabel("Throughput (normalized)", fontsize=12)
    ax.set_ylabel("Quality", fontsize=12)
    ax.set_title("Hash Function Quality vs Throughput", fontsize=14)
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(output_dir, "quality_vs_throughput.png"), dpi=150)
    plt.close(fig)
    print(f"  Saved quality_vs_throughput.png")

    # --- Plot 2: Throughput by key length for top 5 ---
    top5 = sorted(valid, key=lambda r: r["combined_score"], reverse=True)[:5]
    key_lengths = [4, 8, 16, 32, 64, 128, 256, 1024]

    fig, ax = plt.subplots(figsize=(12, 6))
    x = np.arange(len(key_lengths))
    width = 0.15

    for i, r in enumerate(top5):
        gbps = [r.get(f"gbps_{kl}B", 0) for kl in key_lengths]
        offset = (i - len(top5) / 2) * width
        ax.bar(x + offset, gbps, width, label=f"#{i + 1} (score={r['combined_score']:.3f})")

    ax.set_xlabel("Key Length (bytes)", fontsize=12)
    ax.set_ylabel("Throughput (GB/s)", fontsize=12)
    ax.set_title("Throughput by Key Length — Top 5 Evolved Hashes", fontsize=14)
    ax.set_xticks(x)
    ax.set_xticklabels([str(kl) for kl in key_lengths])
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(os.path.join(output_dir, "throughput_by_keylength.png"), dpi=150)
    plt.close(fig)
    print(f"  Saved throughput_by_keylength.png")

    # --- Plot 3: Avalanche heatmap for #1 hash ---
    # This would require a detailed per-bit avalanche mode in the C harness.
    # For now, we show a summary bar chart of quality metrics for top hash.
    if top5:
        best = top5[0]
        fig, ax = plt.subplots(figsize=(8, 5))
        metrics = {
            "Avalanche\n(ideal:32)": best.get("avalanche", 0),
            "Diffusion\n(ideal:32)": best.get("diffusion", 0),
            "Seed Indep\n(ideal:32)": best.get("seed_independence", 0),
        }
        bars = ax.bar(metrics.keys(), metrics.values(), color=["#2196F3", "#4CAF50", "#FF9800"])
        ax.axhline(y=32.0, color="red", linestyle="--", label="Ideal (32.0)")
        ax.set_ylabel("Score", fontsize=12)
        ax.set_title(f"Quality Metrics — Best Hash (combined={best['combined_score']:.3f})", fontsize=14)
        ax.legend()

        for bar, val in zip(bars, metrics.values()):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.3,
                    f"{val:.2f}", ha="center", fontsize=10)

        fig.tight_layout()
        fig.savefig(os.path.join(output_dir, "best_hash_quality.png"), dpi=150)
        plt.close(fig)
        print(f"  Saved best_hash_quality.png")


def write_best_programs(
    programs: list[dict], results: list[dict], output_dir: str, top_n: int = 3
):
    """Write top N hash functions as standalone C files."""
    # Pair programs with results and sort by combined score
    paired = [
        (p, r) for p, r in zip(programs, results) if r and r.get("combined_score", 0) > 0
    ]
    paired.sort(key=lambda x: x[1]["combined_score"], reverse=True)

    for i, (prog, result) in enumerate(paired[:top_n]):
        out_path = os.path.join(output_dir, f"best_{i + 1}.c")

        # Read the source
        source = prog.get("source_code", "")
        if not source and prog.get("source_path"):
            try:
                with open(prog["source_path"], "r") as f:
                    source = f.read()
            except Exception:
                continue

        if not source:
            continue

        with open(out_path, "w") as f:
            f.write(f"// Best hash #{i + 1}\n")
            f.write(f"// Combined score: {result['combined_score']:.4f}\n")
            f.write(f"// Quality: {result['quality']:.4f}\n")
            f.write(f"// Throughput: {result['throughput']:.4f} ({result.get('throughput_gbps', 0):.2f} GB/s weighted avg)\n")
            f.write(f"// Avalanche: {result.get('avalanche', 0):.2f}\n")
            f.write(f"// Collisions: {result.get('collisions', '?')}\n")
            f.write(f"// Code size: {result.get('code_size', '?')} lines\n")
            f.write(f"//\n")
            f.write(source)

        print(f"  Wrote {out_path}")


def run_smhasher(source_path: str) -> str | None:
    """Run SMHasher on the best hash if available."""
    smhasher = shutil.which("SMHasher") or shutil.which("smhasher")
    if not smhasher:
        return None

    # For full SMHasher integration, we'd need to build the bridge.
    # For now, report that SMHasher was found.
    return f"SMHasher found at {smhasher} — use smhasher_bridge.c for full integration"


def main():
    parser = argparse.ArgumentParser(description="Analyze evolved hash functions")
    parser.add_argument("checkpoint_dir", help="Path to OpenEvolve checkpoint directory")
    parser.add_argument("--top", type=int, default=10, help="Number of top programs to analyze")
    parser.add_argument("--smhasher", action="store_true", help="Try to run SMHasher")
    parser.add_argument("--output", default=None, help="Output directory for results")
    args = parser.parse_args()

    if not os.path.isdir(args.checkpoint_dir):
        print(f"Error: {args.checkpoint_dir} is not a directory")
        sys.exit(1)

    output_dir = args.output or os.path.join(args.checkpoint_dir, "analysis")
    os.makedirs(output_dir, exist_ok=True)

    print(f"Loading programs from: {args.checkpoint_dir}")
    programs = load_checkpoint_programs(args.checkpoint_dir)
    print(f"Found {len(programs)} programs")

    if not programs:
        print("No programs found in checkpoint. Looking for .c files...")
        # Try parent directory
        parent = os.path.dirname(args.checkpoint_dir)
        for fname in os.listdir(parent):
            if fname.endswith(".c"):
                fpath = os.path.join(parent, fname)
                with open(fpath, "r") as f:
                    source = f.read()
                programs.append({
                    "source_path": fpath,
                    "source_code": source,
                    "score": 0.0,
                    "metrics": {},
                })
        print(f"Found {len(programs)} programs in parent directory")

    if not programs:
        print("No programs to analyze")
        sys.exit(1)

    # Evaluate top N programs
    top_n = min(args.top, len(programs))
    print(f"\nEvaluating top {top_n} programs...")

    results = []
    for i, prog in enumerate(programs[:top_n]):
        print(f"\n  [{i + 1}/{top_n}] Evaluating...")

        # Write source to temp file if needed
        with tempfile.TemporaryDirectory() as tmpdir:
            if prog.get("source_path") and os.path.exists(prog["source_path"]):
                source_path = prog["source_path"]
            elif prog.get("source_code"):
                source_path = os.path.join(tmpdir, "hash.c")
                with open(source_path, "w") as f:
                    f.write(prog["source_code"])
            else:
                print("    No source available")
                results.append(None)
                continue

            result = evaluate_program_detailed(source_path)
            results.append(result)

            if result:
                print(
                    f"    Score: {result.get('combined_score', 0):.3f} "
                    f"(quality={result.get('quality', 0):.3f}, "
                    f"throughput={result.get('throughput', 0):.3f})"
                )

    # Print summary table
    print("\n" + "=" * 100)
    print("RESULTS SUMMARY")
    print("=" * 100)
    print(
        f"{'Rank':<6} {'Quality':>8} {'GB/s(8B)':>9} {'GB/s(64B)':>10} "
        f"{'Avalanche':>10} {'Collisions':>11} {'Lines':>6} {'Structure'}"
    )
    print("-" * 100)

    valid_pairs = []
    for i, (prog, result) in enumerate(zip(programs[:top_n], results)):
        if result is None or result.get("combined_score", 0) == 0:
            continue

        source_path = prog.get("source_path", "")
        structure = describe_structure(source_path) if source_path else "unknown"

        valid_pairs.append((prog, result))
        print(
            f"  {i + 1:<4} {result.get('quality', 0):>8.3f} "
            f"{result.get('gbps_8B', 0):>9.2f} "
            f"{result.get('gbps_64B', 0):>10.2f} "
            f"{result.get('avalanche', 0):>10.2f} "
            f"{result.get('collisions', '?'):>11} "
            f"{result.get('code_size', '?'):>6} "
            f"{structure}"
        )

    # Generate visualizations
    print(f"\nGenerating visualizations in {output_dir}...")
    generate_visualizations(results, output_dir)

    # Write best programs
    print(f"\nWriting best programs...")
    write_best_programs(programs[:top_n], results, output_dir, top_n=3)

    # SMHasher
    if args.smhasher:
        print("\nChecking for SMHasher...")
        smhasher_result = run_smhasher("")
        if smhasher_result:
            print(f"  {smhasher_result}")
        else:
            print("  SMHasher not found in PATH")

    print(f"\nResults written to: {output_dir}")


if __name__ == "__main__":
    main()
