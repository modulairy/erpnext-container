# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE
ARG NODE_IMAGE
FROM ${NODE_IMAGE} AS node
FROM ${PYTHON_IMAGE} AS os-backports
COPY --from=resolved lock.json /opt/build/lock.json
COPY scripts/os_backports.py /opt/build/os_backports.py
RUN python /opt/build/os_backports.py
FROM ${PYTHON_IMAGE} AS base
ARG BENCH_VERSION
ARG YARN_VERSION
ENV PATH=/home/frappe/frappe-bench/env/bin:/usr/local/bin:${PATH} \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=resolved lock.json /opt/build/lock.json
COPY --from=os-backports /backports /opt/os-backports
RUN useradd -m -u 1000 -s /bin/bash frappe \
    && apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends ca-certificates curl git nginx gettext-base \
       libpango-1.0-0 libharfbuzz0b libpangoft2-1.0-0 libpangocairo-1.0-0 \
       mariadb-client libmariadb3 libpq5 libmagic1 media-types tzdata-legacy \
       chromium-headless-shell fonts-dejavu-core libnginx-mod-http-lua libnginx-mod-http-srcache-filter lua-cjson \
    && apt-get install -y --no-remove --no-install-recommends /opt/os-backports/*.deb \
    && dpkg --purge libperl5.40 perl-modules-5.40 \
    && rm -f /opt/os-backports/*.deb \
    && rm -rf /var/lib/apt/lists/* /etc/nginx/sites-enabled/default \
    && pip install --no-cache-dir "frappe-bench==${BENCH_VERSION}" \
    && python -c 'import json,subprocess; p=json.load(open("/opt/build/lock.json"))["security_overrides"]["python"]; subprocess.run(["python","-m","pip","install","--upgrade"]+[k+"=="+p[k] for k in ("pip","setuptools","urllib3")],check=True)' \
    && python -m pip check \
    && sed -i '/user www-data/d' /etc/nginx/nginx.conf \
    && ln -sf /dev/stdout /var/log/nginx/access.log \
    && ln -sf /dev/stderr /var/log/nginx/error.log \
    && touch /run/nginx.pid \
    && chown -R frappe:frappe /etc/nginx /var/log/nginx /var/lib/nginx /run/nginx.pid
COPY --from=resolved mariadb-keyring.asc /etc/apt/keyrings/mariadb-keyring.asc
RUN python -c 'import hashlib,json,re,subprocess; from pathlib import Path; p=json.load(open("/opt/build/lock.json"))["mariadb_client"]; assert p["repository"] == "https://deb.mariadb.org/11.8/debian"; assert re.fullmatch(r"1:11\.8\.\d+\+maria~deb13",p["version"]); assert hashlib.sha256(Path("/etc/apt/keyrings/mariadb-keyring.asc").read_bytes()).hexdigest() == p["key_sha256"]; Path("/etc/apt/sources.list.d/mariadb.list").write_text("deb [signed-by=/etc/apt/keyrings/mariadb-keyring.asc] "+p["repository"]+" trixie main\n"); subprocess.run(["apt-get","update"],check=True); subprocess.run(["apt-get","install","-y","--no-install-recommends"]+[name+"="+p["version"] for name in ("mariadb-client","mariadb-client-core","mariadb-common","libmariadb3")],check=True)' \
    && rm -rf /var/lib/apt/lists/*
RUN python -c 'import json,re,subprocess; from pathlib import Path; v=json.load(open("/opt/build/lock.json"))["curl_backport"]; assert re.fullmatch(r"\d+\.\d+\.\d+-\d+~bpo13\+\d+(?:\+b\d+)?",v); Path("/etc/apt/sources.list.d/backports.list").write_text("deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://deb.debian.org/debian trixie-backports main\n"); subprocess.run(["apt-get","update"],check=True); subprocess.run(["apt-get","install","-y","--no-install-recommends","-t","trixie-backports"]+[name+"="+v for name in ("curl","libcurl3t64-gnutls","libcurl4-gnutls","libcurl4t64")],check=True)' \
    && rm -rf /var/lib/apt/lists/*
COPY --chmod=755 resources/nginx-entrypoint.sh resources/start.sh /usr/local/bin/
COPY resources/http_cache_config.py /usr/local/bin/
COPY resources/http_cache.lua /usr/local/share/lua/5.1/http_cache.lua
COPY --from=resolved redis.lua /usr/local/share/lua/5.1/resty/redis.lua
RUN python -c 'import hashlib,json; from pathlib import Path; assert hashlib.sha256(Path("/usr/local/share/lua/5.1/resty/redis.lua").read_bytes()).hexdigest() == json.load(open("/opt/build/lock.json"))["redis_lua"]["sha256"]'
COPY resources/nginx-template.conf /templates/nginx/frappe.conf.template
FROM base AS dependencies
USER root
ARG YARN_VERSION
COPY --from=os-backports /build-deps /opt/os-build-deps
RUN pip install --no-cache-dir packaging==26.3
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install --global "yarn@${YARN_VERSION}"
RUN apt-get update \
    && apt-get install -y --no-remove --no-install-recommends /opt/os-build-deps/*.deb build-essential pkg-config libpq-dev \
       libffi-dev liblcms2-dev libldap2-dev libsasl2-dev libtiff-dev \
       libwebp-dev libbz2-dev libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev librsvg2-dev \
    && python -c 'import json,subprocess; p=json.load(open("/opt/build/lock.json"))["mariadb_client"]; subprocess.run(["apt-get","install","-y","--no-install-recommends","libmariadb-dev="+p["version"]],check=True)' \
    && rm -rf /var/lib/apt/lists/* /opt/os-build-deps
COPY scripts/build.py scripts/security.py scripts/pdf_overlay.py /opt/build/
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
