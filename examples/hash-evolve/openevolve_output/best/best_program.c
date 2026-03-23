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

// MUM (Multiply-Universal-Hashing) mixer - the core of modern fast hashes.
static inline uint64_t mum(uint64_t a, uint64_t b) {
    __uint128_t r = (__uint128_t)a * b;
    return (uint64_t)(r >> 64) ^ (uint64_t)r;
}

uint64_t hash_function(const uint8_t *key, size_t len, uint64_t seed) {
    const uint64_t P1 = 0xBF58476D1CE4E5B9ULL;
    const uint64_t P2 = 0x94D049BB133111EBULL;

    if (len <= 16) {
        if (len == 0) return mum(seed, P1);
        if (len > 8) { // 9-16 bytes
            uint64_t k1 = read64(key);
            uint64_t k2 = read64(key + len - 8);
            return mum(k1 ^ seed, k2 ^ len ^ P1);
        }
        // 1-8 bytes
        uint64_t k1;
        if (len >= 4) { // 4-8 bytes
            k1 = (uint64_t)read32(key);
            k1 |= (uint64_t)read32(key + len - 4) << 32;
        } else { // 1-3 bytes
            k1 = key[0];
            k1 |= (uint64_t)key[len >> 1] << 8;
            k1 |= (uint64_t)key[len - 1] << 16;
        }
        return mum(k1 ^ seed, len ^ P1);
    }

    // Bulk processing (> 16 bytes)
    const uint8_t *p = key;
    const uint8_t *end = p + len;
    uint64_t h = mum(seed, len ^ P1);

    while (p + 16 <= end) {
        uint64_t k1 = read64(p);
        uint64_t k2 = read64(p + 8);
        h = mum(h + k1, P2 + k2);
        p += 16;
    }

    // Handle tail: process the last 16 bytes, which may overlap with the loop.
    // This is a branchless approach that covers all remaining bytes.
    if ((len & 15) != 0) { // Only if there's a tail
        uint64_t k1 = read64(end - 16);
        uint64_t k2 = read64(end - 8);
        h = mum(h ^ k1, P2 ^ k2);
    }

    return mum(h, P1);
}

// --- EVOLVE-BLOCK-END ---
