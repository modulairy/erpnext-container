# Community ERPNext container

Builds `ghcr.io/modulairy/erpnext:<ERPNext-release-tag>` from public upstream sources. GitHub Actions runs **only manually**. No push, PR, release or scheduled triggers are configured.

## Applications and versions

Includes Frappe, ERPNext, Helpdesk, Telephony, Builder, Payments, Webshop, CRM, Wiki and Print Designer. Writer is excluded because it requires Drive. No Modulairy application, private repository, ACR base image or SCM credential is used.

Each run enumerates official stable GitHub releases and resolves declared app dependencies. The reviewed framework line is v16; future major versions require a runtime/test policy update. Python 3.14 and Node 24 LTS use the newest compatible patch releases; Bench and Yarn 1 are resolved from their official registries. All app sources are locked to commit SHAs and base/test/scanner images to registry digests before building.

Telephony (`develop`), Payments (`version-16`) and Webshop (`version-16`) currently have no published releases. They use explicit, immutable commit snapshots and are identified as such in `image-lock.json`. Payments/Webshop `develop` require Frappe 17 and cannot be used with stable v16. Missing metadata, undeclared app dependencies or an incompatible complete set stops the run. Metadata compatibility is only a candidate check: installation and runtime tests must also pass.

The GHCR tag is the selected ERPNext release, for example `v16.52.1`. Rebuilding that ERPNext release can update the same tag when another app, snapshot or security package changes. Use the reported image digest for immutable deployments. No `latest` tag is published. The complete source/runtime selection is in `/home/frappe/frappe-bench/image-lock.json` and the Actions reports.

## Manual build and publication

In **Actions â†’ Community ERPNext â†’ Run workflow**, choose a branch and leave `publish` disabled for validation. After reviewing and merging the PR, run on `main` with `publish` enabled to publish. The repository's `GITHUB_TOKEN` needs GHCR package write access; the first package may need its visibility set to public in GitHub package settings. No personal registry token is required.

Every build is followed by an isolated site installation and migration for every included app, HTTP/login/API CRUD checks, static asset requests, websocket connectivity, real jobs on all three worker queues and runtime process checks. MariaDB and Redis are temporary test services, with generated credentials and no production access. Reports and sanitized logs are retained for 14 days. Temporary containers, networks and volumes are removed on completion/failure.

Trivy scans the image's OS/language dependencies and secrets, and the repository's configuration and secrets. HIGH/CRITICAL findings (including unfixed findings), scanner errors or failed tests block publication. A failed upstream combination is reported; the workflow does not remove apps, weaken the gate or silently exempt vulnerabilities. SBOM and provenance attestations accompany publication. Passing smoke tests is not a guarantee that every application feature is covered.

`security-policy.json` defines reviewed dependency update ranges. Each run selects stable versions from PyPI/npm, applies explicit source overlays before installation and frontend compilation, and records the changes in the image lock. These overlays intentionally differ from upstream dependency pins; image-codec, PDF-reader, file-lock and GitPython checks supplement integration tests. A newer upstream requirement that would be downgraded stops the build for policy review.

npm/Yarn are confined to the builder; pip and esbuild compiler packages are removed from the final runtime after installation and asset generation. Add or update dependencies through a rebuilt image. Other node_modules are retained because public asset symlinks serve some files directly; their package metadata remains available to the scanner. MariaDB and Redis image digests are also scanned separately, and their HIGH/CRITICAL findings block publication. No CVE exceptions are configured.

Security overlays update direct dependency declarations as well as Yarn resolutions. Upstream Yarn locks are regenerated within the reviewed ranges, and installed package versions are checked before asset compilation. Explicit major migrations are listed in the policy and must pass the same build and runtime tests. MariaDB client libraries inside the ERPNext image use the newest common 11.8 patch from MariaDB's signed Debian 13 repository; the external MariaDB service remains an official image and has a separate vulnerability report.

Plyr stays on 3.7.8 because 3.8 changes the deep import used by this Frappe release. Helpdesk uses the patched Vite 7 line because Vite 8 rejects duplicate imports in its current source. Other applications use Vite 8. Revisiting these compatibility limits requires a successful frontend build.

Curl library packages use the newest common version from signed Debian 13 backports. The installed MariaDB client and curl versions are checked against the image lock during integration tests.

Helpdesk uses Zod 4 with its unused Zod 3 adapter removed. A source guard rejects new adapter consumers; runtime tests exercise the actual email and phone validators. ACL 2.4.0, ncurses 6.6, Perl 5.42.3 and systemd 262.1 source packages are rebuilt against Debian 13 in an isolated stage, with package tests enabled. Only the selected runtime packages are installed, including systemd's client libraries rather than its daemon. Signed Debian source indexes and locked source SHA256 values protect the inputs; source and binary provenance is retained in the image lock. These reviewed source versions fail closed if unavailable. No unstable binary repository is installed. A backported fix can still be reported by the distribution security feed; those findings remain blocking.

The build job has no package write permission. Only a successful run can transfer the exact tested image to the main-only publish job; it verifies the image ID and pushes without rebuilding.

## Runtime

One image supports separate `backend` (default), `frontend`, `websocket`, `scheduler`, `worker-short`, `worker-default` and `worker-long` containers. Other commands are passed through, for example `bench --site example.com migrate`. Run as the included non-root `frappe` user. Use persistent volumes for `/home/frappe/frappe-bench/sites` and `/home/frappe/frappe-bench/logs`; do not mount a separate assets volume, because image assets must match application code. All service roles must use the same image digest.

Classic prints, report exports and Print Designer use the distribution-maintained Chromium. The image removes pdfkit and wkhtmltopdf. PDF jobs run as `frappe`, use private temporary directories under `/tmp/frappe-pdf`, and terminate after 90 seconds. Generated invoice/report PDFs and renderer sandbox evidence are retained in the Actions reports.

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

Requires Python 3.14, Docker with Buildx/Compose, network access and sufficient disk space for the multi-app build. Set `GITHUB_TOKEN` in the process environment to avoid unauthenticated API limits. It is used only by the resolver, never passed into the container build. Optionally set `BUILD_DIR` to an absolute output directory.

```sh
python -m pip install -r requirements-ci.txt
python -m unittest discover -s tests -v
python scripts/resolve.py
python scripts/pipeline.py build
python scripts/pipeline.py integration
python scripts/pipeline.py scan
```

Container layout follows the official [Frappe Docker](https://github.com/frappe/frappe_docker) approach. Upstream application licenses remain applicable to their bundled sources.
