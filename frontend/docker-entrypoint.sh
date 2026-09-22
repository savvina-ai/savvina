#!/bin/sh
# Render nginx's server config from its template at startup, so a deployment
# can name its reverse proxy's address range without rebuilding the image.
set -euf

# TRUSTED_LB_CIDR (optional, comma/space-separated CIDRs): when a reverse proxy
# or load balancer fronts this container, trust X-Forwarded-For from those
# addresses so $remote_addr / X-Real-IP carry the true client IP instead of the
# proxy's — otherwise all clients share one backend rate-limit bucket. The
# character-class check below is an injection barrier, not address validation —
# a hex-only non-address such as "abc" passes it and then fails only in
# nginx's own config parse.
REAL_IP_CONFIG=""
if [ -n "${TRUSTED_LB_CIDR:-}" ]; then
    for cidr in $(echo "$TRUSTED_LB_CIDR" | tr ',' ' '); do
        case "$cidr" in
            *[!0-9a-fA-F.:/]*)
                echo "TRUSTED_LB_CIDR: '$cidr' is not an IPv4/IPv6 address or CIDR" >&2
                exit 1
                ;;
        esac
        REAL_IP_CONFIG="${REAL_IP_CONFIG}set_real_ip_from ${cidr}; "
    done
    REAL_IP_CONFIG="${REAL_IP_CONFIG}real_ip_header X-Forwarded-For; real_ip_recursive on;"
fi

# /etc/nginx/http.d is mode 777 so any UID can create a file here, but a
# restart under a different LOCAL_UID cannot truncate the previous UID's
# file — remove it first so re-renders stay idempotent across UID changes.
rm -f /etc/nginx/http.d/default.conf
sed -e "s|__REAL_IP_CONFIG__|${REAL_IP_CONFIG}|g" \
    /etc/nginx/templates/default.conf.template > /etc/nginx/http.d/default.conf

exec nginx -g "daemon off;" "$@"
