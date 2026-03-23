#!/bin/bash
# Usage: ./practrand_test.sh <source.c> [max_bytes]
# Example: ./practrand_test.sh best_1.c 1TB
set -e
SOURCE="${1:?Usage: $0 <source.c> [max_bytes]}"
MAX="${2:-1TB}"
BINARY=$(mktemp)
trap 'rm -f "$BINARY"' EXIT
echo "Compiling $SOURCE ..."
gcc -O2 -march=native -o "$BINARY" "$SOURCE" -lm
echo "Running PractRand up to $MAX ..."
"$BINARY" binary | RNG_test stdin64 -tlmax "$MAX"
