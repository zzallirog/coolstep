#!/usr/bin/env bash
# tests/test_coolstep_init.sh — smoke tests for scripts/coolstep_init.sh
#
# Run:
#   bash tests/test_coolstep_init.sh
#
# All tests use --dry-run so no files are written, no daemon is touched.
# Exit code 0 = all passed, non-zero = first failure.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INIT="$REPO_ROOT/scripts/coolstep_init.sh"

PASS=0
FAIL=0

_pass() { PASS=$((PASS + 1)); printf 'PASS: %s\n' "$1"; }
_fail() { FAIL=$((FAIL + 1)); printf 'FAIL: %s\n  expected: %s\n  got: %s\n' "$1" "$2" "$3"; }

_assert_contains() {
    local label="$1" needle="$2" haystack="$3"
    if printf '%s' "$haystack" | grep -qF "$needle"; then
        _pass "$label"
    else
        _fail "$label" "$needle" "(not found in output)"
    fi
}

_assert_not_contains() {
    local label="$1" needle="$2" haystack="$3"
    if printf '%s' "$haystack" | grep -qF "$needle"; then
        _fail "$label" "(absent)" "$needle"
    else
        _pass "$label"
    fi
}

# --------------------------------------------------------------------------- #
# T1: laptop profile — Slice + CPUQuota
# --------------------------------------------------------------------------- #
OUT=$(bash "$INIT" --dry-run --profile laptop 2>&1)
_assert_contains "T1.1 laptop: Slice=claw-monitor"   "Slice=claw-monitor" "$OUT"
_assert_contains "T1.2 laptop: CPUQuota=10%"          "CPUQuota=10%"       "$OUT"
_assert_contains "T1.3 laptop: MemoryMax=512M"        "MemoryMax=512M"     "$OUT"
_assert_contains "T1.4 laptop: profile printed"       "profile=laptop"     "$OUT"
_assert_contains "T1.5 laptop: dry-run flag echoed"   "dry-run=true"       "$OUT"

# --------------------------------------------------------------------------- #
# T2: desktop profile — Slice + CPUQuota
# --------------------------------------------------------------------------- #
OUT=$(bash "$INIT" --dry-run --profile desktop 2>&1)
_assert_contains "T2.1 desktop: Slice=claw"    "Slice=claw.slice"  "$OUT"
_assert_contains "T2.2 desktop: CPUQuota=30%"  "CPUQuota=30%"      "$OUT"
_assert_contains "T2.3 desktop: MemoryMax=1G"  "MemoryMax=1G"      "$OUT"
_assert_contains "T2.4 desktop: profile echo"  "profile=desktop"   "$OUT"

# --------------------------------------------------------------------------- #
# T3: server profile — no-armed implicit + MemoryMax=2G
# --------------------------------------------------------------------------- #
OUT=$(bash "$INIT" --dry-run --profile server 2>&1)
_assert_contains "T3.1 server: CPUQuota=50%"     "CPUQuota=50%"   "$OUT"
_assert_contains "T3.2 server: MemoryMax=2G"      "MemoryMax=2G"   "$OUT"
_assert_contains "T3.3 server: actuator skipped"  "server profile" "$OUT"

# --------------------------------------------------------------------------- #
# T4: --no-armed flag on laptop
# --------------------------------------------------------------------------- #
OUT=$(bash "$INIT" --dry-run --profile laptop --no-armed 2>&1)
_assert_contains "T4.1 no-armed: flag shown"    "no-armed=true" "$OUT"
_assert_contains "T4.2 no-armed: actuator skip" "armed actuator: skipped" "$OUT"

# --------------------------------------------------------------------------- #
# T5: invalid profile exits non-zero
# --------------------------------------------------------------------------- #
if bash "$INIT" --dry-run --profile invalid_xyz 2>/dev/null; then
    _fail "T5.1 invalid profile must exit non-zero" "exit 1" "exit 0"
else
    _pass "T5.1 invalid profile exits non-zero"
fi

# --------------------------------------------------------------------------- #
# T6: dry-run produces no files
# --------------------------------------------------------------------------- #
TMP_CHECK=$(mktemp)
bash "$INIT" --dry-run --profile laptop > "$TMP_CHECK" 2>&1
# The data/hardware-fingerprint.json is NOT written in dry-run mode,
# but we can only assert it wasn't in a tmpdir setup.  We just verify
# the script printed the "(fingerprint would be written to ...)" line.
_assert_contains "T6.1 dry-run: fingerprint deferred" "fingerprint would be written" "$(cat "$TMP_CHECK")"
rm -f "$TMP_CHECK"

# --------------------------------------------------------------------------- #
# T7: unknown flag exits non-zero
# --------------------------------------------------------------------------- #
if bash "$INIT" --unknown-flag 2>/dev/null; then
    _fail "T7.1 unknown flag must exit non-zero" "exit 1" "exit 0"
else
    _pass "T7.1 unknown flag exits non-zero"
fi

# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
