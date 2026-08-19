ARG GO_VERSION=1.26
ARG NODE_VERSION=20
ARG GO_IMAGE=golang:${GO_VERSION}-bookworm
ARG NODE_IMAGE=node:${NODE_VERSION}-bookworm-slim
ARG RUNTIME_IMAGE=debian:bookworm-slim

# Stage 1: build the WebUI assets.
FROM --platform=$BUILDPLATFORM ${NODE_IMAGE} AS frontend-builder
WORKDIR /src/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 1.5: prepare the Playwright driver from npm.
#
# playwright-go v0.5700.1 expects the 1.57.0 driver, but the upstream driver zip
# may be absent from the Playwright CDN while the npm package is already
# available. Supplying package/cli.js here lets playwright-go skip driver
# download and only install browser binaries.
FROM --platform=$BUILDPLATFORM ${NODE_IMAGE} AS playwright-driver
ARG PLAYWRIGHT_VERSION=1.57.0
WORKDIR /src/playwright-driver
RUN npm init -y \
    && npm install --omit=dev --ignore-scripts "playwright-core@${PLAYWRIGHT_VERSION}"

# Stage 2: build the Go backend.
FROM --platform=$BUILDPLATFORM ${GO_IMAGE} AS backend-builder
ARG GOPROXY=https://proxy.golang.org,direct
ENV GOPROXY=${GOPROXY}
WORKDIR /src
COPY backend/go.mod backend/go.sum ./backend/
RUN --mount=type=cache,target=/go/pkg/mod \
    cd backend && go mod download
COPY backend/ ./backend/
ARG TARGETOS
ARG TARGETARCH
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    cd backend && CGO_ENABLED=0 GOOS=${TARGETOS:-linux} GOARCH=${TARGETARCH:-amd64} \
    go build -trimpath -ldflags="-s -w" -o /out/qwen2api .

# Stage 3: runtime image.
FROM ${RUNTIME_IMAGE}
WORKDIR /app
ARG PLAYWRIGHT_VERSION=1.57.0
ARG DEBIAN_MIRROR=http://deb.debian.org

ENV DEBIAN_FRONTEND=noninteractive \
    PORT=7860 \
    LOG_LEVEL=INFO \
    DATA_DIR=/app/data \
    LOGS_DIR=/app/logs \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_DRIVER_PATH=/ms-playwright-driver/${PLAYWRIGHT_VERSION} \
    PLAYWRIGHT_NODEJS_PATH=/usr/bin/node

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    packages="ca-certificates curl wget unzip nodejs \
    libasound2 libatk-bridge2.0-0 libatk1.0-0 libcups2 libdbus-1-3 \
    libdrm2 libgbm1 libglib2.0-0 libgtk-3-0 libnspr4 libnss3 \
    libpangocairo-1.0-0 libpulse0 libx11-6 libx11-xcb1 libxcb1 \
    libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 \
    libxrandr2 libxshmfence1 fonts-liberation fonts-wqy-zenhei" \
    && sed -i "s|http://deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' 'APT::Keep-Downloaded-Packages "true";' > /etc/apt/apt.conf.d/99keep-downloaded-packages \
    && apt-get -o Acquire::Retries=10 update \
    && for attempt in 1 2 3 4 5 6; do \
    apt-get -o Acquire::Retries=10 --fix-missing --fix-broken install -y --no-install-recommends || true; \
    if apt-get -o Acquire::Retries=10 --fix-missing install -y --no-install-recommends ${packages} \
    && dpkg-query -W ${packages} >/dev/null 2>&1; then break; fi; \
    if [ "${attempt}" = "6" ]; then exit 1; fi; \
    sleep $((attempt * 5)); \
    done \
    && rm -f /etc/apt/apt.conf.d/99keep-downloaded-packages

COPY --from=backend-builder /out/qwen2api /usr/local/bin/qwen2api
COPY --from=frontend-builder /src/frontend/dist ./frontend/dist
COPY --from=playwright-driver /src/playwright-driver/node_modules/playwright-core /ms-playwright-driver/${PLAYWRIGHT_VERSION}/package

ARG INSTALL_BROWSERS=true
RUN mkdir -p /app/data /app/logs /ms-playwright \
    && if [ "${INSTALL_BROWSERS}" = "true" ]; then \
        /usr/local/bin/qwen2api --install-browsers; \
    else \
        echo "Skipping Playwright browser install during image build"; \
    fi

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-7860}/healthz" || exit 1

CMD ["/usr/local/bin/qwen2api"]
