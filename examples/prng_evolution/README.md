# PRNG Evolution

Evolve high-quality, high-throughput 64-bit pseudo-random number generators using OpenEvolve.

## How it works

OpenEvolve starts with a competitive 2-word add/rotate/xor generator in C and iteratively evolves the state structure and `rng_next()` function using LLM-guided mutations. Each candidate goes through a 3-stage cascade:

1. **Stage 1 — Compile + sanity** (~1s): Compiles with `gcc -O2 -march=native`, checks for non-trivial output, no short cycles, full bit span
2. **Stage 2 — Basic stats + PractRand to 256 MB** (~15s): Byte uniformity, bit frequency, serial correlation, lower-byte pair uniformity, plus PractRand up to 256 MB
3. **Stage 3 — PractRand to 64 GB + throughput** (~10 min): Full PractRand test to 64 GB (gold-standard), throughput benchmark

Only candidates that pass stage thresholds advance to the next stage, so most broken mutations are filtered cheaply.

Quality is a blend of PractRand results (60%) and basic statistical tests (40%). Throughput is normalized against a 1.5 Gop/s ceiling. The combined score weights 70% quality, 30% throughput.

MAP-Elites maintains diversity across throughput x PractRand-score dimensions, discovering generators at different points on the Pareto front.

## Prerequisites

- Python 3.10+
- OpenEvolve: `pip install openevolve`
- GCC
- numpy, scipy, matplotlib
- **PractRand** (required for quality testing)

### Installing PractRand

```bash
git clone https://github.com/tylov-fork/PractRand.git /tmp/PractRand
cd /tmp/PractRand && make -j$(nproc)
sudo cp build/Linux/bin/RNG_test /usr/local/bin/
```

## Running

```bash
openevolve-run initial_program.c evaluator.py --config config.yaml --iterations 500
```

Resume from checkpoint:

```bash
openevolve-run initial_program.c evaluator.py \
  --config config.yaml \
  --checkpoint openevolve_output/checkpoints/checkpoint_200/ \
  --iterations 300
```

## Analyzing results

```bash
python analysis.py openevolve_output/checkpoints/checkpoint_500/
```

Produces:
- **results.png** — quality vs throughput scatter with Pareto front, colored by state size
- **best_1.c, best_2.c, best_3.c** — top 3 generators as standalone C files
- Summary table with quality, throughput, state size, and structure description

## PractRand testing

Test a generator through PractRand at full scale:

```bash
./practrand_test.sh best_1.c         # default: up to 1 TB
./practrand_test.sh best_1.c 256GB   # custom limit
```

## Validating the evaluator

```bash
python validate.py
```

Runs reference generators (counter, xorshift64, SplitMix64, xoshiro256\*\*, xoshiro128\*\*, wyrand, RomuDuoJr, lehmer128, squares64, CWG64, L64X64) through the evaluator and verifies correct ranking.

## Scoring system

### Quality (70% of combined score)

**PractRand component (60% of quality):** The generator streams binary output to PractRand's `RNG_test stdin64`. Score = fraction of 29 levels passed, from 256 bytes up to 64 GB. A generator that passes all levels scores 1.0.

**Basic stats component (40% of quality):** Fast statistical tests on 8 MB of output:
- Byte-level chi-squared uniformity (8 byte positions)
- Bit frequency balance (64 bit positions)
- Lag-1 serial correlation
- Lower-byte pair uniformity (catches linear generator weaknesses)

### Throughput (30% of combined score)

Measured via `./program bench` (100M iterations with `CLOCK_MONOTONIC`). Normalized as `ops_sec / 1_500_000_000`, capped at 1.0.

### 3-stage cascade

| Stage | What | Time | Threshold |
|-------|------|------|-----------|
| 1 | Compile + sanity | ~1s | combined_score > 0.05 |
| 2 | Basic stats + PractRand 256 MB | ~15s | combined_score > 0.55 |
| 3 | PractRand 64 GB + throughput | ~10 min | Final score |

Most broken mutations are eliminated at stage 1 or 2, keeping iteration throughput high.

### Interpretation

| Quality | Meaning |
|---------|---------|
| < 0.1 | Broken (constant, short cycle, trivial patterns) |
| 0.1–0.3 | Poor (fails PractRand immediately, like plain xorshift) |
| 0.3–0.6 | Mediocre (passes some PractRand levels) |
| 0.6–0.8 | Good (passes PractRand to several GB) |
| 0.8–1.0 | Excellent (passes PractRand to 64 GB, competitive with state of the art) |

### Reference generators

| Generator | Quality | Throughput (M ops/s) | State | Combined |
|-----------|---------|---------------------|-------|----------|
| RomuDuoJr | 0.981 | 1215 | 128b | 0.869 |
| wyrand | 0.992 | 1156 | 64b | 0.868 |
| evolved best | 0.987 | 1042 | 128b | 0.847 |
| lehmer128 | 0.994 | 819 | 128b | 0.819 |
| CWG64 | 0.978 | 496 | 256b | 0.759 |
| squares64 | 0.995 | 297 | 128b | 0.741 |
| splitmix64 | 0.964 | 364 | 64b | 0.729 |
| xoshiro256** | 0.988 | 171 | 256b | 0.717 |
| L64X64 | 0.985 | 172 | 192b | 0.715 |
| xoshiro128** | 0.970 | 156 | 128b | 0.703 |

## Customization

### Increasing PractRand depth

Edit `PRACTRAND_STAGE3_MAX` and `PRACTRAND_STAGE3_TIMEOUT` in `evaluator.py`. Larger values give finer discrimination between good generators but increase evaluation time.

### Changing quality/throughput balance

Edit `QUALITY_WEIGHT` and `THROUGHPUT_WEIGHT` in `evaluator.py` (must sum to 1.0).

### Adjusting throughput baseline

Change `THROUGHPUT_CEIL` in `evaluator.py`. This is the ops/sec that maps to throughput score 1.0.

### Adding statistical tests

Add tests to `_basic_stats()` in `evaluator.py`. Each test should return a 0-1 score and be appended to the `scores` list.
