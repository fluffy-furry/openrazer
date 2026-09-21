#!/bin/bash

set -euo pipefail

if [ "${1:-}" != "--session" ]; then
    exec dbus-run-session -- bash "$0" --session
fi

stage=$(mktemp -d)
fake_pid=
daemon_pid=

cleanup() {
    result=$?
    trap - EXIT
    if [ "$result" -ne 0 ]; then
        cat "$stage"/*.log "$stage"/logs/* 2>/dev/null || true
    fi
    for pid in "$daemon_pid" "$fake_pid"; do
        if [ -n "$pid" ]; then
            kill "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
        fi
    done
    chmod -R u+w "$stage"
    rm -rf "$stage"
    exit "$result"
}
trap cleanup EXIT

export PYTHONPATH="$PWD/pylib:$PWD/daemon"
export OPENRAZER_TEST_DIR="$stage/devices"
mkdir -p "$stage/devices" "$stage/run" "$stage/logs" "$stage/config"
export XDG_CONFIG_HOME="$stage/config"
python3 scripts/create_fake_device.py --dest "$stage/devices" --all --non-interactive > "$stage/fake.log" 2>&1 &
fake_pid=$!

expected=$(python3 -c 'from openrazer._fake_driver import SPECS; print(len(SPECS))')
for attempt in {1..100}; do
    actual=$(find "$stage/devices" -name device_type | wc -l)
    [ "$actual" -eq "$expected" ] && break
    kill -0 "$fake_pid"
    sleep 0.1
done
[ "$actual" -eq "$expected" ]

python3 daemon/run_openrazer_daemon.py --foreground --as-root \
    --test-dir "$stage/devices" --run-dir "$stage/run" --log-dir "$stage/logs" \
    --config "$PWD/daemon/resources/razer.conf" > "$stage/daemon.log" 2>&1 &
daemon_pid=$!

for attempt in {1..100}; do
    actual=$(python3 -c 'from openrazer.client import DeviceManager; print(len(DeviceManager().devices))' 2>/dev/null || true)
    [ "$actual" = "$expected" ] && break
    kill -0 "$daemon_pid"
    sleep 0.1
done
[ "$actual" = "$expected" ]

./scripts/ci/test-daemon.sh
