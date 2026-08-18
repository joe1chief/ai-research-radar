#!/usr/bin/env bash
set -euo pipefail

mode="${1:-collect}"
missing=()
invalid=()

DASHSCOPE_HOST="https://dashscope.aliyuncs.com/compatible-mode/v1"
YICLOUD_HOST="https://token-api.yicloud.com/v1"

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    missing+=("$name")
  fi
}

require_value() {
  local name="$1"
  local value="$2"
  if [[ -z "$value" ]]; then
    missing+=("$name")
  fi
}

validate_boolean() {
  local name="$1"
  local value="$2"
  if [[ -n "$value" && "$value" != "true" && "$value" != "false" ]]; then
    invalid+=("$name must be true or false")
  fi
}

validate_model_provider() {
  local provider="${LLM_PROVIDER:-dashscope}"
  local api_key=""
  local base_url=""
  local classifier_model=""
  local summarizer_model=""
  local embedding_mode="${LLM_EMBEDDING_MODE:-shared}"
  local embedding_model="${LLM_EMBEDDING_MODEL:-${QWEN_EMBEDDING_MODEL:-text-embedding-v4}}"
  local embedding_dimensions="${LLM_EMBEDDING_DIMENSIONS:-${QWEN_EMBEDDING_DIMENSIONS:-1024}}"
  local max_tokens="${LLM_MAX_TOKENS:-1200}"

  case "$provider" in
    dashscope)
      # Legacy DashScope variables remain accepted. They are never considered
      # when YiCloud is selected, so a YiCloud credential cannot silently fall
      # through to the DashScope host (or vice versa in Actions).
      api_key="${LLM_API_KEY:-${DASHSCOPE_API_KEY:-}}"
      base_url="${LLM_BASE_URL:-${DASHSCOPE_BASE_URL:-$DASHSCOPE_HOST}}"
      classifier_model="${LLM_CLASSIFIER_MODEL:-${QWEN_CLASSIFIER_MODEL:-qwen-flash}}"
      summarizer_model="${LLM_SUMMARIZER_MODEL:-${QWEN_SUMMARIZER_MODEL:-qwen-plus}}"
      if [[ "${base_url%/}" != "$DASHSCOPE_HOST" ]]; then
        invalid+=("LLM_BASE_URL must be $DASHSCOPE_HOST for dashscope")
      fi
      ;;
    yicloud)
      api_key="${LLM_API_KEY:-}"
      base_url="${LLM_BASE_URL:-}"
      classifier_model="${LLM_CLASSIFIER_MODEL:-}"
      summarizer_model="${LLM_SUMMARIZER_MODEL:-}"
      if [[ -n "${DASHSCOPE_API_KEY:-}" ]]; then
        invalid+=("DASHSCOPE_API_KEY must be unset when LLM_PROVIDER=yicloud")
      fi
      if [[ -n "${DASHSCOPE_BASE_URL:-}" ]]; then
        invalid+=("DASHSCOPE_BASE_URL must be unset when LLM_PROVIDER=yicloud")
      fi
      require_value LLM_BASE_URL "$base_url"
      if [[ -n "$base_url" && "${base_url%/}" != "$YICLOUD_HOST" ]]; then
        invalid+=("LLM_BASE_URL must be $YICLOUD_HOST for yicloud")
      fi
      if [[ "$embedding_mode" != "local" ]]; then
        invalid+=(
          "LLM_EMBEDDING_MODE must be local for yicloud until its embedding API is validated"
        )
      fi
      if [[ "$classifier_model" == required-yicloud-* ]]; then
        invalid+=("YICLOUD_CLASSIFIER_MODEL or an explicit LLM_CLASSIFIER_MODEL is required")
      fi
      if [[ "$summarizer_model" == required-yicloud-* ]]; then
        invalid+=("YICLOUD_SUMMARIZER_MODEL or an explicit LLM_SUMMARIZER_MODEL is required")
      fi
      ;;
    *)
      invalid+=("LLM_PROVIDER must be dashscope or yicloud")
      return
      ;;
  esac

  require_value LLM_API_KEY "$api_key"
  require_value LLM_CLASSIFIER_MODEL "$classifier_model"
  require_value LLM_SUMMARIZER_MODEL "$summarizer_model"

  validate_boolean LLM_JSON_RESPONSE_FORMAT "${LLM_JSON_RESPONSE_FORMAT:-true}"
  validate_boolean LLM_ENABLE_THINKING "${LLM_ENABLE_THINKING:-}"
  if [[ ! "$max_tokens" =~ ^[0-9]+$ ]]; then
    invalid+=("LLM_MAX_TOKENS must be an integer from 64 through 4096")
  elif (( 10#$max_tokens < 64 || 10#$max_tokens > 4096 )); then
    invalid+=("LLM_MAX_TOKENS must be an integer from 64 through 4096")
  fi

  case "$embedding_mode" in
    local)
      ;;
    shared)
      require_value LLM_EMBEDDING_MODEL "$embedding_model"
      ;;
    remote)
      require_env LLM_EMBEDDING_API_KEY
      require_env LLM_EMBEDDING_BASE_URL
      require_value LLM_EMBEDDING_MODEL "$embedding_model"
      if [[ -n "${LLM_EMBEDDING_BASE_URL:-}" && "${LLM_EMBEDDING_BASE_URL}" != https://* ]]; then
        invalid+=("LLM_EMBEDDING_BASE_URL must use https")
      fi
      ;;
    *)
      invalid+=("LLM_EMBEDDING_MODE must be shared, remote, or local")
      ;;
  esac

  if [[ "$embedding_dimensions" != "1024" ]]; then
    invalid+=("LLM_EMBEDDING_DIMENSIONS must be 1024")
  fi
}

case "$mode" in
  collect|digest)
    require_env RADAR_DATABASE_URL
    require_env RADAR_USER_AGENT
    require_env SUPABASE_URL
    require_env SUPABASE_SECRET_KEY
    validate_model_provider
    ;;
  delivery)
    require_env RADAR_DATABASE_URL
    if [[ "${DELIVERY_MODE:-shadow}" == "live" ]]; then
      require_env AGENTMAIL_API_KEY
      require_env AGENTMAIL_INBOX_ID
      require_env DIGEST_RECIPIENT
    fi
    ;;
  maintenance)
    require_env RADAR_DATABASE_URL
    require_env SUPABASE_URL
    require_env SUPABASE_SECRET_KEY
    ;;
  database)
    require_env RADAR_DATABASE_URL
    ;;
  model)
    validate_model_provider
    ;;
  *)
    echo "Unknown validation mode: $mode" >&2
    exit 2
    ;;
esac

if (( ${#missing[@]} > 0 )); then
  echo "Missing required environment variables: ${missing[*]}" >&2
fi

if (( ${#invalid[@]} > 0 )); then
  printf 'Invalid runtime configuration: %s\n' "${invalid[@]}" >&2
fi

if (( ${#missing[@]} > 0 || ${#invalid[@]} > 0 )); then
  exit 1
fi
