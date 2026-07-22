#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REVISION="$(git -C "${ROOT_DIR}" rev-parse --short HEAD)"
IMAGE_TAG="${1:-dev-go-${REVISION}}"
SSH_TARGET="${US_SSH_TARGET:-us}"
SSH_PORT="${US_SSH_PORT:-}"
REMOTE_DIR="${US_DEPLOY_DIR:-/opt/docker_projects/qwen2api}"
PUBLIC_HEALTH_URL="${US_PUBLIC_HEALTH_URL:-https://qwen2api.codeai.de5.net/healthz}"
PROXY_CONFIG="${US_PROXY_CONFIG:-/opt/docker_projects/nginx-proxy/nginx.conf}"
PROXY_CONTAINER="${US_PROXY_CONTAINER:-nginx-proxy}"
BLUE_PORT="${US_BLUE_PORT:-17861}"
GREEN_PORT="${US_GREEN_PORT:-17862}"
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
  "${PROXY_CONFIG}" "${PROXY_CONTAINER}" "${BLUE_PORT}" "${GREEN_PORT}" <<'REMOTE'
set -euo pipefail
new_tag="$1"
deploy_dir="$2"
archive="$3"
public_health_url="$4"
proxy_config="$5"
proxy_container="$6"
blue_port="$7"
green_port="$8"
state_file="${deploy_dir}/.active-slot"
proxy_backup="${proxy_config}.qwen2api-deploy-backup"

cleanup_archive() { rm -f "${archive}"; }
trap cleanup_archive EXIT
zstd -dc "${archive}" | docker load
docker image inspect "qwen2api:${new_tag}" >/dev/null
cd "${deploy_dir}"
test -f .env.compose

active_slot="legacy"
[[ -f "${state_file}" ]] && active_slot="$(cat "${state_file}")"
case "${active_slot}" in
  blue) candidate_slot="green"; candidate_port="${green_port}" ;;
  green) candidate_slot="blue"; candidate_port="${blue_port}" ;;
  legacy) candidate_slot="green"; candidate_port="${green_port}" ;;
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

cp "${proxy_config}" "${proxy_backup}"
rollback_proxy() {
  cp "${proxy_backup}" "${proxy_config}"
  docker exec "${proxy_container}" nginx -t >/dev/null
  docker exec "${proxy_container}" nginx -s reload
  docker stop "qwen2api-${candidate_slot}" >/dev/null 2>&1 || true
  rm -f "${proxy_backup}"
}
trap 'rollback_proxy; cleanup_archive' ERR

QWEN2API_PROXY_CONFIG="${proxy_config}" QWEN2API_TARGET_PORT="${candidate_port}" python3 - <<'PY'
import os
import re
from pathlib import Path

path = Path(os.environ["QWEN2API_PROXY_CONFIG"])
port = os.environ["QWEN2API_TARGET_PORT"]
text = path.read_text()
marker_at = text.find("server_name qwen2api.codeai.de5.net;")
if marker_at < 0:
    raise SystemExit("qwen2api server block not found")
start = text.rfind("server {", 0, marker_at)
if start < 0:
    raise SystemExit("qwen2api server block start not found")
depth = 0
end = -1
for index in range(start, len(text)):
    if text[index] == "{":
        depth += 1
    elif text[index] == "}":
        depth -= 1
        if depth == 0:
            end = index + 1
            break
if end < 0:
    raise SystemExit("qwen2api server block end not found")
block, count = re.subn(
    r"proxy_pass http://host\.docker\.internal:\d+;",
    f"proxy_pass http://host.docker.internal:{port};",
    text[start:end],
)
if count == 0:
    raise SystemExit("qwen2api proxy_pass not found")
path.write_text(text[:start] + block + text[end:])
PY

docker exec "${proxy_container}" nginx -t >/dev/null
docker exec "${proxy_container}" nginx -s reload
for _ in $(seq 1 30); do
  curl -fsS "${public_health_url}" >/dev/null && break
  sleep 2
done
curl -fsS "${public_health_url}" >/dev/null

printf '%s\n' "${candidate_slot}" > "${state_file}"
printf '%s\n' "${new_tag}" > "${deploy_dir}/.slot-${candidate_slot}-tag"
case "${active_slot}" in
  blue|green) docker stop "qwen2api-${active_slot}" >/dev/null 2>&1 || true ;;
  legacy) docker stop qwen2api >/dev/null 2>&1 || true ;;
esac
rm -f "${proxy_backup}"
trap cleanup_archive EXIT

docker ps --filter "name=qwen2api-${candidate_slot}" --format '{{.Names}} {{.Image}} {{.Status}}'
curl -fsS "${public_health_url}"
REMOTE
