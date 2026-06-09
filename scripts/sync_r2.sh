#!/usr/bin/env bash

set -euo pipefail

input="${1:-dist}"
simple_root="${input%/}/simple"
aws_cli="${AWS_CLI:-aws}"
cache_control="public, max-age=60, stale-while-revalidate=300"

: "${R2_BUCKET:?R2_BUCKET must be set}"
: "${R2_ENDPOINT:?R2_ENDPOINT must be set}"

if [[ ! -d "$simple_root" ]]; then
  echo "missing generated Simple API tree: $simple_root" >&2
  exit 1
fi

if find "$simple_root" -type l -print -quit | grep -q .; then
  echo "refusing to publish symlinks from $simple_root" >&2
  exit 1
fi

unexpected="$(
  find "$simple_root" -type f \
    ! -name index.json \
    ! -name index.html \
    -print -quit
)"
if [[ -n "$unexpected" ]]; then
  echo "unexpected generated Simple API file: $unexpected" >&2
  exit 1
fi

temporary="$(mktemp -d)"
trap 'rm -rf "$temporary"' EXIT
manifest="$temporary/manifest.tsv"
desired="$temporary/desired.txt"
existing="$temporary/existing.txt"

while IFS= read -r -d '' source; do
  relative="${source#"${input%/}/"}"
  case "$source" in
    */index.json)
      key="${relative%index.json}"
      content_type="application/vnd.pypi.simple.v1+json"
      ;;
    */index.html)
      key="${relative%index.html}"
      content_type="application/vnd.pypi.simple.v1+html"
      ;;
  esac
  slashes="${key//[^\/]/}"
  printf '%d\t%s\t%s\t%s\n' \
    "${#slashes}" "$key" "$source" "$content_type" >>"$manifest"
done < <(find "$simple_root" -type f -print0)

if [[ ! -s "$manifest" ]]; then
  echo "refusing to publish an empty Simple API tree: $simple_root" >&2
  exit 1
fi

cut -f2 "$manifest" | LC_ALL=C sort -u >"$desired"

# Publish deeper project documents before their channel and service roots.
while IFS=$'\t' read -r _depth key source content_type; do
  "$aws_cli" s3api put-object \
    --endpoint-url "$R2_ENDPOINT" \
    --bucket "$R2_BUCKET" \
    --key "$key" \
    --body "$source" \
    --content-type "$content_type" \
    --cache-control "$cache_control" \
    --no-cli-pager >/dev/null
  echo "uploaded s3://$R2_BUCKET/$key"
done < <(LC_ALL=C sort -t $'\t' -k1,1nr -k2,2 "$manifest")

"$aws_cli" s3api list-objects-v2 \
  --endpoint-url "$R2_ENDPOINT" \
  --bucket "$R2_BUCKET" \
  --prefix simple/ \
  --query 'Contents[].Key' \
  --output json \
  --no-cli-pager |
  jq -r '.[]?' |
  LC_ALL=C sort -u >"$existing"

while IFS= read -r key; do
  [[ -n "$key" ]] || continue
  "$aws_cli" s3api delete-object \
    --endpoint-url "$R2_ENDPOINT" \
    --bucket "$R2_BUCKET" \
    --key "$key" \
    --no-cli-pager >/dev/null
  echo "deleted stale s3://$R2_BUCKET/$key"
done < <(comm -23 "$existing" "$desired")
