#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <math.h>

// ============================================================
// Reference hash implementations for calibration
// Usage: ./reference_hashes <hash_name> <mode>
//   hash_name: bad, fnv1a, murmur_fmix, wyhash
//   mode: avalanche, distribution, collision, bench, bitindep, diffusion
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

// --- Deliberately bad hash ---
uint64_t hash_bad(const uint8_t *key, size_t len, uint64_t seed) {
    if (len == 0) return seed;
    return seed ^ ((uint64_t)key[0] * 31);
}

// --- FNV-1a (64-bit) ---
uint64_t hash_fnv1a(const uint8_t *key, size_t len, uint64_t seed) {
    uint64_t h = 0xCBF29CE484222325ULL ^ seed;
    for (size_t i = 0; i < len; i++) {
        h ^= (uint64_t)key[i];
        h *= 0x100000001B3ULL;
    }
    return h;
}

// --- MurmurHash3 fmix64 (finalizer only, applied to simple accumulation) ---
static inline uint64_t fmix64(uint64_t h) {
    h ^= h >> 33;
    h *= 0xFF51AFD7ED558CCDULL;
    h ^= h >> 33;
    h *= 0xC4CEB9FE1A85EC53ULL;
    h ^= h >> 33;
    return h;
}

uint64_t hash_murmur_fmix(const uint8_t *key, size_t len, uint64_t seed) {
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

// --- Simplified wyhash ---
static inline uint64_t wymix(uint64_t a, uint64_t b) {
    __uint128_t r = (__uint128_t)a * b;
    return (uint64_t)(r >> 64) ^ (uint64_t)r;
}

uint64_t hash_wyhash(const uint8_t *key, size_t len, uint64_t seed) {
    const uint64_t s0 = 0xA0761D6478BD642FULL;
    const uint64_t s1 = 0xE7037ED1A0B428DBULL;
    const uint64_t s2 = 0x8EBC6AF09C88C6E3ULL;
    const uint64_t s3 = 0x589965CC75374CC3ULL;

    seed ^= s0;
    uint64_t a, b;
    const uint8_t *p = key;

    if (len <= 16) {
        if (len >= 4) {
            a = (uint64_t)(read32(p) << 0) | ((uint64_t)read32(p + ((len >> 3) << 2)) << 32);
            b = (uint64_t)(read32(p + len - 4) << 0) | ((uint64_t)read32(p + len - 4 - ((len >> 3) << 2)) << 32);
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

// ============================================================
// Unified test harness (same as initial_program.c)
// ============================================================

// Function pointer for the selected hash
typedef uint64_t (*hash_fn_t)(const uint8_t *, size_t, uint64_t);
static hash_fn_t selected_hash = NULL;

// Wrapper so test functions call selected_hash
static uint64_t hash_function(const uint8_t *key, size_t len, uint64_t seed) {
    return selected_hash(key, len, seed);
}

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

int main(int argc, char *argv[]) {
    if (argc < 3) {
        fprintf(stderr, "Usage: %s <hash_name> <mode>\n", argv[0]);
        fprintf(stderr, "  hash_name: bad, fnv1a, murmur_fmix, wyhash\n");
        fprintf(stderr, "  mode: avalanche, distribution, collision, bench, bitindep, diffusion\n");
        return 1;
    }

    if (strcmp(argv[1], "bad") == 0) selected_hash = hash_bad;
    else if (strcmp(argv[1], "fnv1a") == 0) selected_hash = hash_fnv1a;
    else if (strcmp(argv[1], "murmur_fmix") == 0) selected_hash = hash_murmur_fmix;
    else if (strcmp(argv[1], "wyhash") == 0) selected_hash = hash_wyhash;
    else {
        fprintf(stderr, "Unknown hash: %s\n", argv[1]);
        return 1;
    }

    if (strcmp(argv[2], "avalanche") == 0) test_avalanche();
    else if (strcmp(argv[2], "distribution") == 0) test_distribution();
    else if (strcmp(argv[2], "collision") == 0) test_collision();
    else if (strcmp(argv[2], "bench") == 0) test_bench();
    else if (strcmp(argv[2], "bitindep") == 0) test_bit_independence();
    else if (strcmp(argv[2], "diffusion") == 0) test_diffusion();
    else {
        fprintf(stderr, "Unknown mode: %s\n", argv[2]);
        return 1;
    }
    return 0;
}
