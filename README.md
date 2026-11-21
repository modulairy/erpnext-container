# Community ERPNext container

Builds `ghcr.io/modulairy/erpnext:<ERPNext-release-tag>` from public upstream sources. GitHub Actions starts **only manually**. Base creation is a separate manual workflow. Application workflows only consume approved immutable base images; no push, PR, release or scheduled triggers are configured.

## Applications and versions

Includes Frappe, ERPNext, Helpdesk, Telephony, Builder, Payments, Webshop, CRM, Wiki and Print Designer. Writer is excluded because it requires Drive. No Modulairy application, private repository, ACR base image or SCM credential is used.

Each run enumerates official stable GitHub releases and resolves declared app dependencies. The reviewed framework line is v16; future major versions require a runtime/test policy update. Python 3.14 and Node 24 LTS use the newest compatible patch releases; Bench and Yarn 1 are resolved from their official registries. All app sources are locked to commit SHAs and base/test/scanner images to registry digests before building.

Telephony (`develop`), Payments (`version-16`) and Webshop (`version-16`) currently have no published releases. They use explicit, immutable commit snapshots and are identified as such in `image-lock.json`. Payments/Webshop `develop` require Frappe 17 and cannot be used with stable v16. Missing metadata, undeclared app dependencies or an incompatible complete set stops the run. Metadata compatibility is only a candidate check: installation and runtime tests must also pass.

The GHCR tag is the selected ERPNext release, for example `v16.54.0`. Rebuilding that ERPNext release can update the same tag when another app, snapshot or security package changes. Use the reported image digest for immutable deployments. No `latest` tag is published. The complete source/runtime selection is in `/home/frappe/frappe-bench/image-lock.json` and the Actions reports.

## Manual build and publication

In **Actions > Community ERPNext > Run workflow**, choose a branch and leave `publish` disabled for validation. After reviewing and merging the PR, run on `main` with `publish` enabled to publish. The repository's `GITHUB_TOKEN` needs GHCR package write access; the first package may need its visibility set to public in GitHub package settings. No personal registry token is required.

Every build is followed by an isolated site installation and migration for every included app, HTTP/login/API CRUD checks, static asset requests, websocket connectivity, real jobs on all three worker queues and runtime process checks. MariaDB and Redis are temporary test services, with generated credentials and no production access. Reports and sanitized logs are retained for 14 days. Temporary containers, networks and volumes are removed on completion/failure.

Trivy scans OS and language dependencies, secrets and repository configuration. Package and browser CVEs are advisory. High and critical secret and configuration findings, scanner errors and failed tests block publication. Raw reports and `gate-security.json` retain the findings. Publication includes SBOM and provenance attestations.

`security-policy.json` defines reviewed dependency update ranges. Each run selects stable versions from PyPI/npm, applies explicit source overlays before installation and frontend compilation, and records the changes in the image lock. These overlays intentionally differ from upstream dependency pins; image-codec, PDF-reader, file-lock and GitPython checks supplement integration tests. A newer upstream requirement that would be downgraded stops the build for policy review.

npm/Yarn are confined to the builder; pip and esbuild compiler packages are removed from the final runtime after installation and asset generation. Add or update dependencies through a rebuilt image. Other node_modules are retained because public asset symlinks serve some files directly; their package metadata remains available to the scanner. MariaDB and Redis image digests are also scanned separately. External MariaDB vulnerabilities are advisory by owner decision; Redis vulnerabilities remain blocking. Scanner errors and secret findings remain blocking for both services.

Security overlays update direct dependency declarations as well as Yarn resolutions. Upstream Yarn locks are regenerated within the reviewed ranges, and installed package versions are checked before asset compilation. Explicit major migrations are listed in the policy and must pass the same build and runtime tests. MariaDB client libraries come from the official Frappe runtime. The external MariaDB service has a separate vulnerability report.

Plyr stays on 3.7.8 because 3.8 changes the deep import used by this Frappe release. Helpdesk uses the patched Vite 7 line because Vite 8 rejects duplicate imports in its current source. Other applications use Vite 8. Wiki's Tiptap packages are aligned at 3.34.0 to keep their core, ProseMirror and extension peer requirements consistent. Revisiting these compatibility limits requires a successful frontend build.

Curl library packages use the newest common version from signed Debian 13 backports. The installed MariaDB client and curl versions are checked against the image lock during integration tests.

Helpdesk uses Zod 4 with its unused Zod 3 adapter removed. A source guard rejects new adapter consumers; runtime tests exercise the actual email and phone validators.

The application build job pulls the public, approved Frappe digests. Only a successful run can transfer the exact tested image to the main-only publish job, which verifies the image ID and pushes without rebuilding.

After a successful push and provenance/SBOM attestations, the publish job creates a Git tag and GitHub Release containing the image digest, source revision, locked applications and Actions evidence. The first release uses the ERPNext tag; rebuilding that ERPNext version from a different source revision uses `<ERPNext-tag>-<7-character-commit-SHA>`. Existing source tags are never moved. The GHCR tag remains the ERPNext tag. Only the publish job has the repository write permission needed for tags/releases. Logout runs last, including on failure.


## Frappe framework image

**Actions > Frappe framework > Run workflow** builds `ghcr.io/modulairy/frappe:<Frappe-release-tag>` with Bench and Frappe only, using the approved ready base images. Enable `publish` on `main` to publish the tested digest and a `frappe-<Frappe-release-tag>` GitHub Release. Rebuilds from another revision append the short commit SHA to the source tag. This release does not replace the latest ERPNext release.

The bases replace Debian Chromium with the SHA-256-pinned Google Chrome Headless Shell. The product scan reports high and critical findings for the installed version and blocks on scanner or coverage-control failures.

## Official base images

The manual base workflow resolves Linux amd64 digests of `frappe/build:version-16` and `frappe/base:version-16`. It replaces the browser and checks the exact Debian package changes, then scans both images and records their inventories. On main, it publishes the exact scanned images and updates the base manifest only after both scans pass. Frappe and ERPNext consume those approved digests.

## Reviewed Frappe 16 backports

`patches/manifest.json` records the source revisions, input/output file hashes and patch checksums for the opt-in [Frappe symlink discovery change](https://github.com/frappe/frappe/pull/44199) and [Helpdesk Redis ticket search](https://github.com/frappe/helpdesk/pull/3914). These are container backports, not upstream-merged features. They are reviewed for Frappe 16.60.2 and Helpdesk 16.2.1. Resolution/build fails on an unreviewed version or changed source instead of omitting a patch. Applied provenance is recorded in `image-lock.json`.

Symlink site discovery is disabled by default. Set `allow_symlink_sites: true` in `common_site_config.json` or set `FRAPPE_ALLOW_SYMLINK_SITES=true`; ENV wins, including explicit false. Only configured directory targets are included. Backend, scheduler, workers and frontend must share the same readable target paths. Manage site directories and links as trusted operator configuration; aliases of one database are not separate tenants.

Ticket search defaults to SQLite. To opt into Redis, use the site's existing `redis_cache` with Redis 8 Search supporting `FT.AGGREGATE ADDSCORES` and cursors:

```sh
bench --site example.com set-config helpdesk_search_backend redis
bench --site example.com execute helpdesk.ticket_search.build_index
```

Keep the scheduler and long workers running. Indexing happens after database commit; reads can briefly lag writes. Missing indexes are rebuilt through scheduled recovery. Force rebuilding temporarily makes ticket search unavailable. Ordinary site cache clearing preserves search keys; a full Redis flush removes them and requires rebuilding. Redis ranking/prefix matching differs from SQLite and has no SQLite spelling correction. Capacity, persistence and eviction must account for index data. Article search retains its existing implementation. Return to SQLite by changing the setting to `sqlite` and rebuilding on storage with SQLite-compatible locking; existing SQLite data is not deleted automatically. Frappe 16 uses its supported bounded background retry mechanism rather than the develop-only `rq_retry` argument.

## Runtime

One image supports separate `backend` (default), `frontend`, `websocket`, `scheduler`, `worker-short`, `worker-default` and `worker-long` containers. Other commands are passed through, for example `bench --site example.com migrate`. Run as the included non-root `frappe` user. Use persistent volumes for `/home/frappe/frappe-bench/sites` and `/home/frappe/frappe-bench/logs`; do not mount a separate assets volume, because image assets must match application code. All service roles must use the same image digest.

Classic prints, report exports and Print Designer use Chrome Headless Shell. The application overlay replaces pdfkit calls with the sandboxed renderer. PDF jobs run as `frappe`, use private temporary directories under `/tmp/frappe-pdf`, and terminate after 90 seconds. Generated invoice/report PDFs and renderer sandbox evidence are retained in the Actions reports.

Containers that render PDFs require Linux user namespaces and the supplied `resources/chromium-seccomp.json` profile, with `cap_drop: [ALL]`, `no-new-privileges:true`, and `init: true` (see `tests/compose.yaml`). The profile derives from [Playwright's Docker profile](https://github.com/microsoft/playwright/blob/v1.64.0/utils/docker/seccomp_profile.json), adding `chroot` so Chromium can isolate itself inside its user namespace without host capabilities. Configure the profile on the Docker host; it cannot be enabled from inside the image. Hosts that prohibit unprivileged user namespaces must receive a narrowly scoped administrator policy change before running this image. The renderer fails closed; do not disable the sandbox, use privileged containers or grant `SYS_ADMIN`.

PDF HTML cannot run document JavaScript or fetch external resources. Store print logos, fonts and styles in permitted site/app assets; private files retain Frappe permission checks. Custom formats depending on external URLs or document scripts need adaptation. Browser CDP listens only on loopback and is never exposed as a service.

Helpdesk search requires Redis Search commands. The test harness uses Redis 8 with its integrated Search module; production Redis must provide the same capability.

MariaDB/Redis are external services. Create a site and configure `db_host`, `redis_cache`, `redis_queue` and `redis_socketio` in the shared site configuration before starting application roles. Set `webserver_host` to the backend service and `webserver_port` to 8000 so realtime authentication can reach it. The frontend accepts `BACKEND` (default `backend:8000`), `SOCKETIO` (default `websocket:9000`) and `FRAPPE_SITE_NAME_HEADER` (default HTTP host). Internal ports are 8000, 8080 and 9000. Supply deployment-specific health checks for each role; a single image health check cannot describe all roles. `tests/compose.yaml` is a disposable test harness, not a production deployment.

## Local verification

Requires Python 3.14, Docker with Buildx/Compose, network access and sufficient disk space for the multi-app build. Set `GITHUB_TOKEN` in the process environment to avoid unauthenticated API limits. It is used only by the resolver, never passed into the container build. Optionally set `BUILD_DIR` to an absolute output directory.

```sh
python -m pip install -r requirements-ci.txt
python -m unittest discover -s tests -v
python scripts/pipeline.py prepare-base
python scripts/pipeline.py scan-base
# The application resolver uses the approved manifest produced by the base workflow.
python scripts/resolve.py
python scripts/pipeline.py build
python scripts/pipeline.py integration
python scripts/pipeline.py scan
```

Container layout follows the official [Frappe Docker](https://github.com/frappe/frappe_docker) approach. Upstream application licenses remain applicable to their bundled sources.

