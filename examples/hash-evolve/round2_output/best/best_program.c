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

// 128-bit multiply-xor-fold (MUM)
static inline uint64_t mum(uint64_t a, uint64_t b) {
    __uint128_t r = (__uint128_t)a * b;
    return (uint64_t)(r >> 64) ^ (uint64_t)r;
}

// Finalizer function (improved avalanche with multiple shifts and multiplies)
static inline uint64_t finalizer(uint64_t h) {
    h ^= h >> 33;
    h *= 0xFF51AFD7ED558CCDULL; // XXH3/wyhash C1
    h ^= h >> 33;
    h *= 0xC4CEB9FE1A85EC53ULL; // XXH3/wyhash C2
    h ^= h >> 33;
    return h;
}

// Constants for mixing
static const uint64_t K1 = 0x9E3779B97F4A7C15ULL; // Golden ratio
static const uint64_t K2 = 0xBF58476D1CE4E5B9ULL; // Another mixing constant
static const uint64_t K3 = 0x165667B19E3779F9ULL; // Yet another mixing constant

uint64_t hash_function(const uint8_t *key, size_t len, uint64_t seed) {
    // Initial state: mix seed, length, and a constant
    uint64_t h = seed ^ len ^ K1;

    // Handle zero-length key immediately
    if (len == 0) {
        return finalizer(h);
    }

    // Process 16-byte chunks for long keys (len >= 16)
    if (len >= 16) {
        uint64_t acc0 = h; // First accumulator, initialized with h
        uint64_t acc1 = seed ^ K2; // Second accumulator, initialized with seed and another constant

        size_t i = 0;
        for (; i + 16 <= len; i += 16) {
            uint64_t k1 = read64(key + i);
            uint64_t k2 = read64(key + i + 8);
            acc0 = mum(acc0 ^ k1, K1); // Mix acc0 with k1 and K1
            acc1 = mum(acc1 ^ k2, K3); // Mix acc1 with k2 and K3 (different constant)
        }
        h = mum(acc0, acc1) ^ h; // Fold accumulators into h
        key += i; // Advance key pointer to remaining bytes
        len -= i; // Update remaining length
    }

    // Handle remaining bytes (0-15 bytes) or keys initially < 16 bytes
    if (len > 0) {
        if (len <= 8) {
            uint64_t k_rem = 0;
            // Safe and efficient way to read 1-8 bytes into a uint64_t
            // using fallthrough to pack bytes from key[0] up to key[len-1]
            switch (len) {
                case 8: k_rem = read64(key); break;
                case 7: k_rem |= (uint64_t)key[6] << 48; // fallthrough
                case 6: k_rem |= (uint64_t)key[5] << 40; // fallthrough
                case 5: k_rem |= (uint64_t)key[4] << 32; // fallthrough
                case 4: k_rem |= read32(key); break; // Read first 4 bytes
                case 3: k_rem |= (uint64_t)key[2] << 16; // fallthrough
                case 2: k_rem |= (uint64_t)key[1] << 8;  // fallthrough
                case 1: k_rem |= (uint64_t)key[0]; break;
            }
            h = mum(h ^ k_rem, K2); // Mix remaining bytes into h
        } else { // len is 9 to 15 bytes
            uint64_t k1 = read64(key);
            uint64_t k2 = read64(key + len - 8); // Overlapping read for the last 8 bytes
            h = mum(h ^ k1, k2 ^ K3); // Mix with both parts of the remaining key
        }
    }

    // Final mixing step
    return finalizer(h);
}

// --- EVOLVE-BLOCK-END ---
