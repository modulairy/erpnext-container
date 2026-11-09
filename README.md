# Community ERPNext container

Two manual Actions build one complete application image on a separately prepared OS base.

| Action | Purpose |
| --- | --- |
| **ERPNext base images** | Build and scan the OS bases; on main, select their approved immutable digests. |
| **Community ERPNext** | Use the approved bases, install all applications, test and scan. Enable `publish` on main to publish GHCR, tag and Release. |

No push, PR, release or scheduled build triggers are configured. Application builds never invoke or rebuild the base. Previously published Frappe-only images remain available, but this repository no longer builds that separate profile.

## Base and applications

`Containerfile.base` has runtime and builder targets in `ghcr.io/modulairy/erpnext-base`. Both stop before Bench. Runtime includes Python, Node, Chrome, Nginx and system dependencies. Builder adds npm, Yarn and build libraries; these tools stay out of the final runtime. The manual base Action uses Docker layer caching, validates both targets with the shared security gate, then updates only `resources/base-images.json` on main. Concurrent main changes reject this update. There is no separate maintenance or verify-only mode.

`Containerfile` installs Bench and the application set declared in `scripts/apps.py`: Frappe, ERPNext, Payments, Telephony, Helpdesk, Builder, Webshop, CRM, Wiki and Print Designer. The resolver selects compatible stable releases within Frappe 16 and records exact commits. Payments/Webshop use `version-16` and Telephony uses `develop` snapshots because they lack published releases. Unknown dependencies or an incompatible set stop the build. An incompatible base runtime requires a separate manual base update.

The published image is `ghcr.io/modulairy/erpnext:<ERPNext-version>`, with no `latest` image tag. Rebuilds may update that version tag; deploy using the reported digest. Git source tags remain immutable: another source revision for the same version uses `<version>-<short-commit>`. The Release records every application commit, Bench version and image digest. `image-lock.json` inside the image and Actions artifacts contain the full selection.

## Validation and security

Every application build installs/migrates all apps on disposable sites and tests login/API CRUD, assets, queues, realtime, PDFs and HTTP cache. MariaDB and Redis are external test services. The publish job receives the exact tested image and checks its image ID; it does not rebuild. SBOM and provenance attestations are published with its digest. Reports are retained for 14 days.

Base and application images share HIGH/CRITICAL and secret/configuration scanning rules. Scanner errors and new/unmatched findings block publication. Exact accepted OS risks, verified source fixes and the original builder's 68 scoped advisories remain separately reported; they are not blanket exclusions. External MariaDB findings are advisory; Redis findings and secrets block. Passing does not mean there are no known vulnerabilities.

Application dependency declarations and upstream lockfiles are preserved. No Python/npm version overrides, Yarn lock regeneration or Helpdesk Zod adapter removal is applied. Python transitive dependencies follow upstream constraints; where upstream supplies a lockfile it is retained. Known vulnerabilities may consequently block the existing security gate; this change adds no exceptions. Reviewed Debian source builds, CPython XML-module fixes, pinned Chrome with a positive scanner control and source-integrity checks remain. Feature patches fail closed on upstream source drift. Raw findings and evidence remain available. npm/Yarn, pip and asset compilers are removed from the application runtime as before; libraries serving assets remain.

## Reviewed Frappe 16 backports

`patches/manifest.json` records the source revisions, input/output file hashes and patch checksums for the opt-in [Frappe symlink discovery change](https://github.com/frappe/frappe/pull/44199) and [Helpdesk Redis ticket search](https://github.com/frappe/helpdesk/pull/3914). These are container backports, not upstream-merged features. They are reviewed for Frappe 16.55.2 and Helpdesk 16.1.0. Resolution/build fails on an unreviewed version or changed source instead of omitting a patch. Applied provenance is recorded in `image-lock.json`.

Symlink site discovery is disabled by default. Set `allow_symlink_sites: true` in `common_site_config.json` or set `FRAPPE_ALLOW_SYMLINK_SITES=true`; ENV wins, including explicit false. Only configured directory targets are included. Backend, scheduler, workers and frontend must share the same readable target paths. Manage site directories and links as trusted operator configuration; aliases of one database are not separate tenants. HTTP cache discovery honors the same opt-in setting; restart frontend after configuration or link changes.

Ticket search defaults to SQLite. To opt into Redis, use the site's existing `redis_cache` with Redis 8 Search supporting `FT.AGGREGATE ADDSCORES` and cursors:

```sh
bench --site example.com set-config helpdesk_search_backend redis
bench --site example.com execute helpdesk.ticket_search.build_index
```

Keep the scheduler and long workers running. Indexing happens after database commit; reads can briefly lag writes. Missing indexes are rebuilt through scheduled recovery. Force rebuilding temporarily makes ticket search unavailable. Ordinary site cache clearing preserves search keys; a full Redis flush removes them and requires rebuilding. Redis ranking/prefix matching differs from SQLite and has no SQLite spelling correction. Capacity, persistence and eviction must account for index data. Article search retains its existing implementation. Return to SQLite by changing the setting to `sqlite` and rebuilding on storage with SQLite-compatible locking; existing SQLite data is not deleted automatically. Frappe 16 uses its supported bounded background retry mechanism rather than the develop-only `rq_retry` argument.

## Runtime

One image supports separate `backend` (default), `frontend`, `websocket`, `scheduler`, `worker-short`, `worker-default` and `worker-long` containers. Other commands are passed through, for example `bench --site example.com migrate`. Run as the included non-root `frappe` user. Use persistent volumes for `/home/frappe/frappe-bench/sites` and `/home/frappe/frappe-bench/logs`; do not mount a separate assets volume, because image assets must match application code. All service roles must use the same image digest.

Classic prints, report exports and Print Designer use the version/hash-pinned official Chrome headless shell. The image retains the upstream pdfkit dependency, but PDF rendering uses Chrome; wkhtmltopdf is not installed. PDF jobs run as `frappe`, use private temporary directories under `/tmp/frappe-pdf`, and terminate after 90 seconds. Generated invoice/report PDFs and renderer sandbox evidence are retained in the Actions reports.

Containers that render PDFs require Linux user namespaces and the supplied `resources/chromium-seccomp.json` profile, with `cap_drop: [ALL]`, `no-new-privileges:true`, and `init: true` (see `tests/compose.yaml`). The profile derives from [Playwright's Docker profile](https://github.com/microsoft/playwright/blob/v1.64.0/utils/docker/seccomp_profile.json), adding `chroot` so Chromium can isolate itself inside its user namespace without host capabilities. Configure the profile on the Docker host; it cannot be enabled from inside the image. Hosts that prohibit unprivileged user namespaces must receive a narrowly scoped administrator policy change before running this image. The renderer fails closed; do not disable the sandbox, use privileged containers or grant `SYS_ADMIN`.

PDF HTML cannot run document JavaScript or fetch external resources. Store print logos, fonts and styles in permitted site/app assets; private files retain Frappe permission checks. Custom formats depending on external URLs or document scripts need adaptation. Browser CDP listens only on loopback and is never exposed as a service.

Helpdesk search requires Redis Search commands. The test harness uses Redis 8 with its integrated Search module; production Redis must provide the same capability.

MariaDB/Redis are external services. Create a site and configure `db_host`, `redis_cache`, `redis_queue` and `redis_socketio` in the shared site configuration before starting application roles. Set `webserver_host` to the backend service and `webserver_port` to 8000 so realtime authentication can reach it. The frontend accepts `BACKEND` (default `backend:8000`), `SOCKETIO` (default `websocket:9000`) and `FRAPPE_SITE_NAME_HEADER` (default HTTP host). Internal ports are 8000, 8080 and 9000. Supply deployment-specific health checks for each role; a single image health check cannot describe all roles. `tests/compose.yaml` is a disposable test harness, not a production deployment.

## Redis HTTP cache

Set `HTTP_REDIS_CACHE_ACTIVE=true` on the **frontend** role to enable Redis-backed HTTP caching. It is disabled by default. The connection is selected per known site: `HTTP_REDIS_CACHE_URL`, then the site's `redis_cache`, then `common_site_config.json`'s `redis_cache`. Site directories and configuration are read at frontend startup; restart the frontend after changing them. Redis passwords are kept in a mode-0600 runtime file and are not written into Nginx directives or logs. `rediss://` requires a certificate trusted by the system CA bundle; certificate verification cannot be disabled.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `HTTP_REDIS_CACHE_ACTIVE` | `false` | Enable HTTP caching |
| `HTTP_REDIS_CACHE_URL` | site/global configuration | Optional `redis://` or `rediss://` connection, including ACL credentials/database |
| `HTTP_REDIS_CACHE_STATIC_TTL` | `86400` | Public static file lifetime, seconds (maximum 30 days) |
| `HTTP_REDIS_CACHE_PUBLIC_TTL` | `60` | Maximum anonymous public response lifetime, seconds (maximum 1 hour) |
| `HTTP_REDIS_CACHE_MAX_BYTES` | `16777216` | Maximum cached response size (maximum 64 MiB) |
| `HTTP_REDIS_CACHE_TIMEOUT_MS` | `200` | Redis operation timeout |
| `HTTP_REDIS_CACHE_POOL_SIZE` | `10` | Redis connections per worker/site |
| `HTTP_REDIS_CACHE_PREFIX` | `frappe-http` | Separate HTTP key namespace |
| `HTTP_REDIS_CACHE_GENERATION` | `1` | Increment and restart to invalidate existing HTTP entries |

Public assets and files can return Redis hits even with browser hard-refresh headers. Other responses are cached only for anonymous GET requests with explicit `Cache-Control: public` and a positive lifetime. Authorization, cookies, private/no-store/no-cache responses, Set-Cookie, unsupported Vary, API, login and protected routes bypass shared caching. Range and HEAD requests use normal handling. Keys separate sites, hosts, schemes, languages, queries and image builds. `X-HTTP-Redis-Cache` reports `HIT`, `MISS` or `BYPASS`. Redis failures fall back to normal file/upstream service.

Changed or deleted public files can remain cached until their TTL expires. Bump the generation to invalidate them immediately; hard refresh intentionally does not purge static entries. No `FLUSHDB`, Redis eviction-policy change or application/session-cache mutation is performed. Account for HTTP objects in Redis capacity planning. Responses above the size limit remain uncached. Nginx's Debian Lua/srcache modules and the locked official Redis Lua client are included in the image's security checks.

## Local verification

Use Python 3.14 and install `requirements-ci.txt`. Offline checks:

```bash
python -m unittest discover -s tests -v
bash -n scripts/base-image.sh resources/start.sh resources/nginx-entrypoint.sh
```

Building requires Docker Buildx/Compose, network access and sufficient disk space. Set `GITHUB_TOKEN` in the process environment for API access; it is never passed into builds. Authenticate GHCR pulls first. To test applications on the selected base:

```bash
python scripts/resolve.py
python scripts/pipeline.py build
python scripts/pipeline.py integration
python scripts/pipeline.py scan
```

Base preparation is separate: `bash scripts/base-image.sh` requires `GHCR_USER`, `GHCR_TOKEN` and `GITHUB_SHA`, then `python scripts/pipeline.py scan-base` verifies the result. Use the manual Action to select verified digests on main. Never invoke base preparation from application builds.

Upstream application licenses remain applicable to bundled sources.
