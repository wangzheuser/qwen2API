#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTION="${ACTION:-deploy}"
RELEASE_STAGE="${RELEASE_STAGE:-A}"
REVISION="$(git -C "${ROOT_DIR}" rev-parse --short HEAD)"
IMAGE_TAG="${1:-dev-go-${REVISION}}"
SSH_TARGET="${US_SSH_TARGET:-us}"
SSH_PORT="${US_SSH_PORT:-}"
REMOTE_DIR="${US_DEPLOY_DIR:-/opt/docker_projects/qwen2api}"
PUBLIC_HEALTH_URL="${US_PUBLIC_HEALTH_URL:-https://qwen2api.codeai.de5.net/healthz}"
POST_SWITCH_OBSERVE_SECONDS="${POST_SWITCH_OBSERVE_SECONDS:-3600}"
POST_SWITCH_STABILIZE_TIMEOUT_SECONDS="${POST_SWITCH_STABILIZE_TIMEOUT_SECONDS:-60}"
OBSERVATION_MAX_AVERAGE_MS="${OBSERVATION_MAX_AVERAGE_MS:-3976}"
MEMORY_RECOVERY_TIMEOUT_SECONDS="${MEMORY_RECOVERY_TIMEOUT_SECONDS:-300}"
POST_SMOKE_COOLDOWN_SECONDS="${POST_SMOKE_COOLDOWN_SECONDS:-0}"
SMOKE_CHAT_SYNTHETIC_COUNT="${SMOKE_CHAT_SYNTHETIC_COUNT:-20}"
CANDIDATE_READY_TIMEOUT_SECONDS="${CANDIDATE_READY_TIMEOUT_SECONDS:-720}"
REUSE_REMOTE_IMAGE="${REUSE_REMOTE_IMAGE:-false}"
LOCAL_BINARY_SHA="-"
SSH_ARGS=()
SCP_ARGS=()
SSH_COMMAND=( ssh )
SCP_COMMAND=( scp )

if [[ "${CONFIRM_PRODUCTION_DEPLOY:-}" != "yes" ]]; then
  printf 'Set CONFIRM_PRODUCTION_DEPLOY=yes to run ACTION=%s for qwen2api on %s.\n' "${ACTION}" "${SSH_TARGET}" >&2
  exit 2
fi
case "${ACTION}" in
  deploy|rollback) ;;
  *) printf 'Invalid ACTION: %s\n' "${ACTION}" >&2; exit 2 ;;
esac
case "${RELEASE_STAGE}" in
  A|B) ;;
  *) printf 'Invalid RELEASE_STAGE: %s\n' "${RELEASE_STAGE}" >&2; exit 2 ;;
esac
[[ "${IMAGE_TAG}" =~ ^[A-Za-z0-9_.-]+$ ]] || { printf 'Invalid image tag: %s\n' "${IMAGE_TAG}" >&2; exit 2; }
[[ "${CANDIDATE_READY_TIMEOUT_SECONDS}" =~ ^[0-9]+$ && "${CANDIDATE_READY_TIMEOUT_SECONDS}" -ge 1 ]] || {
  printf 'Invalid candidate readiness timeout: %s\n' "${CANDIDATE_READY_TIMEOUT_SECONDS}" >&2
  exit 2
}
[[ "${POST_SMOKE_COOLDOWN_SECONDS}" =~ ^[0-9]+$ ]] || {
  printf 'Invalid post-smoke cooldown: %s\n' "${POST_SMOKE_COOLDOWN_SECONDS}" >&2
  exit 2
}
[[ "${POST_SWITCH_STABILIZE_TIMEOUT_SECONDS}" =~ ^[0-9]+$ && "${POST_SWITCH_STABILIZE_TIMEOUT_SECONDS}" -ge 1 ]] || {
  printf 'Invalid post-switch stabilization timeout: %s\n' "${POST_SWITCH_STABILIZE_TIMEOUT_SECONDS}" >&2
  exit 2
}
[[ "${OBSERVATION_MAX_AVERAGE_MS}" =~ ^[0-9]+$ && "${OBSERVATION_MAX_AVERAGE_MS}" -ge 1 ]] || {
  printf 'Invalid observation average latency: %s\n' "${OBSERVATION_MAX_AVERAGE_MS}" >&2
  exit 2
}
[[ "${MEMORY_RECOVERY_TIMEOUT_SECONDS}" =~ ^[0-9]+$ && "${MEMORY_RECOVERY_TIMEOUT_SECONDS}" -ge 1 ]] || {
  printf 'Invalid memory recovery timeout: %s\n' "${MEMORY_RECOVERY_TIMEOUT_SECONDS}" >&2
  exit 2
}
if [[ -n "${SSH_PORT}" ]]; then
  SSH_ARGS+=( -p "${SSH_PORT}" )
  SCP_ARGS+=( -P "${SSH_PORT}" )
fi
for command in ssh scp; do command -v "${command}" >/dev/null; done
if [[ -n "${SSHPASS:-}" ]]; then
  command -v sshpass >/dev/null
  SSH_COMMAND=( sshpass -e ssh )
  SCP_COMMAND=( sshpass -e scp )
fi

work_dir="$(mktemp -d "${TMPDIR:-/tmp}/qwen2api-deploy.XXXXXX")"
archive_path="${work_dir}/qwen2api-${IMAGE_TAG}.tar.zst"
remote_run_id="$(date +%s)-$$-${RANDOM}"
remote_archive="/tmp/qwen2api-${IMAGE_TAG}-${remote_run_id}.tar.zst"
parts_dir="${work_dir}/parts"
remote_parts_dir="${remote_archive}.parts"
remote_stage_dir="/tmp/qwen2api-deploy-${remote_run_id}"
remote_smoke_script="${remote_stage_dir}/smoke-us-candidate.sh"
cleanup() { rm -rf "${work_dir}"; }
trap cleanup EXIT

if [[ "${ACTION}" == "deploy" && "${REUSE_REMOTE_IMAGE}" != "true" ]]; then
  for command in docker zstd; do command -v "${command}" >/dev/null; done
  docker info >/dev/null
  if [[ "${SKIP_LOCAL_BUILD:-false}" != "true" ]]; then
    SAVE_TAR=false "${ROOT_DIR}/scripts/build-docker-image.sh" "${IMAGE_TAG}"
  fi
  docker image inspect "qwen2api:${IMAGE_TAG}" >/dev/null
  docker save "qwen2api:${IMAGE_TAG}" | zstd -T0 -3 -o "${archive_path}"
  mkdir -p "${parts_dir}"
  split -b "${UPLOAD_CHUNK_SIZE:-32m}" "${archive_path}" "${parts_dir}/part-"

  "${SSH_COMMAND[@]}" "${SSH_ARGS[@]}" "${SSH_TARGET}" 'command -v docker >/dev/null && command -v zstd >/dev/null && command -v python3 >/dev/null && command -v flock >/dev/null'
  "${SSH_COMMAND[@]}" "${SSH_ARGS[@]}" "${SSH_TARGET}" python3 - "${remote_archive}" "${remote_parts_dir}" <<'PY'
from pathlib import Path
import shutil
import sys

archive = Path(sys.argv[1])
parts_dir = Path(sys.argv[2])
archive.unlink(missing_ok=True)
shutil.rmtree(parts_dir, ignore_errors=True)
parts_dir.mkdir(mode=0o700)
PY
  for part in "${parts_dir}"/*; do
    "${SCP_COMMAND[@]}" "${SCP_ARGS[@]}" "${part}" "${SSH_TARGET}:${remote_parts_dir}/"
  done
  "${SSH_COMMAND[@]}" "${SSH_ARGS[@]}" "${SSH_TARGET}" python3 - "${remote_archive}" "${remote_parts_dir}" <<'PY'
from pathlib import Path
import shutil
import sys

archive = Path(sys.argv[1])
parts_dir = Path(sys.argv[2])
with archive.open("wb") as output:
    for part in sorted(parts_dir.iterdir()):
        with part.open("rb") as source:
            shutil.copyfileobj(source, output)
shutil.rmtree(parts_dir)
PY
elif [[ "${ACTION}" == "deploy" ]]; then
  [[ "${SKIP_LOCAL_BUILD:-false}" == "true" ]] || {
    printf 'REUSE_REMOTE_IMAGE=true requires SKIP_LOCAL_BUILD=true.\n' >&2
    exit 2
  }
  docker image inspect "qwen2api:${IMAGE_TAG}" >/dev/null
  LOCAL_BINARY_SHA="$(docker run --rm --entrypoint sha256sum "qwen2api:${IMAGE_TAG}" /usr/local/bin/qwen2api | awk '{print $1}')"
  [[ "${LOCAL_BINARY_SHA}" =~ ^[0-9a-f]{64}$ ]] || { printf 'Unable to calculate local binary digest.\n' >&2; exit 1; }
fi

"${SSH_COMMAND[@]}" "${SSH_ARGS[@]}" "${SSH_TARGET}" install -d -m 700 "${remote_stage_dir}"
if [[ "${ACTION}" == "deploy" ]]; then
  "${SCP_COMMAND[@]}" "${SCP_ARGS[@]}" \
    "${ROOT_DIR}/deploy/us/docker-compose.yml" \
    "${ROOT_DIR}/deploy/us/docker-compose.blue-green.yml" \
    "${ROOT_DIR}/deploy/us/docker-compose.release-a.yml" \
    "${ROOT_DIR}/deploy/us/docker-compose.release-b.yml" \
    "${ROOT_DIR}/deploy/us/docker-compose.router.yml" \
    "${ROOT_DIR}/deploy/us/qwen2api-router.conf.template" \
    "${SSH_TARGET}:${remote_stage_dir}/"
fi
"${SCP_COMMAND[@]}" "${SCP_ARGS[@]}" "${ROOT_DIR}/scripts/smoke-us-candidate.sh" "${SSH_TARGET}:${remote_smoke_script}"
"${SSH_COMMAND[@]}" "${SSH_ARGS[@]}" "${SSH_TARGET}" chmod 700 "${remote_smoke_script}"

"${SSH_COMMAND[@]}" "${SSH_ARGS[@]}" "${SSH_TARGET}" bash -s -- \
  "${ACTION}" "${IMAGE_TAG}" "${REMOTE_DIR}" "${remote_archive}" "${PUBLIC_HEALTH_URL}" \
  "17863" "17864" "${RELEASE_STAGE}" "${POST_SWITCH_OBSERVE_SECONDS}" \
  "${SMOKE_CHAT_SYNTHETIC_COUNT}" "${remote_smoke_script}" "${remote_stage_dir}" \
  "${CANDIDATE_READY_TIMEOUT_SECONDS}" "${REUSE_REMOTE_IMAGE}" "${LOCAL_BINARY_SHA}" \
  "${POST_SMOKE_COOLDOWN_SECONDS}" "${POST_SWITCH_STABILIZE_TIMEOUT_SECONDS}" \
  "${OBSERVATION_MAX_AVERAGE_MS}" "${MEMORY_RECOVERY_TIMEOUT_SECONDS}" <<'REMOTE'
set -Eeuo pipefail
action="$1"
new_tag="$2"
deploy_dir="$3"
archive="$4"
public_health_url="$5"
blue_port="$6"
green_port="$7"
release_stage="$8"
observe_seconds="$9"
smoke_chat_count="${10}"
smoke_script="${11}"
stage_dir="${12}"
candidate_ready_timeout_seconds="${13}"
reuse_remote_image="${14}"
expected_binary_sha="${15}"
post_smoke_cooldown_seconds="${16}"
post_switch_stabilize_timeout_seconds="${17}"
observation_max_average_ms="${18}"
memory_recovery_timeout_seconds="${19}"
state_file="${deploy_dir}/.active-slot"
previous_file="${deploy_dir}/.previous-slot"
router_config="${deploy_dir}/qwen2api-router.conf"
router_template="${deploy_dir}/qwen2api-router.conf.template"
router_backup="${router_config}.deploy-backup"
router_compose="${deploy_dir}/docker-compose.router.yml"
stable_ports=( 7860 17861 17862 )

cleanup_archive() {
  if [[ "${action}" == "deploy" ]]; then
    rm -f "${archive}"
  fi
  rm -rf "${stage_dir}"
}
trap cleanup_archive EXIT

cd "${deploy_dir}"
exec 9>"${deploy_dir}/.deploy.lock"
flock -n 9 || { printf 'deployment result=failed reason=lock_busy\n' >&2; exit 1; }
test -f /opt/ai-governance/AGENTS.md
test -f /opt/ai-governance/docs/nginx/POLICY.md
test -f /opt/ai-governance/docs/nginx/DEPLOYMENT.md
/usr/local/bin/codeai-nginx-audit >/dev/null
test -f .env.compose
test -x "${smoke_script}"
install -d -m 0700 "${deploy_dir}/secrets"
if grep -q '^UPSTREAM_PROXY_TEMPLATE_FILE=' .env.compose || grep -q '^UPSTREAM_PROXY_UUIDS_FILE=' .env.compose; then
  test -s "${deploy_dir}/secrets/upstream_proxy_template"
  test -s "${deploy_dir}/secrets/upstream_proxy_uuids"
  chmod 0600 "${deploy_dir}/secrets/upstream_proxy_template" "${deploy_dir}/secrets/upstream_proxy_uuids"
fi
if [[ "${action}" == "deploy" ]]; then
  for file in docker-compose.yml docker-compose.blue-green.yml docker-compose.release-a.yml docker-compose.release-b.yml docker-compose.router.yml qwen2api-router.conf.template; do
    install -m 0644 "${stage_dir}/${file}" "${deploy_dir}/${file}"
  done
fi

slot_port() {
  case "$1" in
    blue) printf '%s\n' "${blue_port}" ;;
    green) printf '%s\n' "${green_port}" ;;
    *) return 1 ;;
  esac
}

opposite_slot() {
  case "$1" in
    blue) printf 'green\n' ;;
    green) printf 'blue\n' ;;
    *) return 1 ;;
  esac
}

atomic_write() {
  local path="$1" value="$2" tmp
  tmp="${path}.tmp.$$"
  printf '%s\n' "${value}" > "${tmp}"
  mv "${tmp}" "${path}"
}

slot_tag() {
  local slot="$1" path="${deploy_dir}/.slot-$1-tag"
  if [[ -f "${path}" ]]; then
    cat "${path}"
    return
  fi
  docker inspect -f '{{.Config.Image}}' "qwen2api-${slot}" | sed 's/^qwen2api://'
}

render_router_config() {
  python3 - "${router_template}" "${router_config}" "$1" <<'PY'
from pathlib import Path
import sys

template = Path(sys.argv[1]).read_text(encoding="utf-8")
port = sys.argv[3]
if not port.isdigit():
    raise SystemExit("invalid router target port")
target = Path(sys.argv[2])
# 单文件 bind mount 绑定 inode；原地写入才能让运行中的路由容器看到新内容。
target.write_text(template.replace("__QWEN2API_TARGET_PORT__", port), encoding="utf-8")
PY
}

verify_slot_ready() {
  curl -fsS "http://172.17.0.1:$1/healthz" >/dev/null
  curl -fsS "http://172.17.0.1:$1/readyz" >/dev/null
}

verify_stable_routes() {
  local port
  for port in "${stable_ports[@]}"; do
    curl -fsS "http://172.17.0.1:${port}/healthz" >/dev/null
    curl -fsS "http://172.17.0.1:${port}/readyz" >/dev/null
  done
  curl -fsS "${public_health_url}" >/dev/null
}

# wait_stable_routes 等待路由旧 worker 退出，避免旧槽停止后的瞬时 502 误判。
wait_stable_routes() {
  local attempts
  attempts=$(( (post_switch_stabilize_timeout_seconds + 1) / 2 ))
  for _ in $(seq 1 "${attempts}"); do
    if verify_stable_routes; then
      return 0
    fi
    sleep 2
  done
  return 1
}

switch_router() {
  local host_config_sha container_config_sha compose_recreate=false
  render_router_config "$1"
  host_config_sha="$(sha256sum "${router_config}" | awk '{print $1}')"
  container_config_sha="$(docker exec qwen2api-router sha256sum /etc/nginx/conf.d/default.conf 2>/dev/null | awk '{print $1}' || true)"
  # 兼容历史原子替换遗留的失联 bind mount；仅在 inode 已脱节时重建内部路由。
  if [[ "${container_config_sha}" != "${host_config_sha}" ]]; then
    compose_recreate=true
  fi
  if [[ "${compose_recreate}" == "true" ]]; then
    docker compose -p qwen2api_router -f "${router_compose}" up -d --force-recreate >/dev/null
  else
    docker compose -p qwen2api_router -f "${router_compose}" up -d >/dev/null
  fi
  docker exec qwen2api-router nginx -t >/dev/null
  docker exec qwen2api-router nginx -s reload
  for _ in $(seq 1 30); do
    verify_stable_routes && return 0
    sleep 1
  done
  return 1
}

memory_current() {
  docker exec "$1" sh -c 'cat /sys/fs/cgroup/memory.current 2>/dev/null || cat /sys/fs/cgroup/memory/memory.usage_in_bytes' | tr -d '\r'
}

memory_peak() {
  docker exec "$1" sh -c 'cat /sys/fs/cgroup/memory.peak 2>/dev/null || cat /sys/fs/cgroup/memory/memory.max_usage_in_bytes 2>/dev/null || cat /sys/fs/cgroup/memory.current' | tr -d '\r'
}

io_bytes() {
  docker exec "$1" sh -c "awk '{for(i=1;i<=NF;i++){if(\$i~/^rbytes=/||\$i~/^wbytes=/){split(\$i,a,\"=\");s+=a[2]}}} END{print s+0}' /sys/fs/cgroup/io.stat 2>/dev/null || echo 0" | tr -d '\r'
}

run_full_smoke() {
  local slot="$1" port="$2" shm_tasks=0 refresh_assertions=true
  local container="qwen2api-${slot}"
  [[ "${release_stage}" == "B" ]] && shm_tasks=5
  refresh_assertions=true
  [[ "${action}" == "rollback" ]] && refresh_assertions=false
  SMOKE_CHAT_SYNTHETIC_COUNT="${smoke_chat_count}" SHM_VALIDATION_TASKS="${shm_tasks}" REFRESH_ASSERTIONS_REQUIRED="${refresh_assertions}" \
    "${smoke_script}" "http://172.17.0.1:${port}" "${container}" "${deploy_dir}/.env.compose" "${deploy_dir}/data"
}

observe_release() {
  local slot="$1" started now next_chat cpu current_memory output duration
  local container="qwen2api-${slot}"
  local metrics_output goroutines goroutine_initial=0 goroutine_previous=0 goroutine_peak=0 goroutine_growth_streak=0
  local over_cpu=0 checks=0 failures=0 total_duration=0 probe_logs empty_cleanup_logs
  started="$(date +%s)"
  next_chat="${started}"
  while true; do
    now="$(date +%s)"
    (( now - started >= observe_seconds )) && break
    verify_stable_routes || return 1
    current_memory="$(memory_current "${container}")"
    [[ "${current_memory}" =~ ^[0-9]+$ && "${current_memory}" -lt 1073741824 ]] || return 1
    cpu="$(docker stats --no-stream --format '{{.CPUPerc}}' "${container}" | tr -d '%')"
    if python3 - "${cpu}" <<'PY'
import sys
raise SystemExit(0 if float(sys.argv[1] or 0) > 100 else 1)
PY
    then
      over_cpu=$((over_cpu + 1))
      (( over_cpu >= 10 )) && return 1
    else
      over_cpu=0
    fi
    metrics_output="$(SMOKE_MODE=metrics "${smoke_script}" \
      "http://172.17.0.1:7860" "${container}" "${deploy_dir}/.env.compose" "${deploy_dir}/data")" || return 1
    goroutines="$(printf '%s\n' "${metrics_output}" | awk '/step=runtime_metrics / {for(i=1;i<=NF;i++) if($i~/^goroutines=/){split($i,a,"=");print a[2]}}' | tail -1)"
    [[ "${goroutines}" =~ ^[0-9]+$ ]] || return 1
    if (( goroutine_initial == 0 )); then goroutine_initial="${goroutines}"; fi
    if (( goroutines > goroutine_peak )); then goroutine_peak="${goroutines}"; fi
    if (( goroutine_previous > 0 && goroutines > goroutine_previous )); then
      goroutine_growth_streak=$((goroutine_growth_streak + 1))
    else
      goroutine_growth_streak=0
    fi
    goroutine_previous="${goroutines}"
    if (( goroutine_growth_streak >= 10 && goroutines > goroutine_initial + 20 )); then return 1; fi
    if (( now >= next_chat )); then
      if output="$(SMOKE_MODE=chat-only SMOKE_CHAT_SYNTHETIC_COUNT=1 "${smoke_script}" \
        "http://172.17.0.1:7860" "${container}" "${deploy_dir}/.env.compose" "${deploy_dir}/data" 2>&1)"; then
        :
      else
        failures=$((failures + 1))
      fi
      duration="$(printf '%s\n' "${output}" | awk '/step=chat_synthetic_1 / {for(i=1;i<=NF;i++) if($i~/^duration_ms=/){split($i,a,"=");print a[2]}}' | tail -1)"
      if [[ "${duration:-}" =~ ^[0-9]+$ ]]; then
        total_duration=$((total_duration + duration))
        checks=$((checks + 1))
      fi
      next_chat=$((now + 180))
    fi
    sleep 60
  done
  (( checks > 0 )) || return 1
  (( failures * 100 < checks * 2 )) || return 1
  (( total_duration / checks <= observation_max_average_ms )) || return 1
  probe_logs="$(docker logs --since "${observe_seconds}s" "${container}" 2>&1 | grep -Ec '请求(进入|完成).*path=/(healthz|readyz)' || true)"
  empty_cleanup_logs="$(docker logs --since "${observe_seconds}s" "${container}" 2>&1 | grep -Ec '上下文缓存清理完成.*removed_records=0.*removed_files=0' || true)"
  (( probe_logs <= 35 )) || return 1
  (( empty_cleanup_logs == 0 )) || return 1
  printf 'observation result=passed checks=%s failures=%s average_ms=%s probe_logs=%s empty_cleanup_logs=%s goroutines_initial=%s goroutines_peak=%s goroutines_final=%s\n' \
    "${checks}" "${failures}" "$((total_duration / checks))" "${probe_logs}" "${empty_cleanup_logs}" \
    "${goroutine_initial}" "${goroutine_peak}" "${goroutine_previous}"
}

if [[ -f "${state_file}" ]]; then
  active_slot="$(cat "${state_file}")"
else
  # 首次接管旧部署时，从唯一存在的槽位容器恢复蓝绿基线。
  existing_slots=()
  for slot in blue green; do
    if docker inspect "qwen2api-${slot}" >/dev/null 2>&1; then existing_slots+=( "${slot}" ); fi
  done
  [[ "${#existing_slots[@]}" -eq 1 ]] || {
    printf 'Missing active slot state and expected exactly one existing slot container, found=%s\n' \
      "${#existing_slots[@]}" >&2
    exit 1
  }
  active_slot="${existing_slots[0]}"
  printf 'deployment baseline=inferred active_slot=%s\n' "${active_slot}"
fi
active_port="$(slot_port "${active_slot}")"
active_tag="$(slot_tag "${active_slot}")"
active_was_running="$(docker inspect -f '{{.State.Running}}' "qwen2api-${active_slot}" 2>/dev/null || printf 'false')"
router_was_running="$(docker inspect -f '{{.State.Running}}' qwen2api-router 2>/dev/null || printf 'false')"

if [[ "${action}" == "rollback" ]]; then
  target_slot="$(if [[ -f "${previous_file}" ]]; then cat "${previous_file}"; else opposite_slot "${active_slot}"; fi)"
  target_port="$(slot_port "${target_slot}")"
  target_tag="$(slot_tag "${target_slot}")"
  test "${target_slot}" != "${active_slot}"
  docker inspect "qwen2api-${target_slot}" >/dev/null
  docker start "qwen2api-${target_slot}" >/dev/null
  for _ in $(seq 1 60); do verify_slot_ready "${target_port}" && break; sleep 2; done
  verify_slot_ready "${target_port}"
  cp "${router_config}" "${router_backup}"
  restore_rollback_route() {
    set +e
    cp "${router_backup}" "${router_config}"
    docker start "qwen2api-${active_slot}" >/dev/null 2>&1 || true
    docker compose -p qwen2api_router -f "${router_compose}" up -d >/dev/null 2>&1 || true
    docker exec qwen2api-router nginx -t >/dev/null 2>&1 || true
    docker exec qwen2api-router nginx -s reload >/dev/null 2>&1 || true
    docker stop "qwen2api-${target_slot}" >/dev/null 2>&1 || true
  }
  trap 'restore_rollback_route; cleanup_archive' ERR
  switch_router "${target_port}"
  run_full_smoke "${target_slot}" "${target_port}"
  docker stop "qwen2api-${active_slot}" >/dev/null
  [[ "$(docker inspect -f '{{.State.Running}}' "qwen2api-${active_slot}")" == "false" ]]
  atomic_write "${state_file}" "${target_slot}"
  atomic_write "${previous_file}" "${active_slot}"
  atomic_write "${deploy_dir}/.active-tag" "${target_tag}"
  atomic_write "${deploy_dir}/.previous-tag" "${active_tag}"
  rm -f "${router_backup}"
  trap - ERR
  trap cleanup_archive EXIT
  printf 'rollback result=passed active_slot=%s previous_slot=%s\n' "${target_slot}" "${active_slot}"
  exit 0
fi

if [[ "${reuse_remote_image}" == "true" ]]; then
  actual_binary_sha="$(docker run --rm --entrypoint sha256sum "qwen2api:${new_tag}" /usr/local/bin/qwen2api | awk '{print $1}')"
  [[ "${expected_binary_sha}" =~ ^[0-9a-f]{64}$ && "${actual_binary_sha}" == "${expected_binary_sha}" ]] || {
    printf 'deployment result=failed reason=remote_image_digest_mismatch\n' >&2
    exit 1
  }
  printf 'remote image reuse=verified binary_sha=%s\n' "${actual_binary_sha:0:12}"
else
  zstd -dc "${archive}" | docker load
fi
docker image inspect "qwen2api:${new_tag}" >/dev/null
candidate_slot="$(opposite_slot "${active_slot}")"
candidate_port="$(slot_port "${candidate_slot}")"
compose_files=( -f docker-compose.yml -f docker-compose.blue-green.yml -f docker-compose.release-a.yml )
if [[ "${release_stage}" == "B" ]]; then
  pids_limit="$(awk -F= '$1=="PIDS_LIMIT" {print $2}' .env.compose | tail -1)"
  [[ "${pids_limit}" =~ ^[0-9]+$ && "${pids_limit}" -ge 128 ]]
  compose_files+=( -f docker-compose.release-b.yml )
fi

# 候选槽会覆盖两次部署前遗留的旧版本；先移除同名容器，避免历史 Compose 项目名差异造成冲突。
candidate_container="qwen2api-${candidate_slot}"
if docker container inspect "${candidate_container}" >/dev/null 2>&1; then
  docker rm -f "${candidate_container}" >/dev/null
fi

DEPLOY_SLOT="${candidate_slot}" DEPLOY_PORT="${candidate_port}" QWEN2API_TAG="${new_tag}" \
  docker compose -p "qwen2api_${candidate_slot}" --env-file .env.compose "${compose_files[@]}" up -d
candidate_ready=false
for _ in $(seq 1 "$(( (candidate_ready_timeout_seconds + 9) / 10 ))"); do
  if verify_slot_ready "${candidate_port}"; then candidate_ready=true; break; fi
  sleep 10
done
if [[ "${candidate_ready}" != "true" ]]; then
  docker logs --tail 100 "qwen2api-${candidate_slot}" 2>&1 | sed -E 's/(Authorization|Cookie|Token|password)[^ ]*/\1=[REDACTED]/Ig' >&2 || true
  docker stop "qwen2api-${candidate_slot}" >/dev/null 2>&1 || true
  exit 1
fi

baseline_memory="$(memory_current "${candidate_container}")"
io_before="$(io_bytes "${candidate_container}")"
shm_peak_file="${deploy_dir}/.shm-peak-${candidate_slot}.tmp"
printf '0\n' > "${shm_peak_file}"
monitor_shm=false
if [[ "${release_stage}" == "B" ]]; then
  monitor_shm=true
  (
    while [[ -f "${shm_peak_file}" ]]; do
      used="$(docker exec "${candidate_container}" df -B1 /dev/shm 2>/dev/null | awk 'NR==2 {print $3}')"
      peak="$(cat "${shm_peak_file}")"
      if [[ "${used:-0}" =~ ^[0-9]+$ && "${used}" -gt "${peak:-0}" ]]; then printf '%s\n' "${used}" > "${shm_peak_file}"; fi
      sleep 1
    done
  ) &
  shm_monitor_pid=$!
fi

candidate_smoke_passed=false
if run_full_smoke "${candidate_slot}" "${candidate_port}"; then candidate_smoke_passed=true; fi
if [[ "${monitor_shm}" == "true" ]]; then
  shm_peak="$(cat "${shm_peak_file}")"
  rm -f "${shm_peak_file}"
  wait "${shm_monitor_pid}" || true
  [[ "${shm_peak}" =~ ^[0-9]+$ && "${shm_peak}" -lt 402653184 ]] || candidate_smoke_passed=false
fi
if [[ "${candidate_smoke_passed}" != "true" ]]; then docker stop "${candidate_container}" >/dev/null 2>&1 || true; exit 1; fi
if (( post_smoke_cooldown_seconds > 0 )); then
  printf 'candidate cooldown seconds=%s\n' "${post_smoke_cooldown_seconds}"
  sleep "${post_smoke_cooldown_seconds}"
  verify_slot_ready "${candidate_port}"
fi

peak_memory="$(memory_peak "${candidate_container}")"
[[ "${peak_memory}" =~ ^[0-9]+$ && "${peak_memory}" -lt 1073741824 ]]
[[ "$(docker inspect -f '{{.State.Running}}' "${candidate_container}")" == "true" ]]
[[ "$(docker inspect -f '{{.State.OOMKilled}}' "${candidate_container}")" == "false" ]]
if docker logs "${candidate_container}" 2>&1 | grep -Eq 'panic:|fatal error:'; then
  printf 'deployment result=failed reason=runtime_panic\n' >&2
  exit 1
fi
io_after="$(io_bytes "${candidate_container}")"
io_delta=$((io_after - io_before))
if [[ "${release_stage}" == "A" ]]; then
  atomic_write "${deploy_dir}/.release-a-browser-io-bytes" "${io_delta}"
elif [[ -f "${deploy_dir}/.release-a-browser-io-bytes" ]]; then
  release_a_io="$(cat "${deploy_dir}/.release-a-browser-io-bytes")"
  [[ "${release_a_io}" =~ ^[0-9]+$ && "${io_delta}" -lt "${release_a_io}" ]]
fi

router_switched=false
old_stopped=false
rollback_routes() {
  set +e
  if [[ "${old_stopped}" == "true" && "${active_was_running}" == "true" ]]; then
    docker start "qwen2api-${active_slot}" >/dev/null 2>&1 || true
  else
    docker stop "qwen2api-${active_slot}" >/dev/null 2>&1 || true
  fi
  if [[ "${router_switched}" == "true" && -f "${router_backup}" && "${router_was_running}" == "true" ]]; then
    cp "${router_backup}" "${router_config}"
    docker compose -p qwen2api_router -f "${router_compose}" up -d >/dev/null 2>&1 || true
    docker exec qwen2api-router nginx -t >/dev/null 2>&1 || true
    docker exec qwen2api-router nginx -s reload >/dev/null 2>&1 || true
  elif [[ "${router_was_running}" != "true" ]]; then
    docker stop qwen2api-router >/dev/null 2>&1 || true
  fi
  docker stop "${candidate_container}" >/dev/null 2>&1 || true
  rm -f "${router_backup}"
}
trap 'rollback_routes; cleanup_archive' ERR
cp "${router_config}" "${router_backup}"
router_switched=true
switch_router "${candidate_port}"
docker stop "qwen2api-${active_slot}" >/dev/null
old_stopped=true
[[ "$(docker inspect -f '{{.State.Running}}' "qwen2api-${active_slot}")" == "false" ]]
wait_stable_routes

if (( observe_seconds > 0 )); then
  if ! observe_release "${candidate_slot}"; then
    printf 'deployment result=failed reason=observation_gate\n' >&2
    rollback_routes
    cleanup_archive
    trap - ERR
    exit 1
  fi
fi
sleep 600
recovery_limit=$((baseline_memory * 120 / 100))
memory_recovery_deadline=$(( $(date +%s) + memory_recovery_timeout_seconds ))
memory_recovered=false
while true; do
  recovered_memory="$(memory_current "${candidate_container}" 2>/dev/null || true)"
  if [[ "${recovered_memory}" =~ ^[0-9]+$ && "${recovered_memory}" -le "${recovery_limit}" ]]; then
    memory_recovered=true
    break
  fi
  (( $(date +%s) >= memory_recovery_deadline )) && break
  sleep 10
done
if [[ "${memory_recovered}" != "true" ]]; then
  printf 'deployment result=failed reason=memory_recovery_gate\n' >&2
  rollback_routes
  cleanup_archive
  trap - ERR
  exit 1
fi
printf 'memory recovery=passed current_bytes=%s limit_bytes=%s\n' "${recovered_memory}" "${recovery_limit}"

atomic_write "${previous_file}" "${active_slot}"
atomic_write "${state_file}" "${candidate_slot}"
atomic_write "${deploy_dir}/.slot-${active_slot}-tag" "${active_tag}"
atomic_write "${deploy_dir}/.slot-${candidate_slot}-tag" "${new_tag}"
atomic_write "${deploy_dir}/.previous-tag" "${active_tag}"
atomic_write "${deploy_dir}/.active-tag" "${new_tag}"
rm -f "${router_backup}"
trap - ERR
trap cleanup_archive EXIT

docker ps --filter "name=${candidate_container}" --format '{{.Names}} {{.Image}} {{.Status}}'
docker ps --filter "name=qwen2api-router" --format '{{.Names}} {{.Image}} {{.Status}}'
verify_stable_routes
printf 'deployment result=passed release_stage=%s active_slot=%s previous_slot=%s image_tag=%s\n' \
  "${release_stage}" "${candidate_slot}" "${active_slot}" "${new_tag}"
REMOTE
