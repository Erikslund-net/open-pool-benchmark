# The published dvb-WarpPool release image (multi-arch, amd64 + arm64) plus a
# start script: the daemon reads the RPC credentials from secrets.toml, so the
# script writes them there from the adapter's env before it starts.
# Source: https://git.warppool.org/dvb-projekt/dvb-WarpPool (AGPL-3.0).
ARG WARPPOOL_TAG=1.39.6
FROM git.warppool.org/dvb-projekt/dvb-warppool:${WARPPOOL_TAG}
USER root
COPY entry.sh /usr/local/bin/openbench-entry.sh
RUN chmod 0755 /usr/local/bin/openbench-entry.sh
USER warppool
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/openbench-entry.sh"]
