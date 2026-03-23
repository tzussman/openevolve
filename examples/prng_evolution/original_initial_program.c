#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <string.h>

// Helper intrinsics
// EVOLVE-HINT: Available operations: XOR, AND, OR, left shift, right shift,
// rotl64, rotr64, add, subtract, multiply by odd constant.
// State can be 1-4 uint64_t words. Consider output scrambling (extra
// operations applied to the return value but not stored back into state).
static inline uint64_t rotl64(uint64_t x, int k) {
    return (x << k) | (x >> (64 - k));
}

static inline uint64_t rotr64(uint64_t x, int k) {
    return (x >> k) | (x << (64 - k));
}

// --- EVOLVE-BLOCK-START ---

typedef struct {
    uint64_t s;
} rng_state_t;

void rng_seed(rng_state_t *state, uint64_t seed) {
    state->s = seed;
    if (state->s == 0) state->s = 1; // avoid zero state
}

uint64_t rng_next(rng_state_t *state) {
    uint64_t x = state->s;
    x ^= x << 13;
    x ^= x >> 7;
    x ^= x << 17;
    state->s = x;
    return x;
}

// --- EVOLVE-BLOCK-END ---

// Output modes for the evaluator
// Mode 1: binary output to stdout (for PractRand or statistical tests)
// Mode 2: benchmark throughput
// Mode 3: report state size in bytes
int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "Usage: %s <mode> [count]\n", argv[0]);
        fprintf(stderr, "  mode=binary  : write raw uint64 to stdout\n");
        fprintf(stderr, "  mode=bench   : benchmark and print ops/sec\n");
        fprintf(stderr, "  mode=statesize: print state size in bytes\n");
        return 1;
    }

    rng_state_t state;
    rng_seed(&state, 0xDEADBEEFCAFE1234ULL);

    if (strcmp(argv[1], "binary") == 0) {
        // Stream binary output; optional count (in millions), default unlimited
        uint64_t count = 0; // 0 = unlimited
        if (argc >= 3) count = (uint64_t)atoll(argv[2]) * 1000000ULL;
        uint64_t buf[512];
        uint64_t generated = 0;
        for (;;) {
            for (int i = 0; i < 512; i++) buf[i] = rng_next(&state);
            fwrite(buf, sizeof(uint64_t), 512, stdout);
            generated += 512;
            if (count > 0 && generated >= count) break;
        }
    } else if (strcmp(argv[1], "bench") == 0) {
        uint64_t n = 100000000ULL; // 100 million
        if (argc >= 3) n = (uint64_t)atoll(argv[2]);
        volatile uint64_t sink = 0;
        struct timespec t0, t1;
        clock_gettime(CLOCK_MONOTONIC, &t0);
        for (uint64_t i = 0; i < n; i++) {
            sink ^= rng_next(&state);
        }
        clock_gettime(CLOCK_MONOTONIC, &t1);
        double elapsed = (t1.tv_sec - t0.tv_sec) + (t1.tv_nsec - t0.tv_nsec) / 1e9;
        printf("%.2f\n", (double)n / elapsed); // ops per second
    } else if (strcmp(argv[1], "statesize") == 0) {
        printf("%zu\n", sizeof(rng_state_t));
    } else {
        fprintf(stderr, "Unknown mode: %s\n", argv[1]);
        return 1;
    }
    return 0;
}
