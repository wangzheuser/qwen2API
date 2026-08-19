#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REVISION="$(git -C "${ROOT_DIR}" rev-parse --short HEAD)"
IMAGE_TAG="${1:-dev-go-${REVISION}}"
PLATFORM="${PLATFORM:-linux/amd64}"
GOPROXY_VALUE="${GOPROXY:-https://goproxy.cn,direct}"
INSTALL_BROWSERS="${INSTALL_BROWSERS:-true}"
PLAYWRIGHT_VERSION="${PLAYWRIGHT_VERSION:-1.57.0}"
DEBIAN_MIRROR="${DEBIAN_MIRROR:-http://mirrors.aliyun.com}"
GO_IMAGE="${GO_IMAGE:-golang:1.26-bookworm}"
NODE_IMAGE="${NODE_IMAGE:-node:20-bookworm-slim}"
RUNTIME_IMAGE="${RUNTIME_IMAGE:-debian:bookworm-slim}"

docker buildx build \
  --platform "${PLATFORM}" \
  --build-arg "GOPROXY=${GOPROXY_VALUE}" \
  --build-arg "INSTALL_BROWSERS=${INSTALL_BROWSERS}" \
  --build-arg "PLAYWRIGHT_VERSION=${PLAYWRIGHT_VERSION}" \
  --build-arg "DEBIAN_MIRROR=${DEBIAN_MIRROR}" \
  --build-arg "GO_IMAGE=${GO_IMAGE}" \
  --build-arg "NODE_IMAGE=${NODE_IMAGE}" \
  --build-arg "RUNTIME_IMAGE=${RUNTIME_IMAGE}" \
  -t "qwen2api:${IMAGE_TAG}" \
  --load \
  "${ROOT_DIR}"

OUTPUT="${OUTPUT:-/tmp/qwen2api-${IMAGE_TAG}.tar}"
printf 'image=%s\n' "qwen2api:${IMAGE_TAG}"
if [[ "${SAVE_TAR:-false}" == "true" ]]; then
  docker save "qwen2api:${IMAGE_TAG}" -o "${OUTPUT}"
  printf 'tar=%s\n' "${OUTPUT}"
fi
