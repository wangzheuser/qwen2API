#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:?candidate base URL is required}"
CONTAINER_NAME="${2:?candidate container name is required}"
ENV_FILE="${3:?compose env file is required}"
DATA_DIR="${4:?host data directory is required}"
SMOKE_TIMEOUT_SECONDS="${SMOKE_TIMEOUT_SECONDS:-900}"
SMOKE_MODEL="${SMOKE_MODEL:-gpt-3.5-turbo}"
SMOKE_CHAT_SYNTHETIC_COUNT="${SMOKE_CHAT_SYNTHETIC_COUNT:-20}"
SMOKE_TRANSIENT_RETRIES="${SMOKE_TRANSIENT_RETRIES:-5}"
SMOKE_TRANSIENT_RETRY_DELAY_SECONDS="${SMOKE_TRANSIENT_RETRY_DELAY_SECONDS:-10}"
SMOKE_CHAT_STAGGER_SECONDS="${SMOKE_CHAT_STAGGER_SECONDS:-5}"
SHM_VALIDATION_TASKS="${SHM_VALIDATION_TASKS:-0}"
SMOKE_MODE="${SMOKE_MODE:-full}"
REFRESH_ASSERTIONS_REQUIRED="${REFRESH_ASSERTIONS_REQUIRED:-true}"

work_dir="$(mktemp -d "${TMPDIR:-/tmp}/qwen2api-smoke.XXXXXX")"
body_file="${work_dir}/body"
header_file="${work_dir}/headers"
error_file="${work_dir}/curl-error"
api_config="${work_dir}/api.curl"
admin_config="${work_dir}/admin.curl"
cleanup() { rm -rf "${work_dir}"; }
trap cleanup EXIT

mapfile -t credentials < <(python3 - "${ENV_FILE}" "${DATA_DIR}/api_keys.json" <<'PY'
import json
from pathlib import Path
import re
import sys

env_path = Path(sys.argv[1])
keys_path = Path(sys.argv[2])
values = {}
for raw in env_path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    values[key.strip()] = value.strip()

api_key = ""
for name in ("QWEN_API_KEY", "QWEN_API_KEYS", "API_KEYS"):
    candidates = [part for part in re.split(r"[,;\s]+", values.get(name, "")) if part]
    if candidates:
        api_key = candidates[0]
        break
if not api_key and keys_path.exists():
    try:
        payload = json.loads(keys_path.read_text(encoding="utf-8"))
        candidates = payload.get("keys", []) if isinstance(payload, dict) else payload
        if isinstance(candidates, list) and candidates:
            api_key = str(candidates[0]).strip()
    except Exception:
        pass
admin_key = values.get("ADMIN_KEY", "").strip() or api_key
print(api_key)
print(admin_key)
PY
)
API_KEY="${credentials[0]:-}"
ADMIN_KEY="${credentials[1]:-}"
if [[ -z "${API_KEY}" || -z "${ADMIN_KEY}" ]]; then
  printf 'smoke result=failed step=credentials reason=missing_key\n' >&2
  exit 1
fi

umask 077
printf 'header = "Authorization: Bearer %s"\n' "${API_KEY//\"/\\\"}" > "${api_config}"
printf 'header = "Authorization: Bearer %s"\n' "${ADMIN_KEY//\"/\\\"}" > "${admin_config}"

now_ms() {
  python3 - <<'PY'
import time
print(time.time_ns() // 1_000_000)
PY
}

response_kind() {
  python3 - "${body_file}" "${header_file}" <<'PY'
import json
from pathlib import Path
import sys

body = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
headers = Path(sys.argv[2]).read_text(encoding="utf-8", errors="replace").lower()
if "text/event-stream" in headers or body.lstrip().startswith("data:"):
    print("event_stream")
else:
    try:
        value = json.loads(body)
        if isinstance(value, dict):
            print("json:" + ",".join(sorted(value.keys())[:8]))
        elif isinstance(value, list):
            print("json:list")
        else:
            print("json:scalar")
    except Exception:
        print("text")
PY
}

status_allowed() {
  local status="$1"
  local allowed="$2"
  [[ ",${allowed}," == *",${status},"* ]]
}

REQUEST_STATUS="000"
REQUEST_DURATION_MS=0
REQUEST_KIND="transport_or_http_error"
SMOKE_TRANSIENT_RETRY_TOTAL=0

perform_request() {
  local name="$1"
  local allowed="$2"
  shift 2
  local started status duration kind
  started="$(now_ms)"
  : > "${error_file}"
  status="$(curl --silent --show-error --max-time "${SMOKE_TIMEOUT_SECONDS}" \
    --dump-header "${header_file}" --output "${body_file}" --write-out '%{http_code}' \
    "$@" 2>"${error_file}" || true)"
  duration="$(( $(now_ms) - started ))"
  REQUEST_STATUS="${status:-000}"
  REQUEST_DURATION_MS="${duration}"
  REQUEST_KIND="transport_or_http_error"
  if ! status_allowed "${status}" "${allowed}"; then
    return 1
  fi
  kind="$(response_kind)"
  REQUEST_KIND="${kind}"
}

request() {
  local name="$1" allowed="$2"
  shift 2
  if ! perform_request "${name}" "${allowed}" "$@"; then
    printf 'smoke result=failed step=%s status=%s duration_ms=%s response_type=%s\n' \
      "${name}" "${REQUEST_STATUS}" "${REQUEST_DURATION_MS}" "${REQUEST_KIND}" >&2
    return 1
  fi
  printf 'smoke result=passed step=%s status=%s duration_ms=%s response_type=%s\n' \
    "${name}" "${REQUEST_STATUS}" "${REQUEST_DURATION_MS}" "${REQUEST_KIND}"
}

retryable_request() {
  local name="$1" allowed="$2" retries="$3"
  shift 3
  local attempt=1
  while true; do
    if perform_request "${name}" "${allowed}" "$@"; then
      printf 'smoke result=passed step=%s status=%s duration_ms=%s response_type=%s attempts=%s\n' \
        "${name}" "${REQUEST_STATUS}" "${REQUEST_DURATION_MS}" "${REQUEST_KIND}" "${attempt}"
      return 0
    fi
    if [[ ! "${REQUEST_STATUS}" =~ ^(429|502|503|504)$ || "${attempt}" -gt "${retries}" ]]; then
      printf 'smoke result=failed step=%s status=%s duration_ms=%s response_type=%s attempts=%s\n' \
        "${name}" "${REQUEST_STATUS}" "${REQUEST_DURATION_MS}" "${REQUEST_KIND}" "${attempt}" >&2
      return 1
    fi
    SMOKE_TRANSIENT_RETRY_TOTAL=$((SMOKE_TRANSIENT_RETRY_TOTAL + 1))
    printf 'smoke result=retry step=%s status=%s duration_ms=%s attempt=%s\n' \
      "${name}" "${REQUEST_STATUS}" "${REQUEST_DURATION_MS}" "${attempt}"
    attempt=$((attempt + 1))
    sleep "${SMOKE_TRANSIENT_RETRY_DELAY_SECONDS}"
  done
}

json_field() {
  local field="$1"
  python3 - "${body_file}" "${field}" <<'PY'
import json
from pathlib import Path
import sys

value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for part in sys.argv[2].split("."):
    if isinstance(value, dict):
        value = value.get(part)
    else:
        value = None
        break
print("" if value is None else value)
PY
}

api_json() {
  local name="$1" path="$2" payload="$3" allowed="${4:-200}"
  request "${name}" "${allowed}" --config "${api_config}" \
    --header 'Content-Type: application/json' --request POST --data "${payload}" "${BASE_URL}${path}"
}

api_json_retryable() {
  local name="$1" path="$2" payload="$3" allowed="${4:-200}"
  retryable_request "${name}" "${allowed}" "${SMOKE_TRANSIENT_RETRIES}" --config "${api_config}" \
    --header 'Content-Type: application/json' --request POST --data "${payload}" "${BASE_URL}${path}"
}

request healthz "200" "${BASE_URL}/healthz"
request readyz "200" "${BASE_URL}/readyz"
request models "200" --config "${api_config}" "${BASE_URL}/v1/models"
if [[ "${SMOKE_MODE}" == "metrics" ]]; then
  request admin_runtime "200" --config "${admin_config}" "${BASE_URL}/api/admin/status"
  goroutines="$(json_field runtime.goroutines)"
  [[ "${goroutines}" =~ ^[0-9]+$ ]] || { printf 'smoke result=failed step=runtime_metrics reason=invalid_goroutines\n' >&2; exit 1; }
  printf 'smoke result=passed step=runtime_metrics status=200 duration_ms=0 response_type=json:aggregate goroutines=%s\n' "${goroutines}"
  exit 0
fi

api_json_retryable openai_non_stream "/v1/chat/completions" \
  "{\"model\":\"${SMOKE_MODEL}\",\"stream\":false,\"messages\":[{\"role\":\"user\",\"content\":\"Reply exactly SMOKE_OK\"}]}"
sleep "${SMOKE_CHAT_STAGGER_SECONDS}"
api_json_retryable openai_stream "/v1/chat/completions" \
  "{\"model\":\"${SMOKE_MODEL}\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"Reply exactly STREAM_OK\"}]}"
sleep "${SMOKE_CHAT_STAGGER_SECONDS}"
for index in $(seq 1 "${SMOKE_CHAT_SYNTHETIC_COUNT}"); do
  api_json_retryable "chat_synthetic_${index}" "/v1/chat/completions" \
    "{\"model\":\"${SMOKE_MODEL}\",\"stream\":false,\"messages\":[{\"role\":\"user\",\"content\":\"Reply exactly CHAT_${index}_OK\"}]}"
  sleep "${SMOKE_CHAT_STAGGER_SECONDS}"
done
if [[ "${SMOKE_MODE}" == "chat-only" ]]; then
  printf 'smoke result=passed step=chat_only status=200 duration_ms=0 response_type=summary transient_retries=%s\n' \
    "${SMOKE_TRANSIENT_RETRY_TOTAL}"
  exit 0
fi

api_json_retryable responses "/v1/responses" \
  "{\"model\":\"${SMOKE_MODEL}\",\"input\":\"Reply exactly RESPONSE_OK\"}"
response_id="$(json_field id)"
[[ -n "${response_id}" ]] || { printf 'smoke result=failed step=responses_id reason=missing_id\n' >&2; exit 1; }
api_json_retryable responses_previous "/v1/responses" \
  "{\"model\":\"${SMOKE_MODEL}\",\"previous_response_id\":\"${response_id}\",\"input\":\"Reply exactly RESPONSE_CONTINUE_OK\"}"

api_json_retryable anthropic_messages "/anthropic/v1/messages" \
  "{\"model\":\"claude-3-haiku\",\"max_tokens\":32,\"messages\":[{\"role\":\"user\",\"content\":\"Reply exactly ANTHROPIC_OK\"}]}"
api_json_retryable gemini_generate "/v1beta/models/gemini-2.5-flash:generateContent" \
  '{"contents":[{"role":"user","parts":[{"text":"Reply exactly GEMINI_OK"}]}]}'
api_json_retryable gemini_stream "/v1beta/models/gemini-2.5-flash:streamGenerateContent" \
  '{"contents":[{"role":"user","parts":[{"text":"Reply exactly GEMINI_STREAM_OK"}]}]}'

printf 'qwen2api candidate smoke file\n' > "${work_dir}/smoke.txt"
request file_upload "200" --config "${api_config}" --request POST \
  --form "file=@${work_dir}/smoke.txt;type=text/plain" "${BASE_URL}/v1/files"
file_id="$(json_field id)"
[[ -n "${file_id}" ]] || { printf 'smoke result=failed step=file_id reason=missing_id\n' >&2; exit 1; }
request file_delete "200" --config "${api_config}" --request DELETE "${BASE_URL}/v1/files/${file_id}"

api_json image_create "/v1/images/generations" \
  '{"model":"qwen-image","prompt":"A simple blue circle on white background","size":"1024x1024","async":true}' "202"
image_task_id="$(json_field id)"
[[ -n "${image_task_id}" ]] || { printf 'smoke result=failed step=image_task_id reason=missing_id\n' >&2; exit 1; }
request image_task "200" --config "${api_config}" "${BASE_URL}/v1/images/tasks/${image_task_id}"

for index in $(seq 1 "${SHM_VALIDATION_TASKS}"); do
  api_json "shm_image_create_${index}" "/v1/images/generations" \
    "{\"model\":\"qwen-image\",\"prompt\":\"Shared memory validation blue square ${index}\",\"size\":\"1024x1024\",\"async\":true}" "202"
  shm_task_id="$(json_field id)"
  [[ -n "${shm_task_id}" ]] || { printf 'smoke result=failed step=shm_task_id_%s reason=missing_id\n' "${index}" >&2; exit 1; }
  task_status=""
  for _ in $(seq 1 120); do
    request "shm_image_task_${index}" "200" --config "${api_config}" "${BASE_URL}/v1/images/tasks/${shm_task_id}"
    task_status="$(json_field status)"
    case "${task_status}" in
      completed|succeeded) break ;;
      failed|expired|cancelled) printf 'smoke result=failed step=shm_image_task_%s reason=terminal_status\n' "${index}" >&2; exit 1 ;;
    esac
    sleep 5
  done
  case "${task_status}" in
    completed|succeeded) ;;
    *) printf 'smoke result=failed step=shm_image_task_%s reason=timeout\n' "${index}" >&2; exit 1 ;;
  esac
done

api_json video_create "/v1/videos/generations" \
  '{"model":"qwen-video","prompt":"A blue circle slowly moving right","duration":1,"async":true}' "202"
video_task_id="$(json_field id)"
[[ -n "${video_task_id}" ]] || { printf 'smoke result=failed step=video_task_id reason=missing_id\n' >&2; exit 1; }
request video_task "200" --config "${api_config}" "${BASE_URL}/v1/videos/tasks/${video_task_id}"

request admin_refresh_status "200" --config "${admin_config}" "${BASE_URL}/api/admin/settings"
if [[ "${REFRESH_ASSERTIONS_REQUIRED}" == "true" ]]; then
  python3 - "${body_file}" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
status = payload.get("token_refresh_status", {})
required = {
    "phase", "fast_pending", "slow_scan_cursor_present", "attempted_total",
    "succeeded_total", "failed_total", "backoff_count", "breaker_state",
    "breaker_open_until", "last_batch_duration_ms", "last_batch_selected",
    "next_fast_run_at", "next_slow_run_at",
}
missing = sorted(required.difference(status))
if missing:
    raise SystemExit("missing refresh aggregate fields")
PY
  printf 'smoke result=passed step=admin_refresh_fields status=200 duration_ms=0 response_type=json:aggregate\n'

  self_test_started="$(now_ms)"
  if ! docker exec "${CONTAINER_NAME}" /usr/local/bin/qwen2api --token-refresh-self-test >"${body_file}" 2>"${error_file}"; then
    printf 'smoke result=failed step=refresh_controlled_failure status=1 duration_ms=%s response_type=process_error\n' "$(( $(now_ms) - self_test_started ))" >&2
    exit 1
  fi
  python3 - "${body_file}" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("status") != "ok" or payload.get("failed") != 5:
    raise SystemExit("refresh self-test counters invalid")
if not payload.get("breaker_open") or not payload.get("half_open_probe") or not payload.get("breaker_recovered"):
    raise SystemExit("refresh self-test breaker lifecycle invalid")
PY
  printf 'smoke result=passed step=refresh_controlled_failure status=0 duration_ms=%s response_type=json:self_test\n' "$(( $(now_ms) - self_test_started ))"
fi

printf 'smoke result=passed step=full_capability status=200 duration_ms=0 response_type=summary transient_retries=%s\n' \
  "${SMOKE_TRANSIENT_RETRY_TOTAL}"
