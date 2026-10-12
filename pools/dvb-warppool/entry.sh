#!/bin/sh
set -eu
umask 077
printf 'rpc_user = "%s"\nrpc_pass = "%s"\n' "$OPENBENCH_RPC_USER" "$OPENBENCH_RPC_PASS" > /data/secrets.toml
exec dvb-warppool-daemon --config /config/config.toml --secrets /data/secrets.toml --data-dir /data --ui-dir /opt/dvb-warppool/ui
