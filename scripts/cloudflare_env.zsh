# Source this on the Mac mini before running wrangler from non-interactive jobs.
# It exports the existing Global API Key file without printing any secret value.
if [ -z "${CLOUDFLARE_API_TOKEN:-}" ] && [ -z "${CLOUDFLARE_API_KEY:-}" ] && [ -f "$HOME/.cloudflared/cf-global-api-key.json" ]; then
  export CLOUDFLARE_EMAIL="$(jq -r '.email // empty' "$HOME/.cloudflared/cf-global-api-key.json" 2>/dev/null)"
  export CLOUDFLARE_API_KEY="$(jq -r '.global_api_key // empty' "$HOME/.cloudflared/cf-global-api-key.json" 2>/dev/null)"
  export CLOUDFLARE_ACCOUNT_ID="$(jq -r '.account_id // empty' "$HOME/.cloudflared/cf-global-api-key.json" 2>/dev/null)"
fi
