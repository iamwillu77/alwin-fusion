#!/usr/bin/env bash
# Run the Alwin Fusion automated test suite.
#
# Usage:
#   scripts/run_tests.sh              # all tests, verbose
#   scripts/run_tests.sh -q           # quiet (any unittest args are passed through)
#   scripts/run_tests.sh -k permission   # run tests matching a substring
#
# Tests run entirely inside temporary directories and never modify
# your real home directory.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "Alwin Fusion test suite"
echo "python: $(python3 --version)"
echo

exec python3 -m unittest discover -s tests -v "$@"
