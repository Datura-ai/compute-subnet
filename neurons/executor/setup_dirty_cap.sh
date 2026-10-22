#!/bin/bash
# Dirty page cache cap (DAH-4978).
#
# Tearing down a container's overlay rootfs syncs the whole docker-root filesystem
# and waits for every dirty page on it, whoever wrote them. With the default
# vm.dirty_ratio=20 a 4 TB host may hold ~800 GB dirty; on a 125 MB/s disk dockerd
# then sits in umount for minutes, `docker rm` of a filler misses the validator's
# 60 s limit and the customer's create fails; the same wait ends ~1.6 % of customer
# pod removals ("did not receive an exit event"). A fixed byte cap keeps the flush
# at seconds: the kernel starts writing at 256 MB and throttles writers at 1 GB, so
# the cache never builds up. vm.* sysctls are host-global and this container is
# privileged, so writing /proc/sys/vm here sets them for the host.
#
# setup ramps instead of jumping: a cap below the host's current Dirty would block
# every writer on the host, customers included, until the backlog drains (at
# 125 MB/s a 500 GB backlog is over an hour). The cap starts at
# max(target, 1.1 x Dirty); the janitor lowers it toward the target as Dirty falls,
# then re-asserts it every 15 min (a provider's config management writing
# vm.dirty_ratio zeroes dirty_bytes). A provider's own lower cap is left alone.
#
# Everything is fail-open: a failure logs a warning and the executor runs with the
# host's values.
#
# Usage: setup_dirty_cap.sh [setup|janitor]

SYS_ROOT=${DIRTY_CAP_SYS_ROOT:-}          # test-only prefix for /proc
RAMP_SECONDS=${DIRTY_CAP_RAMP_SECONDS:-60}
HOLD_SECONDS=${DIRTY_CAP_HOLD_SECONDS:-900}
TARGET_BYTES=$((1024 * 1024 * 1024))
MEMINFO="$SYS_ROOT/proc/meminfo"
VM="$SYS_ROOT/proc/sys/vm"

log() { echo "[dirty-cap] $*"; }

dirty_bytes_now() {
    awk '/^Dirty:/ { print $2 * 1024; found = 1 } END { exit !found }' "$MEMINFO" 2>/dev/null
}

cap_now() { cat "$VM/dirty_bytes" 2>/dev/null; }

apply_cap() {
    echo "$1" > "$VM/dirty_bytes" && echo "$(($1 / 4))" > "$VM/dirty_background_bytes"
}

cap_is_settled() {
    # 0 = dirty_ratio mode (the default, or a provider reset); anything up to the target is fine
    local cap="$1"
    [ -n "$cap" ] && [ "$cap" -gt 0 ] && [ "$cap" -le "$TARGET_BYTES" ]
}

step_toward_target() {
    # lower the cap one notch: 0 once it is at (or a provider put it under) the target, 2 while
    # the ramp is still above it, 1 when /proc cannot be read or written
    local dirty cap want
    dirty=$(dirty_bytes_now) || { log "cannot read Dirty from $MEMINFO"; return 1; }
    cap=$(cap_now) || { log "cannot read $VM/dirty_bytes"; return 1; }
    cap_is_settled "$cap" && return 0
    want=$((dirty + dirty / 10))
    [ "$want" -gt "$TARGET_BYTES" ] || want=$TARGET_BYTES
    if [ "$cap" -eq 0 ] || [ "$want" -lt "$cap" ]; then
        apply_cap "$want" || { log "write to $VM failed"; return 1; }
        log "dirty_bytes=$want dirty_background_bytes=$((want / 4)) (Dirty $dirty, target $TARGET_BYTES)"
    fi
    cap_is_settled "$want" || return 2
}

janitor_loop() {
    while :; do
        if step_toward_target; then sleep "$HOLD_SECONDS"; else sleep "$RAMP_SECONDS"; fi
    done
}

case "${1:-setup}" in
    setup)
        step_toward_target
        case $? in
            0) ;;
            2) log "cap not at target yet; the janitor keeps lowering it" ;;
            *) exit 1 ;;
        esac ;;
    janitor) janitor_loop ;;
    *) log "unknown command: $1"; exit 1 ;;
esac
