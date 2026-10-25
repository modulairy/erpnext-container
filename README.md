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

npm/Yarn are confined to the builder, and esbuild compiler packages are removed after asset generation. Other node_modules are retained because public asset symlinks serve some files directly; their package metadata remains available to the scanner. MariaDB and Redis image digests are also scanned separately, and their HIGH/CRITICAL findings block publication. No CVE exceptions are configured.

The build job has no package write permission. Only a successful run can transfer the exact tested image to the main-only publish job; it verifies the image ID and pushes without rebuilding.

## Runtime

One image supports separate `backend` (default), `frontend`, `websocket`, `scheduler`, `worker-short`, `worker-default` and `worker-long` containers. Other commands are passed through, for example `bench --site example.com migrate`. Run as the included non-root `frappe` user. Use persistent volumes for `/home/frappe/frappe-bench/sites` and `/home/frappe/frappe-bench/logs`; do not mount a separate assets volume, because image assets must match application code. All service roles must use the same image digest.

Print Designer uses the image's distribution-maintained Chromium through its expected executable path, so site installation does not download an older browser.

Helpdesk search requires Redis Search commands. The test harness uses Redis 8 with its integrated Search module; production Redis must provide the same capability.

MariaDB/Redis are external services. Create a site and configure `db_host`, `redis_cache`, `redis_queue` and `redis_socketio` in the shared site configuration before starting application roles. Set `webserver_host` to the backend service and `webserver_port` to 8000 so realtime authentication can reach it. The frontend accepts `BACKEND` (default `backend:8000`), `SOCKETIO` (default `websocket:9000`) and `FRAPPE_SITE_NAME_HEADER` (default HTTP host). Internal ports are 8000, 8080 and 9000. Supply deployment-specific health checks for each role; a single image health check cannot describe all roles. `tests/compose.yaml` is a disposable test harness, not a production deployment.

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
