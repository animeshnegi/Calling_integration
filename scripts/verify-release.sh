#!/usr/bin/env bash
# Compatibility wrapper: the real checks are Python and CRLF-safe. #
# Trailing comments absorb a Windows carriage return on every line. #
exec python3 "$(dirname "$0")/finish-deployment.py" --verify-only "$@" #
