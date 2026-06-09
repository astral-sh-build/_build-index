#!/usr/bin/env bash

set -euo pipefail

input="${1:-dist}"
simple_root="${input%/}/simple"
aws_cli="${AWS_CLI:-aws}"
cache_control="public, max-age=60, stale-while-revalidate=300"
upload_concurrency="${R2_UPLOAD_CONCURRENCY:-16}"

: "${R2_BUCKET:?R2_BUCKET must be set}"
: "${R2_ENDPOINT:?R2_ENDPOINT must be set}"

if [[ ! "$upload_concurrency" =~ ^[1-9][0-9]*$ ]]; then
  echo "R2_UPLOAD_CONCURRENCY must be a positive integer" >&2
  exit 1
fi

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

  if [[ "$key" == "simple/" ]]; then
    stage=1
  elif [[ "$key" == simple/v1+json/* || "$key" == simple/v1+html/* ]]; then
    suffix="${key#simple/v1+*/}"
    [[ "${suffix%/}" == */* ]] && stage=3 || stage=2
  else
    suffix="${key#simple/}"
    [[ "${suffix%/}" == */* ]] && stage=3 || stage=2
  fi

  printf '%d\t%s\t%s\t%s\n' \
    "$stage" "$key" "$source" "$content_type" >>"$manifest"
done < <(find "$simple_root" -type f -print0)

if [[ ! -s "$manifest" ]]; then
  echo "refusing to publish an empty Simple API tree: $simple_root" >&2
  exit 1
fi

cut -f2 "$manifest" | LC_ALL=C sort -u >"$desired"

upload_document() {
  local key="$1"
  local source="$2"
  local content_type="$3"

  "$aws_cli" s3api put-object \
    --endpoint-url "$R2_ENDPOINT" \
    --bucket "$R2_BUCKET" \
    --key "$key" \
    --body "$source" \
    --content-type "$content_type" \
    --cache-control "$cache_control" \
    --no-cli-pager >/dev/null
  echo "uploaded s3://$R2_BUCKET/$key"
}

wait_for_uploads() {
  local status=0
  local pid
  for pid in "$@"; do
    wait "$pid" || status=1
  done
  return "$status"
}

# Publish project documents, then channel roots, then the service root.
for stage in 3 2 1; do
  pids=()
  while IFS=$'\t' read -r _stage key source content_type; do
    upload_document "$key" "$source" "$content_type" &
    pids+=("$!")
    if (( ${#pids[@]} >= upload_concurrency )); then
      wait_for_uploads "${pids[@]}"
      pids=()
    fi
  done < <(LC_ALL=C sort -t $'\t' -k2,2 "$manifest" | awk -F '\t' -v stage="$stage" '$1 == stage')
  wait_for_uploads "${pids[@]}"
done

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
