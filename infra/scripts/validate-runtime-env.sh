#!/usr/bin/env bash
set -euo pipefail

mode="${1:-collect}"
missing=()

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    missing+=("$name")
  fi
}

require_env RADAR_DATABASE_URL

case "$mode" in
  collect|digest)
    require_env RADAR_USER_AGENT
    require_env DASHSCOPE_API_KEY
    require_env SUPABASE_URL
    require_env SUPABASE_SECRET_KEY
    ;;
  delivery)
    if [[ "${DELIVERY_MODE:-shadow}" == "live" ]]; then
      require_env AGENTMAIL_API_KEY
      require_env AGENTMAIL_INBOX_ID
      require_env DIGEST_RECIPIENT
    fi
    ;;
  maintenance)
    require_env SUPABASE_URL
    require_env SUPABASE_SECRET_KEY
    ;;
  database)
    ;;
  *)
    echo "Unknown validation mode: $mode" >&2
    exit 2
    ;;
esac

if (( ${#missing[@]} > 0 )); then
  echo "Missing required environment variables: ${missing[*]}" >&2
  exit 1
fi
