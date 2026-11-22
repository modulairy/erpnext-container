# syntax=docker/dockerfile:1
ARG RUNTIME_BASE_IMAGE=frappe/base:version-16@sha256:b8fcea788326f88d95646fcf89f3b8311bcb00944b1adeb7936603bb58301a12
ARG BUILDER_BASE_IMAGE=frappe/build:version-16@sha256:0ea1392506828b8deec87c82788ca8936c3203848ecb93901194a5b506a7be46
FROM ${RUNTIME_BASE_IMAGE} AS base
USER root
COPY --chmod=755 resources/start.sh /usr/local/bin/
FROM ${BUILDER_BASE_IMAGE} AS dependencies
USER root
COPY --from=resolved lock.json /opt/build/lock.json
COPY scripts/build.py scripts/security.py scripts/pdf_overlay.py scripts/community_overlay.py /opt/build/
COPY patches/ /opt/build/patches/
COPY resources/chrome_pdf_generator.py resources/pdf_resources.py /opt/build/pdf/
USER frappe
RUN python -m venv /tmp/erpnext-build-env \
    && /tmp/erpnext-build-env/bin/python -m pip install --no-cache-dir --no-deps packaging==26.3
RUN /tmp/erpnext-build-env/bin/python /opt/build/build.py install
FROM dependencies AS builder
RUN /tmp/erpnext-build-env/bin/python /opt/build/build.py assets
FROM base AS runtime
ARG ERPNEXT_TAG
ARG SOURCE_REVISION
LABEL org.opencontainers.image.source="https://github.com/modulairy/erpnext-container" \
      org.opencontainers.image.version="${ERPNEXT_TAG}" \
      org.opencontainers.image.revision="${SOURCE_REVISION}"
COPY --from=builder --chown=frappe:0 /home/frappe/frappe-bench /home/frappe/frappe-bench
USER root
RUN node_bin="$(dirname "$(command -v node)")" \
    && rm -rf "$node_bin/../lib/node_modules" \
    && rm -f "$node_bin/npm" "$node_bin/npx" "$node_bin/yarn" "$node_bin/yarnpkg" "$node_bin/corepack" "$node_bin/pnpm" "$node_bin/pnpx" \
    && /usr/local/bin/python -m pip uninstall -y pip \
    && /home/frappe/frappe-bench/env/bin/python -m pip uninstall -y pip
USER frappe
WORKDIR /home/frappe/frappe-bench
RUN test -x /usr/bin/chromium-headless-shell \
    && mkdir -p chromium/chrome-linux \
    && ln -s /usr/bin/chromium-headless-shell chromium/chrome-linux/headless_shell
VOLUME ["/home/frappe/frappe-bench/sites", "/home/frappe/frappe-bench/logs"]
EXPOSE 8000 8080 9000
HEALTHCHECK NONE
ENTRYPOINT ["/usr/local/bin/start.sh"]
CMD ["backend"]
