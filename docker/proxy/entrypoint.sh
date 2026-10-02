#!/bin/sh
set -eu

if [ -z "${SEBAS_PROXY_TOKEN:-}" ]; then
  echo "SEBAS_PROXY_TOKEN is required" >&2
  exit 1
fi
if [ "${#SEBAS_PROXY_TOKEN}" -lt 16 ]; then
  echo "SEBAS_PROXY_TOKEN must be at least 16 characters" >&2
  exit 1
fi
case "$SEBAS_PROXY_TOKEN" in
  *[!A-Za-z0-9._~+=-]*)
    echo "SEBAS_PROXY_TOKEN contains unsupported characters" >&2
    exit 1
    ;;
esac

case "${SEBAS_ALLOWED_CLIENT:-}" in
  *";"*|*"{"*|*"}"*|*"\n"*|*"\r"*)
    echo "SEBAS_ALLOWED_CLIENT contains invalid characters" >&2
    exit 1
    ;;
esac
if [ -n "${SEBAS_ALLOWED_CLIENT:-}" ]; then
  case "$SEBAS_ALLOWED_CLIENT" in
    *[!0-9A-Fa-f:./]*) echo "SEBAS_ALLOWED_CLIENT must be an IP address or CIDR" >&2; exit 1 ;;
  esac
  export SEBAS_CLIENT_ACCESS_RULES="allow 127.0.0.1; allow ${SEBAS_ALLOWED_CLIENT}; deny all;"
else
  export SEBAS_CLIENT_ACCESS_RULES=""
fi

envsubst '${SEBAS_PROXY_TOKEN} ${SEBAS_CLIENT_ACCESS_RULES}' \
  < /etc/nginx/templates/sebas.conf.template \
  > /etc/nginx/nginx.conf
exec nginx -g 'daemon off;'
