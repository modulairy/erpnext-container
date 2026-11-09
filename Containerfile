# syntax=docker/dockerfile:1
ARG RUNTIME_BASE_IMAGE
ARG BUILDER_BASE_IMAGE
FROM ${RUNTIME_BASE_IMAGE} AS base
USER root
ARG BENCH_VERSION
COPY --from=resolved lock.json /opt/build/lock.json
RUN pip install --no-cache-dir "frappe-bench==${BENCH_VERSION}" \
    && python -m pip check
COPY --chmod=755 resources/nginx-entrypoint.sh resources/start.sh /usr/local/bin/
COPY resources/http_cache_config.py /usr/local/bin/
COPY resources/http_cache.lua /usr/local/share/lua/5.1/http_cache.lua
COPY --from=resolved redis.lua /usr/local/share/lua/5.1/resty/redis.lua
RUN python -c 'import hashlib,json; from pathlib import Path; assert hashlib.sha256(Path("/usr/local/share/lua/5.1/resty/redis.lua").read_bytes()).hexdigest() == json.load(open("/opt/build/lock.json"))["redis_lua"]["sha256"]'
COPY resources/nginx-template.conf /templates/nginx/frappe.conf.template
FROM ${BUILDER_BASE_IMAGE} AS dependencies
USER root
ARG BENCH_VERSION
COPY --from=resolved lock.json /opt/build/lock.json
RUN pip install --no-cache-dir "frappe-bench==${BENCH_VERSION}" \
    && python -m pip check
COPY scripts/apps.py scripts/build.py scripts/pdf_overlay.py scripts/community_overlay.py /opt/build/
COPY patches/ /opt/build/patches/
COPY resources/chrome_pdf_generator.py resources/pdf_resources.py /opt/build/pdf/
USER frappe
RUN python /opt/build/build.py install
FROM dependencies AS builder
RUN python /opt/build/build.py assets
FROM base AS runtime
ARG ERPNEXT_TAG
ARG SOURCE_REVISION
LABEL org.opencontainers.image.source="https://github.com/modulairy/erpnext-container" \
      org.opencontainers.image.version="${ERPNEXT_TAG}" \
      org.opencontainers.image.revision="${SOURCE_REVISION}"
COPY --from=builder --chown=frappe:frappe /home/frappe/frappe-bench /home/frappe/frappe-bench
USER root
RUN /usr/local/bin/python -m pip uninstall -y pip \
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
