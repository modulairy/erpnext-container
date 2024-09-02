ARG PYTHON_VERSION=3.12.5
ARG DEBIAN_BASE=bookworm
FROM python:${PYTHON_VERSION}-slim-${DEBIAN_BASE} AS base

ARG WKHTMLTOPDF_VERSION=0.12.6.1-3
ARG WKHTMLTOPDF_DISTRO=bookworm
ARG NODE_VERSION=20.17.0
ENV NVM_DIR=/home/frappe/.nvm
ENV PATH=${NVM_DIR}/versions/node/v${NODE_VERSION}/bin/:${PATH}

RUN useradd -ms /bin/bash frappe \
    && apt-get update \
    && apt-get install --no-install-recommends -y \
    curl \
    git \
    vim \
    nginx \
    gettext-base \
    file \
    # weasyprint dependencies
    libpango-1.0-0 \
    libharfbuzz0b \
    libpangoft2-1.0-0 \
    libpangocairo-1.0-0 \
    # For backups
    restic \
    gpg \
    # MariaDB
    mariadb-client \
    less \
    # Postgres
    libpq-dev \
    postgresql-client \
    # For healthcheck
    wait-for-it \
    jq \
    # NodeJS
    && mkdir -p ${NVM_DIR} \
    && curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.5/install.sh | bash \
    && . ${NVM_DIR}/nvm.sh \
    && nvm install ${NODE_VERSION} \
    && nvm use v${NODE_VERSION} \
    && npm install -g yarn \
    && nvm alias default v${NODE_VERSION} \
    && rm -rf ${NVM_DIR}/.cache \
    && echo 'export NVM_DIR="/home/frappe/.nvm"' >>/home/frappe/.bashrc \
    && echo '[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"  # This loads nvm' >>/home/frappe/.bashrc \
    && echo '[ -s "$NVM_DIR/bash_completion" ] && \. "$NVM_DIR/bash_completion"  # This loads nvm bash_completion' >>/home/frappe/.bashrc \
    # Install wkhtmltopdf with patched qt
    && if [ "$(uname -m)" = "aarch64" ]; then export ARCH=arm64; fi \
    && if [ "$(uname -m)" = "x86_64" ]; then export ARCH=amd64; fi \
    && downloaded_file=wkhtmltox_${WKHTMLTOPDF_VERSION}.${WKHTMLTOPDF_DISTRO}_${ARCH}.deb \
    && curl -sLO https://github.com/wkhtmltopdf/packaging/releases/download/$WKHTMLTOPDF_VERSION/$downloaded_file \
    && apt-get install -y ./$downloaded_file \
    && rm $downloaded_file \
    # Clean up
    && rm -rf /var/lib/apt/lists/* \
    && rm -fr /etc/nginx/sites-enabled/default \
    && pip3 install frappe-bench \
    # Fixes for non-root nginx and logs to stdout
    && sed -i '/user www-data/d' /etc/nginx/nginx.conf \
    && ln -sf /dev/stdout /var/log/nginx/access.log && ln -sf /dev/stderr /var/log/nginx/error.log \
    && touch /run/nginx.pid \
    && chown -R frappe:frappe /etc/nginx/conf.d \
    && chown -R frappe:frappe /etc/nginx/nginx.conf \
    && chown -R frappe:frappe /var/log/nginx \
    && chown -R frappe:frappe /var/lib/nginx \
    && chown -R frappe:frappe /run/nginx.pid

RUN pip install --upgrade frappe-bench

COPY resources/nginx-template.conf /templates/nginx/frappe.conf.template
COPY resources/nginx-entrypoint.sh /usr/local/bin/nginx-entrypoint.sh

FROM base AS builder

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
    # For frappe framework
    wget \
    # For psycopg2
    libpq-dev \
    # Other
    libffi-dev \
    liblcms2-dev \
    libldap2-dev \
    libmariadb-dev \
    libsasl2-dev \
    libtiff5-dev \
    libwebp-dev \
    redis-tools \
    rlwrap \
    tk8.6-dev \
    cron \
    # For pandas
    gcc \
    build-essential \
    libbz2-dev \
    && rm -rf /var/lib/apt/lists/*

USER frappe

ARG FRAPPE_BRANCH=version-15
ARG FRAPPE_PATH=https://github.com/modulairy/frappe
RUN bench init \
  --frappe-branch=${FRAPPE_BRANCH} \
  --frappe-path=${FRAPPE_PATH} \
  --no-procfile \
  --no-backups \
  --skip-redis-config-generation \
  --verbose /home/frappe/frappe-bench

WORKDIR /home/frappe/frappe-bench  
RUN echo "{\"webserver_port\":443,\"socketio_port\":9000}" > sites/common_site_config.json

# ARG ERPNEXT_REPO=https://github.com/frappe/erpnext
# ARG ERPNEXT_BRANCH=version-15

RUN bench get-app --branch=v15.34.0 --resolve-deps erpnext https://github.com/frappe/erpnext
RUN bench get-app --branch v1.20.0 --resolve-deps crm https://github.com/frappe/crm
RUN bench get-app --branch $(git ls-remote --tags https://github.com/frappe/helpdesk.git | awk -F/ '{print $NF}' | sort -V | tail -n1) --resolve-deps helpdesk https://github.com/frappe/helpdesk


# RUN bench get-app --branch $(git ls-remote --tags https://github.com/frappe/hrms.git | awk -F/ '{print $NF}' | sort -V | tail -n1) --resolve-deps hrms https://github.com/frappe/hrms
# RUN bench get-app --branch $(git ls-remote --tags https://github.com/frappe/builder.git | awk -F/ '{print $NF}' | sort -V | tail -n1) --resolve-deps builder https://github.com/frappe/builder
# RUN bench get-app --branch $(git ls-remote --tags https://github.com/frappe/drive.git | awk -F/ '{print $NF}' | sort -V | tail -n1) --resolve-deps drive https://github.com/frappe/drive
# RUN bench get-app --branch $(git ls-remote --tags https://github.com/frappe/books.git | awk -F/ '{print $NF}' | sort -V | tail -n1) --resolve-deps books https://github.com/frappe/books
# RUN bench get-app --branch $(git ls-remote --tags https://github.com/frappe/lms.git | awk -F/ '{print $NF}' | sort -V | tail -n1) --resolve-deps lms https://github.com/frappe/lms
# RUN bench get-app --branch $(git ls-remote --tags https://github.com/frappe/wiki.git | awk -F/ '{print $NF}' | sort -V | tail -n1) --resolve-deps wiki https://github.com/frappe/wiki

RUN bench get-app hrms
RUN bench get-app drive
# RUN bench get-app books
RUN bench get-app lms
# RUN bench get-app insight
RUN bench get-app wiki
# RUN bench get-app drive

#   find apps -mindepth 1 -path "*/.git" | xargs rm -fr

FROM base AS erpnext

USER frappe

COPY --from=builder --chown=frappe:frappe /home/frappe/frappe-bench /home/frappe/frappe-bench

WORKDIR /home/frappe/frappe-bench

VOLUME [ \
  "/home/frappe/frappe-bench/sites", \
  "/home/frappe/frappe-bench/sites/assets", \
  "/home/frappe/frappe-bench/logs" \
]

CMD [ \
  "/home/frappe/frappe-bench/env/bin/gunicorn", \
  "--chdir=/home/frappe/frappe-bench/sites", \
  "--bind=0.0.0.0:8000", \
  "--threads=4", \
  "--workers=2", \
  "--worker-class=gthread", \
  "--worker-tmp-dir=/dev/shm", \
  "--timeout=120", \
  "--preload", \
  "frappe.app:application" \
]
