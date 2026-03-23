/*
 * SMHasher bridge for evolved hash functions.
 *
 * This file wraps an evolved hash function for use with SMHasher's
 * test framework. It implements a standalone test binary that can
 * run a subset of SMHasher-style tests.
 *
 * Build instructions:
 *   1. Standalone (built-in tests only):
 *      gcc -O2 -march=native -o smhasher_bridge smhasher_bridge.c -lm
 *      ./smhasher_bridge --test all
 *
 *   2. With SMHasher (full test suite):
 *      - Clone SMHasher: git clone https://github.com/aappleby/smhasher
 *      - Add this file to the SMHasher build
 *      - Link against the SMHasher library
 *      - See SMHasher docs for details on adding custom hashes
 *
 * Usage:
 *   ./smhasher_bridge --test <test_list>
 *   ./smhasher_bridge --verify
 *   ./smhasher_bridge --info
 *
 * Test list (comma-separated):
 *   all, Verification, Avalanche, Distribution, BIC, Sparse, Permutation
 */

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

// ============================================================
// Hash function (paste evolved hash here or #include it)
// ============================================================

static inline uint64_t rotl64(uint64_t x, int k) {
    return (x << k) | (x >> (64 - k));
}

static inline uint64_t rotr64(uint64_t x, int k) {
    return (x >> k) | (x << (64 - k));
}

static inline uint64_t read64(const uint8_t *p) {
    uint64_t v;
    memcpy(&v, p, 8);
    return v;
}

static inline uint32_t read32(const uint8_t *p) {
    uint32_t v;
    memcpy(&v, p, 4);
    return v;
}

// --- Include the evolved hash function here ---
// Either paste the EVOLVE-BLOCK content or use:
// #include "best_1_evolve_block.h"

static inline uint64_t hash_finalize(uint64_t h) {
    h ^= h >> 33;
    h *= 0xFF51AFD7ED558CCDULL;
    h ^= h >> 33;
    h *= 0xC4CEB9FE1A85EC53ULL;
    h ^= h >> 33;
    return h;
}

uint64_t hash_function(const uint8_t *key, size_t len, uint64_t seed) {
    uint64_t h = seed ^ (len * 0x9E3779B97F4A7C15ULL);

    const uint8_t *end = key + len;
    const uint8_t *p = key;

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

    return hash_finalize(h);
}

// ============================================================
// SMHasher-compatible interface
// ============================================================

// SMHasher expects this signature: void hash(const void *key, int len, uint32_t seed, void *out)
void smhasher_hash(const void *key, int len, uint32_t seed, void *out) {
    uint64_t h = hash_function((const uint8_t *)key, (size_t)len, (uint64_t)seed);
    memcpy(out, &h, 8);
}

// ============================================================
// Built-in verification and tests
// ============================================================

// Verification value: hash of all byte sequences of length 0..255
uint32_t compute_verification(void) {
    uint8_t key[256];
    uint32_t verification = 0;

    for (int i = 0; i < 256; i++) {
        for (int j = 0; j < i; j++) {
            key[j] = (uint8_t)j;
        }
        uint64_t h = hash_function(key, i, (uint64_t)(256 - i));
        // Fold 64-bit hash into verification accumulator
        verification ^= (uint32_t)(h & 0xFFFFFFFF);
        verification ^= (uint32_t)(h >> 32);
    }
    return verification;
}

// Simple PRNG for test key generation
static uint64_t test_rng_state = 0;

static uint64_t test_rng(void) {
    test_rng_state ^= test_rng_state << 13;
    test_rng_state ^= test_rng_state >> 7;
    test_rng_state ^= test_rng_state << 17;
    return test_rng_state;
}

static void test_rng_seed(uint64_t s) {
    test_rng_state = s ? s : 1;
}

// --- Avalanche test ---
int test_avalanche(void) {
    printf("--- Avalanche Test ---\n");
    const int num_keys = 50000;
    const int key_len = 16;
    int pass = 1;

    test_rng_seed(0x12345678ABCDEF01ULL);

    // Track per-bit avalanche statistics
    double bit_flip_avg[64] = {0};
    uint64_t total_tests = 0;

    for (int k = 0; k < num_keys; k++) {
        uint8_t key[16];
        for (int i = 0; i < key_len; i++) {
            key[i] = (uint8_t)(test_rng() >> 56);
        }
        uint64_t base = hash_function(key, key_len, 0);

        for (int byte_idx = 0; byte_idx < key_len; byte_idx++) {
            for (int bit = 0; bit < 8; bit++) {
                key[byte_idx] ^= (1 << bit);
                uint64_t flipped = hash_function(key, key_len, 0);
                key[byte_idx] ^= (1 << bit);

                uint64_t diff = base ^ flipped;
                for (int ob = 0; ob < 64; ob++) {
                    bit_flip_avg[ob] += (diff >> ob) & 1;
                }
                total_tests++;
            }
        }
    }

    // Check each output bit flips ~50% of the time
    double worst_bias = 0.0;
    int worst_bit = 0;
    for (int ob = 0; ob < 64; ob++) {
        double ratio = bit_flip_avg[ob] / (double)total_tests;
        double bias = fabs(ratio - 0.5);
        if (bias > worst_bias) {
            worst_bias = bias;
            worst_bit = ob;
        }
    }

    printf("  Worst bit bias: bit %d = %.4f (ideal: 0.0)\n", worst_bit, worst_bias);
    if (worst_bias > 0.05) {
        printf("  FAIL: bias > 5%%\n");
        pass = 0;
    } else if (worst_bias > 0.01) {
        printf("  WARN: bias > 1%%\n");
    } else {
        printf("  PASS\n");
    }
    return pass;
}

// --- Distribution test ---
int test_distribution(void) {
    printf("--- Distribution Test ---\n");
    const int num_keys = 1000000;
    const int num_buckets = 65536;
    uint32_t *buckets = calloc(num_buckets, sizeof(uint32_t));
    int pass = 1;

    for (int i = 0; i < num_keys; i++) {
        uint8_t key[4];
        uint32_t val = (uint32_t)i;
        memcpy(key, &val, 4);
        uint64_t h = hash_function(key, 4, 0xDEADBEEF);
        buckets[h % num_buckets]++;
    }

    double expected = (double)num_keys / (double)num_buckets;
    double chi_sq = 0.0;
    for (int i = 0; i < num_buckets; i++) {
        double diff = (double)buckets[i] - expected;
        chi_sq += (diff * diff) / expected;
    }
    free(buckets);

    printf("  Chi-squared: %.2f (expected ~%.0f)\n", chi_sq, (double)(num_buckets - 1));
    if (chi_sq > 2.0 * (num_buckets - 1)) {
        printf("  FAIL\n");
        pass = 0;
    } else {
        printf("  PASS\n");
    }
    return pass;
}

// --- Bit Independence Criterion (BIC) test ---
int test_bic(void) {
    printf("--- Bit Independence Criterion ---\n");
    const int num_keys = 200000;
    int pass = 1;

    // Test correlation between all pairs of output bits
    // (sampling every 4th bit for speed)
    const int step = 4;
    const int nbits = 64 / step;
    double max_corr = 0.0;

    // Accumulate sums for Pearson correlation
    long long count_both1[16][16] = {{0}};
    long long count_i1[16] = {0};
    long long count_j1[16] = {0};

    test_rng_seed(0xABCD1234ULL);
    for (int k = 0; k < num_keys; k++) {
        uint8_t key[8];
        for (int i = 0; i < 8; i++) {
            key[i] = (uint8_t)(test_rng() >> 56);
        }
        uint64_t h = hash_function(key, 8, 0xDEADBEEF);

        for (int i = 0; i < nbits; i++) {
            int bi = (h >> (i * step)) & 1;
            if (bi) count_i1[i]++;
            for (int j = i + 1; j < nbits; j++) {
                int bj = (h >> (j * step)) & 1;
                if (bi && bj) count_both1[i][j]++;
            }
        }
        for (int j = 0; j < nbits; j++) {
            if ((h >> (j * step)) & 1) count_j1[j]++;
        }
    }

    for (int i = 0; i < nbits; i++) {
        for (int j = i + 1; j < nbits; j++) {
            double pi = (double)count_i1[i] / num_keys;
            double pj = (double)count_j1[j] / num_keys;
            double pij = (double)count_both1[i][j] / num_keys;
            double cov = pij - pi * pj;
            double si = pi * (1.0 - pi);
            double sj = pj * (1.0 - pj);
            if (si < 1e-10 || sj < 1e-10) continue;
            double corr = fabs(cov / sqrt(si * sj));
            if (corr > max_corr) max_corr = corr;
        }
    }

    printf("  Max correlation: %.6f (ideal: 0.0)\n", max_corr);
    if (max_corr > 0.05) {
        printf("  FAIL\n");
        pass = 0;
    } else if (max_corr > 0.01) {
        printf("  WARN\n");
    } else {
        printf("  PASS\n");
    }
    return pass;
}

// --- Sparse key test ---
int test_sparse(void) {
    printf("--- Sparse Key Test ---\n");
    // Test keys that differ in only a few bits
    const int num_positions = 64;
    const int key_len = 8;
    int pass = 1;
    int collisions = 0;

    uint64_t *hashes = malloc(num_positions * sizeof(uint64_t));
    uint8_t key[8] = {0};

    for (int bit = 0; bit < num_positions; bit++) {
        memset(key, 0, key_len);
        key[bit / 8] = (1 << (bit % 8));
        hashes[bit] = hash_function(key, key_len, 0);
    }

    // Check for collisions
    for (int i = 0; i < num_positions; i++) {
        for (int j = i + 1; j < num_positions; j++) {
            if (hashes[i] == hashes[j]) {
                collisions++;
            }
        }
    }

    // Also check that popcount of differences is high
    double avg_diff_bits = 0;
    int diff_count = 0;
    for (int i = 0; i < num_positions; i++) {
        for (int j = i + 1; j < num_positions; j++) {
            avg_diff_bits += __builtin_popcountll(hashes[i] ^ hashes[j]);
            diff_count++;
        }
    }
    avg_diff_bits /= diff_count;

    free(hashes);

    printf("  Collisions: %d\n", collisions);
    printf("  Avg diff bits: %.2f (ideal: 32.0)\n", avg_diff_bits);
    if (collisions > 0 || avg_diff_bits < 28.0) {
        printf("  FAIL\n");
        pass = 0;
    } else {
        printf("  PASS\n");
    }
    return pass;
}

// --- Permutation test ---
int test_permutation(void) {
    printf("--- Permutation Test ---\n");
    // Verify that permuting key bytes changes the hash
    const int key_len = 8;
    int pass = 1;
    int failures = 0;
    int total = 0;

    test_rng_seed(0x5678ABCDULL);
    for (int trial = 0; trial < 10000; trial++) {
        uint8_t key[8];
        for (int i = 0; i < key_len; i++) {
            key[i] = (uint8_t)(test_rng() >> 56);
        }
        uint64_t h1 = hash_function(key, key_len, 0);

        // Swap two random bytes
        int a = test_rng() % key_len;
        int b = test_rng() % key_len;
        if (a == b) continue;
        if (key[a] == key[b]) continue; // swap of identical bytes doesn't count

        uint8_t tmp = key[a];
        key[a] = key[b];
        key[b] = tmp;
        uint64_t h2 = hash_function(key, key_len, 0);

        total++;
        if (h1 == h2) failures++;
    }

    double failure_rate = total > 0 ? (double)failures / total : 0;
    printf("  Permutation collisions: %d / %d (%.4f%%)\n", failures, total,
           failure_rate * 100);
    if (failure_rate > 0.001) {
        printf("  FAIL\n");
        pass = 0;
    } else {
        printf("  PASS\n");
    }
    return pass;
}

// ============================================================
// Main
// ============================================================

void print_info(void) {
    printf("Hash function: evolved_hash (64-bit)\n");
    printf("Output size: 64 bits\n");
    printf("Seed support: yes (64-bit seed)\n");
    printf("Verification value: 0x%08X\n", compute_verification());
}

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "Usage: %s [--test <tests>] [--verify] [--info]\n", argv[0]);
        fprintf(stderr, "  --test all|Avalanche,Distribution,BIC,Sparse,Permutation\n");
        fprintf(stderr, "  --verify   Compute verification value\n");
        fprintf(stderr, "  --info     Print hash function info\n");
        return 1;
    }

    if (strcmp(argv[1], "--info") == 0) {
        print_info();
        return 0;
    }

    if (strcmp(argv[1], "--verify") == 0) {
        printf("Verification: 0x%08X\n", compute_verification());
        return 0;
    }

    if (strcmp(argv[1], "--test") == 0) {
        if (argc < 3) {
            fprintf(stderr, "Specify test names or 'all'\n");
            return 1;
        }

        int total = 0, passed = 0;
        char *tests = strdup(argv[2]);
        int run_all = (strcmp(tests, "all") == 0);

        char *tok = strtok(tests, ",");
        while (tok || run_all) {
            int result = 1;

            if (run_all || strcmp(tok, "Avalanche") == 0) {
                result = test_avalanche();
                total++;
                passed += result;
                if (run_all && !tok) break;
            }
            if (run_all || (tok && strcmp(tok, "Distribution") == 0)) {
                result = test_distribution();
                total++;
                passed += result;
            }
            if (run_all || (tok && strcmp(tok, "BIC") == 0)) {
                result = test_bic();
                total++;
                passed += result;
            }
            if (run_all || (tok && strcmp(tok, "Sparse") == 0)) {
                result = test_sparse();
                total++;
                passed += result;
            }
            if (run_all || (tok && strcmp(tok, "Permutation") == 0)) {
                result = test_permutation();
                total++;
                passed += result;
            }

            if (run_all) break;
            tok = strtok(NULL, ",");
        }

        free(tests);
        printf("\n=== Results: %d/%d tests passed ===\n", passed, total);
        return (passed == total) ? 0 : 1;
    }

    fprintf(stderr, "Unknown option: %s\n", argv[1]);
    return 1;
}
