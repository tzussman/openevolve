/*
 * Test harness for hash function evaluation.
 *
 * The evolved hash implementation is injected via gcc's -include flag:
 *   gcc -O2 -march=native -include evolved_program.c -o hash_test test_harness.c -lm
 *
 * This file expects the following to be defined by the included program:
 *   uint64_t hash_function(const uint8_t *key, size_t len, uint64_t seed);
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <math.h>

/* hash_function is provided by the -include'd evolved program */

// Mode: avalanche — measure how well 1-bit input changes propagate
// Prints: average number of output bits that flip per input bit flip
void test_avalanche(void) {
    const int num_keys = 10000;
    const int key_len = 16;
    uint64_t seed = 0xDEADBEEF;
    uint64_t total_flips = 0;
    uint64_t total_tests = 0;

    uint64_t rng = 0x12345678ABCDEF01ULL;
    for (int k = 0; k < num_keys; k++) {
        uint8_t key[16];
        for (int i = 0; i < key_len; i++) {
            rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17;
            key[i] = (uint8_t)(rng >> 56);
        }
        uint64_t base_hash = hash_function(key, key_len, seed);

        for (int byte_idx = 0; byte_idx < key_len; byte_idx++) {
            for (int bit = 0; bit < 8; bit++) {
                key[byte_idx] ^= (1 << bit);
                uint64_t flipped_hash = hash_function(key, key_len, seed);
                key[byte_idx] ^= (1 << bit);
                uint64_t diff = base_hash ^ flipped_hash;
                total_flips += __builtin_popcountll(diff);
                total_tests++;
            }
        }
    }
    printf("%.6f\n", (double)total_flips / (double)total_tests);
}

// Mode: distribution — hash sequential integers, measure bucket uniformity
// Prints: chi-squared statistic (lower = more uniform)
void test_distribution(void) {
    const int num_keys = 1000000;
    const int num_buckets = 65536;
    uint32_t *buckets = calloc(num_buckets, sizeof(uint32_t));
    uint64_t seed = 0xDEADBEEF;

    for (int i = 0; i < num_keys; i++) {
        uint8_t key[4];
        uint32_t val = (uint32_t)i;
        memcpy(key, &val, 4);
        uint64_t h = hash_function(key, 4, seed);
        buckets[h % num_buckets]++;
    }

    double expected = (double)num_keys / (double)num_buckets;
    double chi_sq = 0.0;
    for (int i = 0; i < num_buckets; i++) {
        double diff = (double)buckets[i] - expected;
        chi_sq += (diff * diff) / expected;
    }
    free(buckets);
    printf("%.6f\n", chi_sq);
}

// Mode: collision — count collisions among hashes of sequential keys
// Prints: collision count (lower = better; 0 is ideal for small key sets)
void test_collision(void) {
    const int num_keys = 100000;
    uint64_t seed = 0xDEADBEEF;
    uint64_t *hashes = malloc(num_keys * sizeof(uint64_t));

    for (int i = 0; i < num_keys; i++) {
        uint8_t key[4];
        uint32_t val = (uint32_t)i;
        memcpy(key, &val, 4);
        hashes[i] = hash_function(key, 4, seed);
    }

    // Sort and count duplicates
    for (int i = 1; i < num_keys; i++) {
        uint64_t tmp = hashes[i];
        int j = i - 1;
        while (j >= 0 && hashes[j] > tmp) {
            hashes[j + 1] = hashes[j];
            j--;
        }
        hashes[j + 1] = tmp;
    }
    int collisions = 0;
    for (int i = 1; i < num_keys; i++) {
        if (hashes[i] == hashes[i - 1]) collisions++;
    }
    free(hashes);
    printf("%d\n", collisions);
}

// Mode: bench — throughput for various key lengths
// Prints: GB/s for each key length on a separate line
void test_bench(void) {
    uint64_t seed = 0xDEADBEEF;
    int key_lengths[] = {4, 8, 16, 32, 64, 128, 256, 1024};
    int num_lengths = sizeof(key_lengths) / sizeof(key_lengths[0]);

    for (int li = 0; li < num_lengths; li++) {
        int klen = key_lengths[li];
        uint8_t *key = malloc(klen);
        for (int i = 0; i < klen; i++) key[i] = (uint8_t)(i * 37 + 13);

        uint64_t iters = 500000000ULL / (uint64_t)(klen + 1);
        if (iters < 10000) iters = 10000;

        volatile uint64_t sink = 0;
        struct timespec t0, t1;
        clock_gettime(CLOCK_MONOTONIC, &t0);
        for (uint64_t i = 0; i < iters; i++) {
            sink ^= hash_function(key, klen, seed + i);
        }
        clock_gettime(CLOCK_MONOTONIC, &t1);
        double elapsed = (t1.tv_sec - t0.tv_sec) + (t1.tv_nsec - t0.tv_nsec) / 1e9;
        double bytes_processed = (double)iters * (double)klen;
        double gbps = bytes_processed / elapsed / 1e9;
        printf("%.4f\n", gbps);
        free(key);
    }
}

// Mode: bit-independence — test that output bit i and output bit j
// are statistically independent across many keys.
// Prints: max |correlation| across all (i,j) pairs (lower = better, 0.0 = perfect)
void test_bit_independence(void) {
    const int num_keys = 200000;
    uint64_t seed = 0xDEADBEEF;
    const int step = 4;
    const int nbits = 64 / step;
    int counts[32][32] = {{0}};
    double max_corr = 0.0;

    uint64_t rng = 0xABCD1234ULL;
    for (int k = 0; k < num_keys; k++) {
        uint8_t key[8];
        for (int i = 0; i < 8; i++) {
            rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17;
            key[i] = (uint8_t)(rng >> 56);
        }
        uint64_t h = hash_function(key, 8, seed);

        for (int i = 0; i < nbits; i++) {
            int bi = (h >> (i * step)) & 1;
            for (int j = i + 1; j < nbits; j++) {
                int bj = (h >> (j * step)) & 1;
                counts[i * 2 + bi][j * 2 + bj]++;
            }
        }
    }

    for (int i = 0; i < nbits; i++) {
        for (int j = i + 1; j < nbits; j++) {
            int n00 = counts[i * 2 + 0][j * 2 + 0];
            int n01 = counts[i * 2 + 0][j * 2 + 1];
            int n10 = counts[i * 2 + 1][j * 2 + 0];
            int n11 = counts[i * 2 + 1][j * 2 + 1];
            int total = n00 + n01 + n10 + n11;
            if (total == 0) continue;
            double p00 = (double)n00 / total;
            double p01 = (double)n01 / total;
            double p10 = (double)n10 / total;
            double p11 = (double)n11 / total;
            double pi = p10 + p11;
            double pj = p01 + p11;
            double cov = p11 - pi * pj;
            double si = pi * (1.0 - pi);
            double sj = pj * (1.0 - pj);
            if (si < 1e-10 || sj < 1e-10) continue;
            double corr = cov / sqrt(si * sj);
            if (fabs(corr) > max_corr) max_corr = fabs(corr);
        }
    }
    printf("%.6f\n", max_corr);
}

// Mode: diffusion — for small keys, measure how many output bits flip
// when a single input bit is changed in an 8-byte key.
// Prints: average bits changed (ideal: 32.0)
void test_diffusion(void) {
    const int num_trials = 50000;
    uint64_t seed = 0xDEADBEEF;
    uint64_t total_flips = 0;
    uint64_t total_tests = 0;

    for (int t = 0; t < num_trials; t++) {
        uint8_t key[8] = {0};
        uint32_t val = (uint32_t)t;
        memcpy(key, &val, 4);
        uint64_t base = hash_function(key, 8, seed);

        for (int bit = 0; bit < 64; bit++) {
            key[bit / 8] ^= (1 << (bit % 8));
            uint64_t flipped = hash_function(key, 8, seed);
            key[bit / 8] ^= (1 << (bit % 8));
            total_flips += __builtin_popcountll(base ^ flipped);
            total_tests++;
        }
    }
    printf("%.6f\n", (double)total_flips / (double)total_tests);
}

// Mode: extended distribution — test various key patterns
// Prints one chi-squared value per line for each pattern
void test_extended_distribution(void) {
    const int num_buckets = 65536;
    uint32_t *buckets = calloc(num_buckets, sizeof(uint32_t));
    uint64_t seed = 0xDEADBEEF;
    double expected;
    double chi_sq;
    int num_keys;

    // Pattern 1: Keys differing only in last byte
    num_keys = 256;
    expected = (double)num_keys / (double)num_buckets;
    memset(buckets, 0, num_buckets * sizeof(uint32_t));
    for (int i = 0; i < 256; i++) {
        uint8_t key[8] = {0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0};
        key[7] = (uint8_t)i;
        uint64_t h = hash_function(key, 8, seed);
        buckets[h % num_buckets]++;
    }
    chi_sq = 0.0;
    for (int i = 0; i < num_buckets; i++) {
        double diff = (double)buckets[i] - expected;
        chi_sq += (diff * diff) / expected;
    }
    printf("%.6f\n", chi_sq);

    // Pattern 2: Keys differing only in first byte
    memset(buckets, 0, num_buckets * sizeof(uint32_t));
    for (int i = 0; i < 256; i++) {
        uint8_t key[8] = {0, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42};
        key[0] = (uint8_t)i;
        uint64_t h = hash_function(key, 8, seed);
        buckets[h % num_buckets]++;
    }
    chi_sq = 0.0;
    for (int i = 0; i < num_buckets; i++) {
        double diff = (double)buckets[i] - expected;
        chi_sq += (diff * diff) / expected;
    }
    printf("%.6f\n", chi_sq);

    // Pattern 3: Varying length keys (1 to 256 bytes) with same content (0xAA)
    num_keys = 256;
    expected = (double)num_keys / (double)num_buckets;
    memset(buckets, 0, num_buckets * sizeof(uint32_t));
    for (int l = 1; l <= 256; l++) {
        uint8_t *key = malloc(l);
        memset(key, 0xAA, l);
        uint64_t h = hash_function(key, l, seed);
        buckets[h % num_buckets]++;
        free(key);
    }
    chi_sq = 0.0;
    for (int i = 0; i < num_buckets; i++) {
        double diff = (double)buckets[i] - expected;
        chi_sq += (diff * diff) / expected;
    }
    printf("%.6f\n", chi_sq);

    // Pattern 4: All-zeros keys of different lengths (1 to 256)
    memset(buckets, 0, num_buckets * sizeof(uint32_t));
    for (int l = 1; l <= 256; l++) {
        uint8_t *key = calloc(l, 1);
        uint64_t h = hash_function(key, l, seed);
        buckets[h % num_buckets]++;
        free(key);
    }
    chi_sq = 0.0;
    for (int i = 0; i < num_buckets; i++) {
        double diff = (double)buckets[i] - expected;
        chi_sq += (diff * diff) / expected;
    }
    printf("%.6f\n", chi_sq);

    // Pattern 5: All 65536 two-byte keys
    num_keys = 65536;
    expected = (double)num_keys / (double)num_buckets;
    memset(buckets, 0, num_buckets * sizeof(uint32_t));
    for (int i = 0; i < 65536; i++) {
        uint8_t key[2];
        key[0] = (uint8_t)(i & 0xFF);
        key[1] = (uint8_t)((i >> 8) & 0xFF);
        uint64_t h = hash_function(key, 2, seed);
        buckets[h % num_buckets]++;
    }
    chi_sq = 0.0;
    for (int i = 0; i < num_buckets; i++) {
        double diff = (double)buckets[i] - expected;
        chi_sq += (diff * diff) / expected;
    }
    printf("%.6f\n", chi_sq);

    free(buckets);
}

// Mode: seed independence — test that different seeds produce uncorrelated output
// Prints: average popcount of XOR of hash(key, seed0) ^ hash(key, seed1) (ideal: 32.0)
void test_seed_independence(void) {
    const int num_keys = 10000;
    const int num_seed_pairs = 100;
    uint64_t total_flips = 0;
    uint64_t total_tests = 0;

    uint64_t rng = 0x9876543210ABCDEFULL;
    for (int sp = 0; sp < num_seed_pairs; sp++) {
        rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17;
        uint64_t seed0 = rng;
        rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17;
        uint64_t seed1 = rng;

        for (int k = 0; k < num_keys; k++) {
            uint8_t key[8];
            uint64_t val = (uint64_t)k;
            memcpy(key, &val, 8);
            uint64_t h0 = hash_function(key, 8, seed0);
            uint64_t h1 = hash_function(key, 8, seed1);
            total_flips += __builtin_popcountll(h0 ^ h1);
            total_tests++;
        }
    }
    printf("%.6f\n", (double)total_flips / (double)total_tests);
}

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "Usage: %s <mode>\n", argv[0]);
        fprintf(stderr, "  avalanche     : avalanche quality (ideal: 32.0)\n");
        fprintf(stderr, "  distribution  : chi-squared uniformity (lower=better)\n");
        fprintf(stderr, "  collision     : collision count (ideal: 0)\n");
        fprintf(stderr, "  bench         : throughput in GB/s per key length\n");
        fprintf(stderr, "  bitindep      : bit independence (ideal: 0.0)\n");
        fprintf(stderr, "  diffusion     : single-chunk diffusion (ideal: 32.0)\n");
        fprintf(stderr, "  extdist       : extended distribution patterns\n");
        fprintf(stderr, "  seedindep     : seed independence (ideal: 32.0)\n");
        return 1;
    }

    if (strcmp(argv[1], "avalanche") == 0) test_avalanche();
    else if (strcmp(argv[1], "distribution") == 0) test_distribution();
    else if (strcmp(argv[1], "collision") == 0) test_collision();
    else if (strcmp(argv[1], "bench") == 0) test_bench();
    else if (strcmp(argv[1], "bitindep") == 0) test_bit_independence();
    else if (strcmp(argv[1], "diffusion") == 0) test_diffusion();
    else if (strcmp(argv[1], "extdist") == 0) test_extended_distribution();
    else if (strcmp(argv[1], "seedindep") == 0) test_seed_independence();
    else {
        fprintf(stderr, "Unknown mode: %s\n", argv[1]);
        return 1;
    }
    return 0;
}
