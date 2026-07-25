#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REVISION="$(git -C "${ROOT_DIR}" rev-parse --short HEAD)"
IMAGE_TAG="${1:-dev-go-${REVISION}}"
SSH_TARGET="${US_SSH_TARGET:-us}"
SSH_PORT="${US_SSH_PORT:-}"
REMOTE_DIR="${US_DEPLOY_DIR:-/opt/docker_projects/qwen2api}"
PUBLIC_HEALTH_URL="${US_PUBLIC_HEALTH_URL:-https://qwen2api.codeai.de5.net/healthz}"
BLUE_PORT="17863"
GREEN_PORT="17864"
SSH_ARGS=()
SCP_ARGS=()

if [[ "${CONFIRM_PRODUCTION_DEPLOY:-}" != "yes" ]]; then
  printf 'Set CONFIRM_PRODUCTION_DEPLOY=yes to deploy qwen2api:%s to %s.\n' "${IMAGE_TAG}" "${SSH_TARGET}" >&2
  exit 2
fi
if [[ ! "${IMAGE_TAG}" =~ ^[A-Za-z0-9_.-]+$ ]]; then
  printf 'Invalid image tag: %s\n' "${IMAGE_TAG}" >&2
  exit 2
fi
if [[ -n "${SSH_PORT}" ]]; then
  SSH_ARGS+=( -p "${SSH_PORT}" )
  SCP_ARGS+=( -P "${SSH_PORT}" )
fi
for command in docker zstd ssh scp; do
  command -v "${command}" >/dev/null
done
docker info >/dev/null

work_dir="$(mktemp -d "${TMPDIR:-/tmp}/qwen2api-deploy.XXXXXX")"
archive_path="${work_dir}/qwen2api-${IMAGE_TAG}.tar.zst"
remote_archive="/tmp/qwen2api-${IMAGE_TAG}.tar.zst"
parts_dir="${work_dir}/parts"
remote_parts_dir="${remote_archive}.parts"
cleanup() { rm -rf "${work_dir}"; }
trap cleanup EXIT

if [[ "${SKIP_LOCAL_BUILD:-false}" != "true" ]]; then
  SAVE_TAR=false "${ROOT_DIR}/scripts/build-docker-image.sh" "${IMAGE_TAG}"
fi
docker image inspect "qwen2api:${IMAGE_TAG}" >/dev/null
docker save "qwen2api:${IMAGE_TAG}" | zstd -T0 -3 -o "${archive_path}"
mkdir -p "${parts_dir}"
split -b "${UPLOAD_CHUNK_SIZE:-32m}" "${archive_path}" "${parts_dir}/part-"

ssh "${SSH_ARGS[@]}" "${SSH_TARGET}" 'command -v docker >/dev/null && command -v zstd >/dev/null && command -v python3 >/dev/null'
scp "${SCP_ARGS[@]}" \
  "${ROOT_DIR}/deploy/us/docker-compose.yml" \
  "${ROOT_DIR}/deploy/us/docker-compose.blue-green.yml" \
  "${ROOT_DIR}/deploy/us/docker-compose.router.yml" \
  "${ROOT_DIR}/deploy/us/qwen2api-router.conf.template" \
  "${SSH_TARGET}:${REMOTE_DIR}/"
ssh "${SSH_ARGS[@]}" "${SSH_TARGET}" python3 - "${remote_archive}" "${remote_parts_dir}" <<'PY'
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
  scp "${SCP_ARGS[@]}" "${part}" "${SSH_TARGET}:${remote_parts_dir}/"
done
ssh "${SSH_ARGS[@]}" "${SSH_TARGET}" python3 - "${remote_archive}" "${remote_parts_dir}" <<'PY'
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

ssh "${SSH_ARGS[@]}" "${SSH_TARGET}" bash -s -- \
  "${IMAGE_TAG}" "${REMOTE_DIR}" "${remote_archive}" "${PUBLIC_HEALTH_URL}" \
  "${BLUE_PORT}" "${GREEN_PORT}" <<'REMOTE'
set -euo pipefail
new_tag="$1"
deploy_dir="$2"
archive="$3"
public_health_url="$4"
blue_port="$5"
green_port="$6"
state_file="${deploy_dir}/.active-slot"
router_config="${deploy_dir}/qwen2api-router.conf"
router_template="${deploy_dir}/qwen2api-router.conf.template"
router_backup="${router_config}.deploy-backup"
router_compose="${deploy_dir}/docker-compose.router.yml"
stable_ports=( 7860 17861 17862 )

cleanup_archive() { rm -f "${archive}"; }
trap cleanup_archive EXIT
zstd -dc "${archive}" | docker load
docker image inspect "qwen2api:${new_tag}" >/dev/null
cd "${deploy_dir}"
test -f .env.compose

[[ -f "${state_file}" ]] || { printf 'Missing active slot state: %s\n' "${state_file}" >&2; exit 1; }
active_slot="$(cat "${state_file}")"
case "${active_slot}" in
  blue) candidate_slot="green"; candidate_port="${green_port}" ;;
  green) candidate_slot="blue"; candidate_port="${blue_port}" ;;
  *) printf 'Invalid active slot: %s\n' "${active_slot}" >&2; exit 1 ;;
esac

compose_files=( -f docker-compose.yml -f docker-compose.blue-green.yml )
DEPLOY_SLOT="${candidate_slot}" DEPLOY_PORT="${candidate_port}" QWEN2API_TAG="${new_tag}" \
  docker compose -p "qwen2api_${candidate_slot}" --env-file .env.compose "${compose_files[@]}" up -d

candidate_ready=false
for _ in $(seq 1 90); do
  if curl -fsS "http://172.17.0.1:${candidate_port}/healthz" >/dev/null; then
    candidate_ready=true
    break
  fi
  sleep 2
done
if [[ "${candidate_ready}" != "true" ]]; then
  docker logs --tail 100 "qwen2api-${candidate_slot}" >&2 || true
  docker stop "qwen2api-${candidate_slot}" >/dev/null 2>&1 || true
  exit 1
fi

# Render the stable internal router without storing runtime values in Git.
render_router_config() {
  python3 - "${router_template}" "$1" "$2" <<'PY'
from pathlib import Path
import sys

template = Path(sys.argv[1]).read_text()
output = Path(sys.argv[2])
port = sys.argv[3]
if not port.isdigit():
    raise SystemExit("invalid router target port")
output.write_text(template.replace("__QWEN2API_TARGET_PORT__", port))
PY
}

# Reuse the service-owned router for all stable internal and public proxy ports.
switch_router() {
  render_router_config "${router_config}" "$1"
  docker compose -p qwen2api_router -f "${router_compose}" up -d
  docker exec qwen2api-router nginx -t >/dev/null
  docker exec qwen2api-router nginx -s reload
  for _ in $(seq 1 30); do
    ready=true
    for stable_port in "${stable_ports[@]}"; do
      curl -fsS "http://172.17.0.1:${stable_port}/healthz" >/dev/null || ready=false
    done
    [[ "${ready}" == "true" ]] && return 0
    sleep 1
  done
  return 1
}

router_switched=false
old_stopped=false
rollback_routes() {
  set +e
  if [[ "${old_stopped}" == "true" ]]; then
    docker start "qwen2api-${active_slot}" >/dev/null 2>&1 || true
  fi
  if [[ "${router_switched}" == "true" && -f "${router_backup}" ]]; then
    cp "${router_backup}" "${router_config}"
    docker compose -p qwen2api_router -f "${router_compose}" up -d >/dev/null
    docker exec qwen2api-router nginx -t >/dev/null
    docker exec qwen2api-router nginx -s reload
  fi
  docker stop "qwen2api-${candidate_slot}" >/dev/null 2>&1 || true
  rm -f "${router_backup}"
}
trap 'rollback_routes; cleanup_archive' ERR
cp "${router_config}" "${router_backup}"

router_switched=true
switch_router "${candidate_port}"
curl -fsS "${public_health_url}" >/dev/null

docker stop "qwen2api-${active_slot}" >/dev/null
old_stopped=true
[[ "$(docker inspect -f '{{.State.Running}}' "qwen2api-${active_slot}")" == "false" ]]

printf '%s\n' "${candidate_slot}" > "${state_file}"
printf '%s\n' "${new_tag}" > "${deploy_dir}/.slot-${candidate_slot}-tag"
rm -f "${router_backup}"
trap - ERR
trap cleanup_archive EXIT

docker ps --filter "name=qwen2api-${candidate_slot}" --format '{{.Names}} {{.Image}} {{.Status}}'
docker ps --filter "name=qwen2api-router" --format '{{.Names}} {{.Image}} {{.Status}}'
for stable_port in "${stable_ports[@]}"; do
  curl -fsS "http://172.17.0.1:${stable_port}/healthz"
done
curl -fsS "${public_health_url}"
REMOTE
