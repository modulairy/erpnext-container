# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE
ARG NODE_IMAGE
FROM ${NODE_IMAGE} AS node
FROM ${PYTHON_IMAGE} AS base
ARG BENCH_VERSION
ARG YARN_VERSION
ENV PATH=/home/frappe/frappe-bench/env/bin:/usr/local/bin:${PATH} \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=resolved wkhtmltox.deb /tmp/wkhtmltox.deb
COPY --from=resolved lock.json /opt/build/lock.json
RUN python -c 'import hashlib,json; from pathlib import Path; assert hashlib.sha256(Path("/tmp/wkhtmltox.deb").read_bytes()).hexdigest() == json.loads(Path("/opt/build/lock.json").read_text())["wkhtmltopdf"]["sha256"]' \
    && useradd -m -u 1000 -s /bin/bash frappe \
    && apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends ca-certificates curl git nginx gettext-base \
       libpango-1.0-0 libharfbuzz0b libpangoft2-1.0-0 libpangocairo-1.0-0 \
       mariadb-client libmariadb3 libpq5 libmagic1 media-types tzdata-legacy \
       chromium-headless-shell /tmp/wkhtmltox.deb \
    && rm -rf /var/lib/apt/lists/* /tmp/wkhtmltox.deb /etc/nginx/sites-enabled/default \
    && pip install --no-cache-dir "frappe-bench==${BENCH_VERSION}" \
    && python -c 'import json,subprocess; p=json.load(open("/opt/build/lock.json"))["security_overrides"]["python"]; subprocess.run(["python","-m","pip","install","--upgrade"]+[k+"=="+p[k] for k in ("pip","setuptools","urllib3")],check=True)' \
    && sed -i '/user www-data/d' /etc/nginx/nginx.conf \
    && ln -sf /dev/stdout /var/log/nginx/access.log \
    && ln -sf /dev/stderr /var/log/nginx/error.log \
    && touch /run/nginx.pid \
    && chown -R frappe:frappe /etc/nginx /var/log/nginx /var/lib/nginx /run/nginx.pid
COPY --chmod=755 resources/nginx-entrypoint.sh resources/start.sh /usr/local/bin/
COPY resources/nginx-template.conf /templates/nginx/frappe.conf.template
FROM base AS builder
USER root
ARG YARN_VERSION
RUN pip install --no-cache-dir packaging==26.3
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install --global "yarn@${YARN_VERSION}"
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential pkg-config libpq-dev \
       libffi-dev liblcms2-dev libldap2-dev libmariadb-dev libsasl2-dev libtiff-dev \
       libwebp-dev libbz2-dev libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev librsvg2-dev \
    && rm -rf /var/lib/apt/lists/*
COPY scripts/build.py scripts/security.py /opt/build/
USER frappe
RUN python /opt/build/build.py
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
