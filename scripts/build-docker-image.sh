#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REVISION="$(git -C "${ROOT_DIR}" rev-parse --short HEAD)"
IMAGE_TAG="${1:-dev-go-${REVISION}}"
PLATFORM="${PLATFORM:-linux/amd64}"
GOPROXY_VALUE="${GOPROXY:-https://goproxy.cn,direct}"
INSTALL_BROWSERS="${INSTALL_BROWSERS:-true}"

docker buildx build \
  --platform "${PLATFORM}" \
  --build-arg "GOPROXY=${GOPROXY_VALUE}" \
  --build-arg "INSTALL_BROWSERS=${INSTALL_BROWSERS}" \
  -t "qwen2api:${IMAGE_TAG}" \
  --load \
  "${ROOT_DIR}"

OUTPUT="${OUTPUT:-/tmp/qwen2api-${IMAGE_TAG}.tar}"
docker save "qwen2api:${IMAGE_TAG}" -o "${OUTPUT}"

printf 'image=%s\n' "qwen2api:${IMAGE_TAG}"
printf 'tar=%s\n' "${OUTPUT}"
