# rclone instellen voor Cloudflare R2 (remote "r2"), uit de secrets R2_ACCOUNT_ID, R2_ACCESS_KEY_ID en
# R2_SECRET_ACCESS_KEY (als omgevingsvariabelen). Gebruik in een stap van de workflow:  source scripts/r2-env.sh
# Spaties en regeleinden rond de secrets worden weggehaald (komen makkelijk mee bij het plakken).

trim() { printf '%s' "$1" | tr -d '[:space:]'; }
for v in R2_ACCOUNT_ID R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY; do
  test -n "$(trim "${!v}")" || { echo "::error::Secret $v ontbreekt of is leeg"; exit 1; }
done
export RCLONE_CONFIG_R2_TYPE=s3
export RCLONE_CONFIG_R2_PROVIDER=Cloudflare
export RCLONE_CONFIG_R2_ACL=private
export RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true
export RCLONE_CONFIG_R2_ACCESS_KEY_ID="$(trim "$R2_ACCESS_KEY_ID")"
export RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$(trim "$R2_SECRET_ACCESS_KEY")"
export RCLONE_CONFIG_R2_ENDPOINT="https://$(trim "$R2_ACCOUNT_ID").r2.cloudflarestorage.com"

# Tegels opladen: alleen gewijzigde (--checksum = MD5 vergelijken), niets wissen.
# application/gzip zonder Content-Encoding: de app pakt de tegel zelf uit.
upload_tiles() {  # upload_tiles <map> <pad in de bucket>
  rclone copy "$1" "r2:$BUCKET/$2" --checksum --fast-list \
    --transfers 32 --checkers 32 --low-level-retries 3 \
    --header-upload "Content-Type: application/gzip" \
    --header-upload "Cache-Control: public, max-age=3600" \
    --stats 60s --stats-one-line --stats-log-level NOTICE
}
