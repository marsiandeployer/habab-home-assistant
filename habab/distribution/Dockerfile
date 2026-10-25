FROM ghcr.io/home-assistant/home-assistant:2026.10.2@sha256:33afb0659d786213264c2211f8d91c5fe87461c56eba6a3a546eb05fc71e89f4

LABEL org.opencontainers.image.title="Habab Home" \
      org.opencontainers.image.version="0.3.6" \
      org.opencontainers.image.description="Local Home Assistant Core distribution maintained by Habab"

RUN apk add --no-cache nodejs=24.22.1-r0

# Same upstream frontend version as Core, with the downstream Assist entry and
# native-form bridge. The installer verifies the complete public wheel first.
COPY install-native-frontend.py native-frontend-release.json /opt/habab-frontend/
RUN python3 /opt/habab-frontend/install-native-frontend.py

# The custom integration is part of the downstream Core tree, so HA's normal
# integration loader imports it as product code rather than as a sidecar.
COPY src/habab_home /usr/src/homeassistant/homeassistant/components/habab_home
COPY src/habab_telegram /usr/src/homeassistant/homeassistant/components/habab_telegram
COPY src/habab_yandex_speech /usr/src/homeassistant/homeassistant/components/habab_yandex_speech
COPY src/idigital_sms /usr/src/homeassistant/homeassistant/components/idigital_sms
COPY telegram-agent /opt/habab-telegram
COPY config/configuration.yaml /usr/local/share/habab-home/configuration.yaml
COPY docker-entrypoint.sh /usr/local/bin/habab-home-entrypoint
COPY habab-ha.py /usr/local/bin/habab-ha
COPY install-xiaomi-home.py /usr/local/bin/habab-install-xiaomi-home
COPY xiaomi-home-release.json /usr/local/bin/xiaomi-home-release.json
RUN mkdir -p /opt/habab-telegram/node_modules \
    && ln -s ../vendor/noxon-telegram /opt/habab-telegram/node_modules/noxon-telegram \
    && node --check /opt/habab-telegram/worker.mjs \
    && chmod 0755 /usr/local/bin/habab-home-entrypoint /usr/local/bin/habab-ha

ENTRYPOINT ["/usr/local/bin/habab-home-entrypoint"]
CMD ["/init"]

# Keep revision metadata in the final image-config layer so normal code layers
# remain cacheable; release builds must pass the full source commit explicitly.
ARG HABAB_SOURCE_REVISION=unknown
ARG HABAB_SOURCE_DIRTY=unknown
LABEL org.opencontainers.image.revision="${HABAB_SOURCE_REVISION}" \
      io.habab.source.dirty="${HABAB_SOURCE_DIRTY}"
