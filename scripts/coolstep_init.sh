#!/usr/bin/env bash
# coolstep_init.sh — single-command bootstrap for a fresh device.
#
# Usage:
#   scripts/coolstep_init.sh                         # full bootstrap
#   scripts/coolstep_init.sh --dry-run               # plan only, no writes
#   scripts/coolstep_init.sh --profile laptop        # override autodetect
#   scripts/coolstep_init.sh --no-armed              # skip armed-actuator drop-in
#   scripts/coolstep_init.sh --dry-run --profile server
#
# Idempotent: safe to re-run.  Every write is guarded by an existence check
# or an explicit overwrite (only the init-profile drop-in is always refreshed).
#
# Sequence:
#   1. Hardware fingerprint  → data/hardware-fingerprint.json
#   2. Dep check / venv      → .venv/
#   3. Data dirs             → data/{hnsw,chroma}/
#   4. Systemd drop-in       → ~/.config/systemd/user/coolstep-collector.service.d/05-init-profile.conf
#   5. First-run wizard      → 60s warmup + armed prompt (if store.db absent)
#   6. Health check          → GET /api/health

set -euo pipefail

# --------------------------------------------------------------------------- #
# Arg parsing
# --------------------------------------------------------------------------- #
DRY_RUN=false
PROFILE_OVERRIDE=""
NO_ARMED=false

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)        DRY_RUN=true ;;
        --profile)        shift; PROFILE_OVERRIDE="$1" ;;
        --no-armed)       NO_ARMED=true ;;
        --help|-h)
            sed -n 's/^# //p' "$0" | head -14
            exit 0 ;;
        *) printf 'unknown arg: %s\n' "$1" >&2; exit 1 ;;
    esac
    shift
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DROPIN_TEMPLATES="$REPO_ROOT/scripts/dropin-templates"
DATA_DIR="$REPO_ROOT/data"
DROPIN_DST="$HOME/.config/systemd/user/coolstep-collector.service.d"
FINGERPRINT_FILE="$DATA_DIR/hardware-fingerprint.json"

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
_run() {
    if [ "$DRY_RUN" = true ]; then
        printf '  DRY-RUN: %s\n' "$*"
    else
        printf '  + %s\n' "$*"
        "$@"
    fi
}

_info()  { printf '  %s\n' "$*"; }
_step()  { printf '\n--- %s ---\n' "$*"; }
_warn()  { printf '  WARN: %s\n' "$*" >&2; }
_fatal() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------- #
# 1. Hardware fingerprint
# --------------------------------------------------------------------------- #
_step "(1/6) hardware fingerprint"

_detect_cpu_vendor() {
    if [ -r /proc/cpuinfo ]; then
        if grep -qi "AuthenticAMD" /proc/cpuinfo; then
            printf 'amd'
            return
        elif grep -qi "GenuineIntel" /proc/cpuinfo; then
            printf 'intel'
            return
        fi
    fi
    printf 'unknown'
}

_detect_gpu() {
    # Returns: nvidia / amd / none / unknown
    if command -v lspci > /dev/null 2>&1; then
        if lspci 2>/dev/null | grep -qi "NVIDIA"; then
            printf 'nvidia'
            return
        elif lspci 2>/dev/null | grep -qi "Radeon\|AMD.*VGA\|ATI.*VGA"; then
            printf 'amd'
            return
        fi
    fi
    # Fallback: check lsmod
    if lsmod 2>/dev/null | grep -q "^nvidia "; then
        printf 'nvidia'
        return
    elif lsmod 2>/dev/null | grep -q "^amdgpu "; then
        printf 'amd'
        return
    fi
    printf 'none'
}

_detect_igpu() {
    # Returns: amd / intel / none
    if lspci 2>/dev/null | grep -qi "Integrated.*Radeon\|780M\|Vega\|Cezanne\|Phoenix"; then
        printf 'amd'
        return
    elif lspci 2>/dev/null | grep -qi "Intel.*UHD\|Intel.*Iris\|Intel.*HD Graphics"; then
        printf 'intel'
        return
    fi
    # k10temp present = AMD APU / Ryzen with iGPU
    if find /sys/class/hwmon -name name 2>/dev/null | xargs grep -ql "k10temp" 2>/dev/null; then
        printf 'amd'
        return
    fi
    printf 'none'
}

_detect_asusctl() {
    if command -v asusctl > /dev/null 2>&1; then
        printf 'true'
    else
        printf 'false'
    fi
}

_detect_fan_control() {
    # Check if any hwmon exposes fan RPM writeable channels
    if find /sys/class/hwmon -name "pwm1" 2>/dev/null | head -1 | grep -q .; then
        printf 'true'
        return
    fi
    if command -v asusctl > /dev/null 2>&1; then
        printf 'true'
        return
    fi
    printf 'false'
}

_detect_chassis() {
    # laptop / desktop / server / vm / unknown
    # BAT detection: a desktop with a USB UPS often exposes a BAT* power_supply
    # too — distinguish by checking the supply's state. A real laptop battery
    # reports Discharging / Charging / Full / Unknown. A UPS in normal AC-line
    # mode reports its own state strings (e.g. "Online") that don't show up
    # here.  Falling back to DMI chassis_type below catches the rest.
    local bat
    for bat in /sys/class/power_supply/BAT*; do
        [ -d "$bat" ] || continue
        local state=""
        if [ -r "$bat/status" ]; then
            state=$(cat "$bat/status" 2>/dev/null || true)
        fi
        case "$state" in
            Discharging|Charging|Full|Not\ charging|Unknown)
                printf 'laptop'
                return
                ;;
        esac
    done
    local chassis_type=""
    if [ -r /sys/class/dmi/id/chassis_type ]; then
        chassis_type=$(cat /sys/class/dmi/id/chassis_type 2>/dev/null || true)
    fi
    # DMI chassis_type: 1=Other, 8=Portable, 9=Laptop, 10=Notebook, 11=Sub Notebook
    # 17=Main Server, 23=Rack Mount, 24=Sealed-case PC, 25=Multi-system
    case "$chassis_type" in
        8|9|10|11) printf 'laptop'; return ;;
        17|23|24|25) printf 'server'; return ;;
        *) ;;
    esac
    # No display = headless = server
    if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
        if ! find /tmp -name ".X*-lock" 2>/dev/null | grep -q .; then
            printf 'server'
            return
        fi
    fi
    printf 'desktop'
}

CPU_VENDOR=$(_detect_cpu_vendor)
DISCRETE_GPU=$(_detect_gpu)
IGPU=$(_detect_igpu)
ASUSCTL=$(_detect_asusctl)
FAN_CONTROL=$(_detect_fan_control)
CHASSIS=$(_detect_chassis)

_info "cpu_vendor    : $CPU_VENDOR"
_info "discrete_gpu  : $DISCRETE_GPU"
_info "igpu          : $IGPU"
_info "asusctl       : $ASUSCTL"
_info "fan_control   : $FAN_CONTROL"
_info "chassis       : $CHASSIS"

# Determine profile
if [ -n "$PROFILE_OVERRIDE" ]; then
    PROFILE="$PROFILE_OVERRIDE"
    _info "profile       : $PROFILE (override)"
else
    PROFILE="$CHASSIS"
    # Normalise: only laptop/desktop/server are valid template names
    case "$PROFILE" in
        laptop|desktop|server) ;;
        vm) PROFILE="server" ;;
        *) PROFILE="desktop" ;;
    esac
    _info "profile       : $PROFILE (autodetected)"
fi

# Validate profile
case "$PROFILE" in
    laptop|desktop|server) ;;
    *) _fatal "unknown profile '$PROFILE' (valid: laptop desktop server)" ;;
esac

# Write fingerprint (skip on dry-run; data/ may not exist yet)
FINGERPRINT_JSON=$(cat <<JSONEOF
{
  "generated_by": "coolstep_init.sh",
  "cpu_vendor": "$CPU_VENDOR",
  "discrete_gpu": "$DISCRETE_GPU",
  "igpu": "$IGPU",
  "asusctl": $ASUSCTL,
  "fan_control": $FAN_CONTROL,
  "chassis": "$CHASSIS",
  "profile": "$PROFILE"
}
JSONEOF
)

if [ "$DRY_RUN" = true ]; then
    _info "(fingerprint would be written to $FINGERPRINT_FILE)"
else
    if [ -d "$DATA_DIR" ] || mkdir -p "$DATA_DIR"; then
        printf '%s\n' "$FINGERPRINT_JSON" > "$FINGERPRINT_FILE"
        _info "written: $FINGERPRINT_FILE"
    fi
fi

# --------------------------------------------------------------------------- #
# 2. Venv / dep check
# --------------------------------------------------------------------------- #
_step "(2/6) venv / deps"

# Detect whether coolstep is already installed via pipx / pip --user / AUR.
# If so, skip the dev-style venv setup — re-creating .venv next to a pipx
# install produces two parallel environments, and the unit's PATH points at
# the pipx venv, not the dev one.
INSTALLED_BIN="$(command -v coolstep 2>/dev/null || true)"
INSTALLED_PY=""
INSTALL_MODE=dev

if [ -n "$INSTALLED_BIN" ]; then
    REAL_BIN="$(readlink -f "$INSTALLED_BIN" 2>/dev/null || echo "$INSTALLED_BIN")"
    case "$REAL_BIN" in
        */pipx/venvs/coolstep/*)
            INSTALL_MODE=pipx
            INSTALLED_PY="${REAL_BIN%/bin/coolstep}/bin/python"
            ;;
        /usr/bin/*|/usr/local/bin/*)
            INSTALL_MODE=system
            INSTALLED_PY="$(command -v python3)"
            ;;
        *)
            INSTALL_MODE=pip
            INSTALLED_PY="$(command -v python3)"
            ;;
    esac
fi

VENV="$REPO_ROOT/.venv"
PIP="$VENV/bin/pip"
PYTHON="$VENV/bin/python"

_venv_has() {
    # $1 = importable module name
    "$PYTHON" -c "import $1" 2>/dev/null
}

_installed_has() {
    [ -x "$INSTALLED_PY" ] && "$INSTALLED_PY" -c "import $1" 2>/dev/null
}

if [ "$INSTALL_MODE" != "dev" ]; then
    _info "coolstep already installed ($INSTALL_MODE): $INSTALLED_BIN"
    _info "skipping dev .venv creation — using the existing install"
    if _installed_has chromadb && _installed_has numpy; then
        _info "deps OK (ML extras present in $INSTALL_MODE install)"
    else
        _warn "ML extras (chromadb + numpy) missing from $INSTALL_MODE install"
        case "$INSTALL_MODE" in
            pipx)
                _warn "  add via: pipx inject coolstep 'chromadb<1.0' 'chroma-hnswlib>=0.7.6'"
                ;;
            pip)
                _warn "  add via: pip install --user --break-system-packages 'coolstep[ml]'"
                ;;
            system)
                _warn "  install the appropriate AUR ml-extras package"
                ;;
        esac
        _warn "  daemon will run without KNN predictor (MetaPredictor fallback)"
    fi
elif [ -x "$PYTHON" ]; then
    _info "venv exists: $VENV"
    if _venv_has chromadb && _venv_has numpy; then
        _info "deps OK (chromadb + numpy present)"
    else
        _warn "venv missing ML deps — running pip install -e .[ml]"
        _run "$PIP" install --quiet -e "$REPO_ROOT[ml]"
    fi
else
    _info "no venv found — creating + installing"
    _run python3 -m venv "$VENV"
    _run "$PIP" install --quiet --upgrade pip
    _run "$PIP" install --quiet -e "$REPO_ROOT[ml]"
fi

# --------------------------------------------------------------------------- #
# 3. Data dirs
# --------------------------------------------------------------------------- #
_step "(3/6) data dirs"

for d in "$DATA_DIR" "$DATA_DIR/hnsw" "$DATA_DIR/chroma"; do
    if [ -d "$d" ]; then
        _info "exists: $d"
    else
        _run mkdir -p "$d"
    fi
done

# --------------------------------------------------------------------------- #
# 4. Systemd drop-in
# --------------------------------------------------------------------------- #
_step "(4/6) systemd drop-in"

TEMPLATE="$DROPIN_TEMPLATES/$PROFILE.conf"
DROPIN_FILE="$DROPIN_DST/05-init-profile.conf"

if [ ! -f "$TEMPLATE" ]; then
    _fatal "template not found: $TEMPLATE"
fi

_info "template : $TEMPLATE"
_info "target   : $DROPIN_FILE"
_info "profile  : $PROFILE"

_run mkdir -p "$DROPIN_DST"

if [ "$DRY_RUN" = true ]; then
    _info "(drop-in content preview:)"
    # show template with substitution applied
    grep -E "^(Slice|CPUQuota|MemoryMax)" "$TEMPLATE" | while IFS= read -r line; do
        _info "  $line"
    done
else
    # envsubst is safe here; templates use no ${VAR} tokens currently,
    # but this makes future parameterisation trivial.
    envsubst < "$TEMPLATE" > "$DROPIN_FILE"
    _info "written: $DROPIN_FILE"
fi

# Optionally materialise armed-actuator drop-in
ARMED_FILE="$DROPIN_DST/50-armed-prod.conf"
if [ "$NO_ARMED" = false ] && [ "$PROFILE" != "server" ]; then
    if [ -f "$ARMED_FILE" ]; then
        _info "armed drop-in already present: $ARMED_FILE (not overwritten)"
    else
        _info "(armed-actuator drop-in will be offered in step 5)"
    fi
else
    _info "armed actuator: skipped (--no-armed or server profile)"
fi

# --------------------------------------------------------------------------- #
# 5. First-run wizard
# --------------------------------------------------------------------------- #
_step "(5/6) first-run wizard"

STORE_DB="$DATA_DIR/store.db"

if [ "$DRY_RUN" = true ]; then
    if [ -f "$STORE_DB" ]; then
        _info "(store.db exists — wizard would be skipped)"
    else
        _info "(store.db absent — wizard would run: 60s warmup + armed prompt)"
    fi
elif [ -f "$STORE_DB" ]; then
    _info "store.db exists — first-run wizard skipped"
else
    _info "store.db absent — starting 60-second telemetry warmup"
    _info "Running: coolstep-collector --period 1.0 (60s, read-only)"

    # Start collector in background for warmup
    WARMUP_LOG="/tmp/coolstep-warmup-$$.log"
    "$VENV/bin/coolstep-collector" --period 1.0 --log-level WARNING \
        > "$WARMUP_LOG" 2>&1 &
    WARMUP_PID=$!

    # Count down
    i=60
    while [ "$i" -gt 0 ]; do
        printf '\r  warmup %2ds remaining...' "$i"
        sleep 1
        i=$((i - 1))
    done
    printf '\n'

    kill "$WARMUP_PID" 2>/dev/null || true
    wait "$WARMUP_PID" 2>/dev/null || true
    _info "warmup complete (log: $WARMUP_LOG)"

    # Armed-actuator prompt (laptop/desktop only)
    if [ "$NO_ARMED" = false ] && [ "$PROFILE" != "server" ] && [ ! -f "$ARMED_FILE" ]; then
        printf '\n  Enable armed actuator (real fan-curve writes via asusctl)? [y/N] '
        read -r ARMED_ANSWER </dev/tty || ARMED_ANSWER="N"
        case "$ARMED_ANSWER" in
            y|Y|yes|YES)
                mkdir -p "$DROPIN_DST"
                cat > "$ARMED_FILE" <<'ARMEDEOF'
# Generated by coolstep_init.sh first-run wizard
[Service]
Environment=COOLSTEP_ACTUATOR_ENABLE=true
Environment=COOLSTEP_ACTUATOR_ALLOW_PRE_CALIBRATION=1
ARMEDEOF
                _info "armed drop-in written: $ARMED_FILE"
                ;;
            *)
                _info "armed actuator skipped — run with --no-armed to suppress this prompt"
                ;;
        esac
    fi
fi

# --------------------------------------------------------------------------- #
# 6. Start + health check
# --------------------------------------------------------------------------- #
_step "(6/6) start + health"

if [ "$DRY_RUN" = true ]; then
    _info "(dry-run — would run: coolstep install-units + daemon-reload + restart coolstep-collector)"
    _info "(would check: curl http://127.0.0.1:18889/api/health)"
else
    # Ensure unit files exist before we try to restart — running this wizard
    # before `coolstep install-units` left users in the "Unit not found"
    # state.  install-units is idempotent (`skipped (exists)` on re-run).
    if [ -n "$INSTALLED_BIN" ]; then
        _run "$INSTALLED_BIN" install-units
    elif [ -x "$VENV/bin/coolstep" ]; then
        _run "$VENV/bin/coolstep" install-units
    else
        _warn "no coolstep binary found — skipping install-units; restart may fail"
    fi
    _run systemctl --user daemon-reload
    _run systemctl --user enable coolstep-collector.service 2>/dev/null || true
    _run systemctl --user restart coolstep-collector.service

    _info "waiting 4s for daemon..."
    sleep 4

    HEALTH_URL="http://127.0.0.1:18889/api/health"
    if curl -sf --max-time 5 "$HEALTH_URL" > /tmp/coolstep-init-health.json 2>/dev/null; then
        printf '\n  OK — /api/health: '
        cat /tmp/coolstep-init-health.json
        printf '\n'
        printf '\n  dashboard: http://127.0.0.1:18889/\n'
    else
        _warn "/api/health did not respond within 5s"
        _warn "check: journalctl --user -u coolstep-collector -n 30"
    fi
fi

printf '\n==> coolstep_init done (profile=%s dry-run=%s no-armed=%s)\n' \
    "$PROFILE" "$DRY_RUN" "$NO_ARMED"
