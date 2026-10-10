# Built from the same image as the standalone Docker install, so the add-on
# runs exactly the released code. The tag matches "version" in config.yaml.
ARG OPENSCAN_VERSION=0.3.0
FROM ghcr.io/parallaxintelligencepartnership/openscan-hub:${OPENSCAN_VERSION}

# Home Assistant mounts /data, /share and /media owned by root.
USER root

COPY run.sh /run.sh
RUN chmod 0755 /run.sh

LABEL io.hass.type="addon" \
      io.hass.arch="amd64|aarch64"

CMD ["/run.sh"]
