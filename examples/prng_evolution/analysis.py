#!/usr/bin/env python3
"""
Post-evolution analysis for the PRNG evolution example.

Usage:
    python analysis.py <checkpoint_directory>
    python analysis.py openevolve_output/checkpoints/checkpoint_500/

Produces:
    - Summary table of top generators
    - results.png — quality vs throughput scatter + Pareto front
    - best_1.c, best_2.c, best_3.c — top 3 standalone source files
    - Optional PractRand run on #1 generator (up to 1 TB)
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def load_checkpoint(checkpoint_dir: str) -> List[Dict[str, Any]]:
    programs_dir = os.path.join(checkpoint_dir, "programs")
    if not os.path.isdir(programs_dir):
        print(f"Error: {programs_dir} not found")
        sys.exit(1)
    programs: List[Dict[str, Any]] = []
    for fname in sorted(os.listdir(programs_dir)):
        if fname.endswith(".json"):
            with open(os.path.join(programs_dir, fname)) as f:
                programs.append(json.load(f))
    return programs


def m(prog: Dict[str, Any], key: str, default: float = 0.0) -> float:
    return float(prog.get("metrics", {}).get(key, default))


def rank(programs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(programs, key=lambda p: m(p, "combined_score"), reverse=True)


def plot_results(programs: List[Dict[str, Any]], output_path: str) -> None:
    quality = [m(p, "quality") for p in programs]
    throughput = [m(p, "ops_per_sec") / 1e6 for p in programs]  # M ops/s
    state_bits = [m(p, "state_bits") for p in programs]

    fig, ax = plt.subplots(figsize=(10, 7))
    sc = ax.scatter(throughput, quality, c=state_bits, cmap="viridis",
                    s=20, alpha=0.5, edgecolors="none")
    fig.colorbar(sc, ax=ax, label="State size (bits)")

    # Pareto front
    pts = np.array(list(zip(throughput, quality)))
    if len(pts) > 0:
        pareto = np.ones(len(pts), dtype=bool)
        for i in range(len(pts)):
            if pareto[i]:
                pareto &= ~((pts[:, 0] >= pts[i, 0]) & (pts[:, 1] >= pts[i, 1])
                            & ((pts[:, 0] > pts[i, 0]) | (pts[:, 1] > pts[i, 1])))
                pareto[i] = True
        pp = pts[pareto]
        if len(pp) > 0:
            order = np.argsort(pp[:, 0])
            ax.plot(pp[order, 0], pp[order, 1], "r-o", markersize=5,
                    linewidth=2, label="Pareto front")

    ax.set_xlabel("Throughput (M ops/sec)")
    ax.set_ylabel("Quality (PractRand + basic stats)")
    ax.set_title("PRNG Evolution — Quality vs Throughput")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    print(f"  Saved {output_path}")


def summarize_structure(code: str) -> str:
    """One-line summary of the generator's structure from its code."""
    n_state = code.count("uint64_t") - 1  # subtract the return type
    has_mul = "*" in code.split("rng_next")[1] if "rng_next" in code else False
    has_rot = "rotl64" in code or "rotr64" in code
    has_add = "+=" in code or ("+" in code.split("rng_next")[1]) if "rng_next" in code else False
    parts = []
    parts.append(f"{max(1, n_state)}-word state")
    ops = []
    if has_rot:
        ops.append("rotate")
    if has_mul:
        ops.append("multiply")
    if has_add:
        ops.append("add")
    ops.append("xor-shift")
    parts.append("+".join(ops))
    return ", ".join(parts)


def write_best(programs: List[Dict[str, Any]], output_dir: str) -> List[str]:
    ranked = rank(programs)
    paths: List[str] = []
    for i, prog in enumerate(ranked[:3]):
        code = prog.get("code", "")
        if not code:
            continue
        path = os.path.join(output_dir, f"best_{i+1}.c")
        with open(path, "w") as f:
            f.write(code)
        paths.append(path)
    return paths


def run_practrand(source: str, max_bytes: str = "1TB") -> Optional[str]:
    script = os.path.join(os.path.dirname(__file__), "practrand_test.sh")
    if not shutil.which("RNG_test") or not os.path.isfile(script):
        print("  PractRand or practrand_test.sh not found — skipping")
        return None
    print(f"  Running PractRand on {source} up to {max_bytes} ...")
    try:
        r = subprocess.run(["bash", script, source, max_bytes],
                           capture_output=True, text=True, timeout=7200)
        output = r.stdout + r.stderr
        print(output[-2000:] if len(output) > 2000 else output)
        return output
    except subprocess.TimeoutExpired:
        print("  PractRand timed out")
        return None


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python analysis.py <checkpoint_directory>")
        sys.exit(1)

    checkpoint_dir = sys.argv[1]
    programs = load_checkpoint(checkpoint_dir)
    print(f"Loaded {len(programs)} programs\n")
    if not programs:
        sys.exit(1)

    ranked = rank(programs)

    # Summary table
    print(f"{'Rank':<6}{'Quality':<10}{'Throughput(M ops/s)':<22}{'State(bits)':<14}{'Structure'}")
    print("-" * 80)
    for i, prog in enumerate(ranked[:20]):
        q = m(prog, "quality")
        tp = m(prog, "ops_per_sec") / 1e6
        sb = int(m(prog, "state_bits"))
        struct = summarize_structure(prog.get("code", ""))
        print(f"{i+1:<6}{q:<10.4f}{tp:<22.0f}{sb:<14}{struct}")

    # Plot
    output_dir = os.path.dirname(os.path.abspath(checkpoint_dir.rstrip("/")))
    print(f"\nGenerating plot ...")
    plot_results(programs, os.path.join(output_dir, "results.png"))

    # Best sources
    print("\nExtracting top 3 generators ...")
    paths = write_best(programs, output_dir)
    for i, p in enumerate(paths):
        q = m(ranked[i], "quality")
        tp = m(ranked[i], "ops_per_sec") / 1e6
        print(f"  best_{i+1}.c — quality={q:.4f}, throughput={tp:.0f} M ops/s")

    # PractRand
    if paths:
        print(f"\nPractRand extended test on best generator ...")
        run_practrand(paths[0])

    print("\nDone.")


if __name__ == "__main__":
    main()
