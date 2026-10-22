FROM ghcr.io/home-assistant/home-assistant:2026.10.2@sha256:33afb0659d786213264c2211f8d91c5fe87461c56eba6a3a546eb05fc71e89f4

LABEL org.opencontainers.image.title="Habab Home" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.description="Local Home Assistant Core distribution maintained by Habab"

# The custom integration is part of the downstream Core tree, so HA's normal
# integration loader imports it as product code rather than as a sidecar.
COPY src/habab_home /usr/src/homeassistant/homeassistant/components/habab_home
COPY config/configuration.yaml /usr/local/share/habab-home/configuration.yaml
COPY docker-entrypoint.sh /usr/local/bin/habab-home-entrypoint
COPY habab-ha.py /usr/local/bin/habab-ha
COPY install-xiaomi-home.py /usr/local/bin/habab-install-xiaomi-home
COPY xiaomi-home-release.json /usr/local/bin/xiaomi-home-release.json
RUN chmod 0755 /usr/local/bin/habab-home-entrypoint /usr/local/bin/habab-ha

ENTRYPOINT ["/usr/local/bin/habab-home-entrypoint"]
CMD ["/init"]
