# Hash Function Evolution with OpenEvolve

Evolve novel non-cryptographic hash functions using LLM-guided evolutionary search. The hash functions are written in C for realistic benchmarking and direct usability; OpenEvolve's Python evaluator compiles and tests them automatically.

## Goal

Discover hash functions that achieve excellent **distribution quality** (passing SMHasher-style tests) while maximizing **throughput**, exploring the tradeoff space between speed, quality, and code complexity.

## Prerequisites

- **Python 3.10+** with OpenEvolve installed (`pip install -e ".[dev]"` from repo root)
- **gcc** (with `-march=native` and `__uint128_t` support)
- **numpy**, **scipy**, **matplotlib** (for analysis)
- **Optional:** [SMHasher](https://github.com/aappleby/smhasher) for publication-quality validation

## Quick Start

```bash
# 1. Calibrate the evaluator with reference hashes
python examples/hash-evolve/validate.py

# 2. Run evolution
python openevolve-run.py \
    examples/hash-evolve/initial_program.c \
    examples/hash-evolve/evaluator.py \
    --config examples/hash-evolve/config.yaml \
    --iterations 500

# 3. Analyze results
python examples/hash-evolve/analysis.py \
    openevolve_output/checkpoints/checkpoint_500/ \
    --smhasher
```

## Project Files

| File | Description |
|------|-------------|
| `initial_program.c` | Seed hash function (naive byte loop) with helper utilities |
| `test_harness.c` | Test harness compiled with the evolved program via `-include` |
| `evaluator.py` | Multi-stage evaluator: compile, test quality, benchmark speed |
| `config.yaml` | OpenEvolve configuration (models, islands, MAP-Elites) |
| `reference_hashes.c` | Reference implementations for calibration (bad, FNV-1a, MurmurHash3, wyhash) |
| `validate.py` | Evaluator calibration — verifies scoring range and sensitivity |
| `analysis.py` | Post-evolution analysis with visualizations |
| `smhasher_bridge.c` | Bridge for testing evolved hashes with SMHasher |

## How It Works

### Architecture

The initial program is kept minimal to reduce token usage during evolution. Only the hash function itself (the EVOLVE-BLOCK) is sent to the LLM:

- `initial_program.c` — helper functions (`rotl64`, `read64`, etc.) and the EVOLVE-BLOCK containing `hash_function()`
- `test_harness.c` — all test modes and `main()`, compiled separately

The evaluator compiles them together using `gcc -include evolved_program.c test_harness.c`, so the test harness is never seen by the LLM.

### Evolution Block

Only the code between `EVOLVE-BLOCK-START` and `EVOLVE-BLOCK-END` markers is modified during evolution. The initial implementation is a naive byte-by-byte loop with a simple finalizer — intentionally weak to give the LLM room to discover:

- **128-bit multiply (MUM)** — the core primitive of all modern fast hashes
- **Bulk word reads** — processing 8-16 bytes at a time instead of byte-by-byte
- **Overlapping reads for short keys** — branchless handling of 1-16 byte keys
- **Multi-accumulator designs** — parallel state for instruction-level parallelism
- **Better finalization** — multiply-xorshift rounds or MUM-based mixing

### Evaluation Pipeline

The evaluator uses a cascade of increasingly expensive tests:

| Stage | Test | Time | What it catches |
|-------|------|------|----------------|
| 1 | Compilation + collisions + basic distribution | ~2s | Broken code, degenerate hashes |
| 2 | Avalanche + diffusion + bit independence | ~5s | Poor mixing, correlated output bits |
| 3 | Extended distribution (5 key patterns) | ~5s | Pattern-dependent bias |
| 4 | Throughput benchmark (8 key lengths) | ~30s | Speed at various key sizes |
| 5 | Seed independence | ~5s | Hashes that ignore the seed |

Programs that fail early stages are rejected quickly, saving compute for promising candidates.

### Scoring

- **Quality** (60% of final score): Weighted combination of stages 1-3 and 5
- **Throughput** (40% of final score): Weighted-average GB/s favoring short keys (4B and 8B get 3x weight)
- **MAP-Elites dimensions**: `throughput` and `code_size` — maintains diversity between fast-simple and complex-thorough hashes

### Test Descriptions

- **Avalanche**: For random 16-byte keys, flip each input bit and measure how many output bits change. Ideal: 32.0 (exactly half of 64 bits flip, on average).
- **Distribution**: Hash 1M sequential integers into 65536 buckets. Measure chi-squared uniformity. Lower is better; expected ~65535 for a random function.
- **Collision**: Hash 100k sequential integer keys and count collisions. A good 64-bit hash should have zero collisions for this set size.
- **Bit Independence**: For random 8-byte keys, measure Pearson correlation between sampled pairs of output bits. Ideal: 0.0 (no correlation).
- **Diffusion**: Like avalanche, but specifically tests 8-byte keys (single-chunk processing). Catches functions with good bulk throughput but poor single-chunk quality.
- **Throughput**: Time hashing at key lengths from 4B to 1024B. Reports GB/s.
- **Extended Distribution**: Tests with keys that differ only in one byte, varying-length same-content keys, all-zeros keys, and all 2-byte keys.
- **Seed Independence**: Hash the same keys with many seed pairs, verify outputs are uncorrelated.

## Adding Custom Test Patterns

To add a new distribution pattern test:

1. Add a new test function in `test_harness.c`
2. Add the mode to `main()` dispatch in `test_harness.c`
3. Add the corresponding parsing and scoring in `evaluator.py`

Example — testing keys with a common prefix:

```c
void test_common_prefix(void) {
    const int num_buckets = 65536;
    uint32_t *buckets = calloc(num_buckets, sizeof(uint32_t));
    uint8_t key[32];
    memset(key, 0x42, 24);  // Common 24-byte prefix
    for (int i = 0; i < 65536; i++) {
        key[24] = i & 0xFF;
        key[25] = (i >> 8) & 0xFF;
        uint64_t h = hash_function(key, 26, 0xDEADBEEF);
        buckets[h % num_buckets]++;
    }
    // ... compute and print chi-squared
}
```

## Testing with SMHasher

For publication-quality validation of evolved hashes:

1. Build the SMHasher bridge:
   ```bash
   gcc -O2 -march=native -o smhasher_bridge examples/hash-evolve/smhasher_bridge.c -lm
   ```

2. Run built-in tests:
   ```bash
   ./smhasher_bridge --test all
   ```

3. For full SMHasher integration, paste your evolved hash's EVOLVE-BLOCK into `smhasher_bridge.c` and link against the SMHasher library. See the comments in that file for details.

## Understanding the Evolution

Common patterns the LLM discovers during evolution:

1. **MUM mixing**: Multiplying two 64-bit values via `__uint128_t` to get a 128-bit result, then XORing the halves. This is the core of rapidhash/wyhash/komihash and provides excellent mixing per cycle.

2. **Bulk word reads**: Replacing the byte-by-byte loop with `read64`/`read32` calls to process 8-16 bytes per iteration. The single biggest throughput improvement.

3. **Overlapping reads for short keys**: Reading from both the start and end of the key (with overlap for small sizes) to handle 1-16 byte keys without branches.

4. **Multi-accumulator designs**: Using 2-4 independent hash state variables that are combined at the end. This maximizes instruction-level parallelism for long keys.

5. **Better finalizers**: Discovering that two rounds of multiply-xorshift is superior to one round, or that a final MUM can replace the finalizer entirely.

## Configuration Tuning

Key configuration parameters to adjust:

- `llm.temperature`: Higher (0.9-1.0) for more exploration, lower (0.6-0.7) to refine near-optimal solutions
- `database.num_islands`: More islands = more diversity; 5 is a good default
- `database.migration_interval`: Lower = faster convergence; higher = more independent exploration
- `evaluator.timeout`: Increase if benchmarks need more time for stable measurements
- `max_iterations`: 500 is usually enough to find good designs; 1000+ for thorough exploration
