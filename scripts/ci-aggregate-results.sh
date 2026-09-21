#!/usr/bin/env bash
# Only success is accepted. skipped/failure/cancelled fail closed so a
# skipped needed job cannot turn the required `ci` check green.
set -euo pipefail

if [[ $# -eq 0 ]]; then
  echo "usage: ci-aggregate-results.sh <result>..." >&2
  exit 2
fi

for result in "$@"; do
  case "$result" in
    success) ;;
    *)
      echo "::error::aggregate ci: needed job result '${result}' is not success" >&2
      exit 1
      ;;
  esac
done
