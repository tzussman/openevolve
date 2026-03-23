#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <math.h>

// Helper intrinsics
// EVOLVE-HINT: Available operations: XOR, AND, OR, left shift, right shift,
// rotl64, rotr64, add, subtract, multiply by constant.
// Consider: reading multiple bytes at once (8 bytes as uint64_t via memcpy),
// using different strategies for short keys (<8, <16, <32 bytes) vs bulk,
// folding techniques for combining partial hash states,
// finalization/avalanche mixing (multiply-xorshift patterns).
// The hash should handle: arbitrary key lengths, alignment-safe reads,
// zero-length keys, and keys that differ in only one bit or one byte.

static inline uint64_t rotl64(uint64_t x, int k) {
    return (x << k) | (x >> (64 - k));
}

static inline uint64_t rotr64(uint64_t x, int k) {
    return (x >> k) | (x << (64 - k));
}

// Safe unaligned read
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

// --- EVOLVE-BLOCK-START ---

uint64_t hash_function(const uint8_t *key, size_t len, uint64_t seed) {
    uint64_t h = seed ^ len;

    for (size_t i = 0; i < len; i++) {
        h ^= (uint64_t)key[i];
        h = rotl64(h, 7) * 0x2127599BF4325C37ULL;
    }

    h ^= h >> 32;
    h *= 0xD6E8FEB86659FD93ULL;
    h ^= h >> 32;
    return h;
}

// --- EVOLVE-BLOCK-END ---
