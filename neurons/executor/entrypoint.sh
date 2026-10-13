#!/bin/sh
set -eu

# The executor reserves every GPU (docker-compose.app.yml), so the NVIDIA container runtime hook
# runs when it starts. After a driver package update without a reboot that hook fails ("driver/library
# version mismatch") and a recreated executor never comes back: port 8080 stays closed and a rented
# node goes offline (12 Oct 2026, executor-v1.139). Before replacing a RUNNING executor, start a
# throwaway container from the image it already runs (local, nothing pulled) with every GPU requested
# and the entrypoint `true`, the same probe daturaai/lium-watchtower runs before it updates the runner.
# If that fails, keep the running executor and exit; the restart policy brings the runner back to try
# again; a probe that hangs past RUNNER_PROBE_TIMEOUT_SECONDS counts as failed. A first install (no executor
# running) has nothing to break and skips the probe.
running_executor=$(docker compose ps --quiet executor 2>/dev/null || true)
if [ -n "$running_executor" ]; then
    probe_image=$(docker inspect --format '{{.Image}}' "$running_executor" 2>/dev/null || true)
    if [ -n "$probe_image" ] && ! probe_error=$(timeout "${RUNNER_PROBE_TIMEOUT_SECONDS:-120}" docker run --rm --gpus all --label io.lium.executor-runner.probe=nvidia-runtime --entrypoint true "$probe_image" 2>&1); then
        case "$probe_error" in
            *"driver/library version mismatch"*)
                cause="the host's NVIDIA driver and its libraries do not match (driver updated without a reboot)"
                remedy="reboot the host or reload the NVIDIA driver once no renter is on it" ;;
            *)
                cause="the NVIDIA runtime cannot start a GPU container"
                remedy="fix the NVIDIA container runtime on the host (nvidia-smi, nvidia-container-toolkit)" ;;
        esac
        echo "Update held, running executor kept: $cause; $remedy. Retrying in ${RUNNER_PROBE_RETRY_SECONDS:-300}s. Probe error: $(echo "$probe_error" | tail -n 1)"
        sleep "${RUNNER_PROBE_RETRY_SECONDS:-300}"
        exit 1
    fi
fi

docker compose up --pull always --detach --wait --force-recreate

# Clean docker images
docker image prune -f

while true
do
    docker compose logs -f
    echo 'All containers died'
    sleep 10
done
