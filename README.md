# Substack Gateway OSS

[![CI](https://github.com/jakub-k-slys/substack-gateway-oss/actions/workflows/ci.yml/badge.svg)](https://github.com/jakub-k-slys/substack-gateway-oss/actions/workflows/ci.yml)
[![E2E Tests](https://github.com/jakub-k-slys/substack-gateway-oss/actions/workflows/e2e.yml/badge.svg)](https://github.com/jakub-k-slys/substack-gateway-oss/actions/workflows/e2e.yml)
[![Deployed on Vercel](https://img.shields.io/badge/deployed%20on-Vercel-000000?logo=vercel)](https://substack-gateway.vercel.app)

A stateless Python gateway for [Substack](https://substack.com) that exposes
a REST API and an MCP server on top of the same service layer.

Designed to make Substack data and actions accessible from scripts,
applications, and AI tooling — without duplicating integration logic across
different interfaces.

## What You Can Do

- Read public Substack profiles, posts, notes, and comments
- Access authenticated `me` endpoints with a base64-encoded credential token
- Create and delete notes through the REST API
- Use the same gateway as an MCP server for AI tools and agent workflows
- Extend the app with custom routes, MCP tools, auth providers, and lifespan
  hooks

## Interfaces

- **REST API** at `/api/v1/*`
- **MCP server** at `/mcp`

Both share the same service layer and HTTP clients. The thin
`substack_gateway` shell package assembles the application (REST + MCP
composition), while `gateway_oss` provides the OSS module surface as a facade.

## Quickstart

Requirements:

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)

Install dependencies:

```bash
uv sync --dev
```

Run the application locally:

```bash
uv run python -m substack_gateway.main
```

or from an environment file

```bash
uv run --env-file .env.autopilot substack-autopilot --once
```

Check the root metadata endpoint:

```bash
curl http://127.0.0.1:5001/
```

Check the liveness probe:

```bash
curl http://127.0.0.1:5001/api/v1/health/live
```

Fetch a public profile:

```bash
curl http://127.0.0.1:5001/api/v1/profiles/<slug>
```

## REST Example

Public profile lookup:

```bash
curl http://127.0.0.1:5001/api/v1/profiles/<slug>
```

Authenticated request:

```bash
curl \
  -H "x-gateway-token: <base64-encoded-json>" \
  http://127.0.0.1:5001/api/v1/me
```

The REST API is mounted under `/api/v1` and includes endpoints for health,
profiles, posts, notes, comments, and authenticated `me` operations.

## Authentication

Gateway access and Substack access are separate concerns.

For this OSS repository, Substack credentials are passed as a base64-encoded
JSON object. REST requests send that value as the `x-gateway-token` header.

Credential shape:

```json
{
  "publication_url": "https://example.substack.com",
  "substack_sid": "s%3A..."
}
```

Encode it with:

```bash
printf '%s' '{"publication_url":"https://example.substack.com","substack_sid":"s%3A..."}' | base64 | tr -d '\n'
```

`SUBSTACK_GATEWAY_TOKEN` is this locally encoded JSON value; it is not a token
issued by Substack under that name. Obtain `substack_sid` from the
`substack.sid` cookie for your signed-in publication session using browser
developer tools (Application or Storage → Cookies) or a local cookies.txt
export. Preserve the exported value exactly, including percent encoding.

Older sessions may also expose a `connect.sid` cookie. If present, it can be
included as the optional JSON field `"connect_sid"`; current sessions commonly
use only `substack.sid`. Never invent or duplicate a missing cookie. Treat all
session cookies as bearer credentials, do not commit real values, and rotate the
session if a credential has been exposed.

## Content Autopilot

The optional `substack-autopilot` worker generates Markdown with Azure OpenAI,
publishes notes on deterministic three-hour slots, and generates one newsletter
slot per day. SQLite stores slot state, artifact paths, draft IDs, and post IDs
so restarts do not intentionally create duplicate work.

Copy `autopilot.env.example` to `.env.autopilot` and fill in credentials.
Run one local cycle with:

```bash
uv run --env-file .env.autopilot substack-autopilot --once
```

For continuous no-Docker operation on a Mac, follow
[Run Content Autopilot on macOS](docs/macos-autopilot.md). Docker deployment is
described below.

Set `SUBSTACK_AUTOPILOT_WRITER_ID` to your numeric Substack user ID. To find it,
open a draft in Substack, use browser developer tools → Network, select the
successful `GET /api/v1/drafts/<draft-id>` request, and read
`postBylines[0].user_id` from its JSON response. The worker uses this value in
newsletter `draft_bylines` and skips the legacy global `/api/v1/user-settings`
lookup, which may return 403 for modern `substack.sid`-only sessions. Do not use
the draft ID, publication ID, or post ID as the writer ID.

Newsletter behavior is controlled by
`SUBSTACK_AUTOPILOT_NEWSLETTER_MODE`:

- `artifact_only`: generate Markdown without contacting Substack.
- `draft_only`: create or update a draft (the safe default).
- `publish_web`: publish to the web with email delivery disabled.
- `publish_and_email`: publish and request subscriber email delivery.

`publish_and_email` additionally requires the exact explicit confirmation shown
in `autopilot.env.example`. Email delivery is high impact and the captured API
flow has only been verified with email disabled; test it against a disposable
publication before enabling it for a real subscriber list.

Image generation is disabled by default. Set
`SUBSTACK_AUTOPILOT_IMAGES_ENABLED=true` and configure an Azure OpenAI image
deployment to generate PNG, JPEG, or WebP artifacts. The generator accepts Azure
responses containing either base64 image data or a temporary HTTPS download URL.
Image size and quality values are deployment-specific; the configured Azure
model and API version must support them.

Generated images are written atomically and their paths and generation status
are persisted in SQLite, so a restart reuses an existing artifact instead of
regenerating it. For newsletters, the worker uploads the image after creating the
draft and prepends it to the newsletter body. Upload response metadata is also
persisted so a later draft-update retry reuses the uploaded image. An upload with
a lost response is treated as `publishing_unknown` and is not retried
automatically.

Newsletter cover images remain unset, and generated images are not attached to
notes. Those private contracts have not been verified, so only the verified
newsletter body-image flow is enabled. Note creation posts to Substack's global
`comment/feed/` endpoint; `SUBSTACK_AUTOPILOT_WRITER_ID` does not change note
authentication if that endpoint rejects the session.

The worker writes generated artifacts and `autopilot.sqlite3` to the configured
artifact directory and state path. Docker uses the persistent `autopilot-data`
volume; local macOS installations should use absolute paths under `data/`. A
`publishing_unknown` slot is deliberately not automatically retried because
Substack may have accepted a request whose response was lost; inspect and
reconcile such a slot manually.

A single note artifact can also be published with:

```bash
uv run substack-publish-note path/to/note.md
```

### Deploy Content Autopilot on an Azure VM

These instructions target an Ubuntu Azure VM that may already run other Docker
applications. Content Autopilot is a background worker: it publishes no host
ports and needs no reverse-proxy route or inbound Azure Network Security Group
(NSG) rule. It requires outbound TCP 443 access to your Azure OpenAI endpoint,
Substack, and Substack's media storage.

Install Docker Engine and the Docker Compose plugin using Docker's official
Ubuntu instructions, then verify them:

```bash
docker --version
docker compose version
```

Install the worker in its own directory. Replace the repository URL if you use a
fork:

```bash
sudo git clone https://github.com/jakub-k-slys/substack-gateway-oss.git /opt/substack-gateway-oss
sudo chown -R "$USER":"$USER" /opt/substack-gateway-oss
cd /opt/substack-gateway-oss
cp autopilot.env.example .env.autopilot
chmod 600 .env.autopilot
```

Edit `.env.autopilot` with the locally constructed `SUBSTACK_GATEWAY_TOKEN`,
`SUBSTACK_AUTOPILOT_WRITER_ID`, and Azure OpenAI settings described above. Keep
`SUBSTACK_AUTOPILOT_NEWSLETTER_MODE=draft_only` until drafts have been reviewed
successfully. Never commit `.env.autopilot`, and rotate the Substack session
cookies and update the file if the session expires or is exposed.

Start the worker with an explicit Compose project name:

```bash
docker compose --project-name substack-autopilot -f compose.autopilot.yaml up --build -d
```

The explicit name keeps this deployment's container, network, and volume names
separate from existing Compose applications, including an application whose
default project name might otherwise match the directory. The `env_file` entry
in `compose.autopilot.yaml` loads `.env.autopilot` into the container; a Compose
`--env-file` option is not needed. On initial startup, the one-shot
`autopilot-data-init` service makes the volume writable by the non-root worker.
It must exit successfully before `autopilot` starts.

Use the same project name and Compose file for every operation:

```bash
# Status
docker compose --project-name substack-autopilot -f compose.autopilot.yaml ps

# Follow logs
docker compose --project-name substack-autopilot -f compose.autopilot.yaml logs -f --tail=200 autopilot

# Restart
docker compose --project-name substack-autopilot -f compose.autopilot.yaml restart autopilot

# Stop without deleting persistent data
docker compose --project-name substack-autopilot -f compose.autopilot.yaml down
```

Do not add `-v` to `docker compose down`: that deletes the volume containing
`autopilot.sqlite3`, generated artifacts, and upload metadata. Run exactly one
Autopilot replica against this SQLite volume; do not use `--scale` or attach the
volume to multiple workers.

If an older deployment repeatedly reports `sqlite3.OperationalError: unable to
open database file`, update to the current Compose file and run `up --build -d`
again. To repair the existing volume immediately before updating, run:

```bash
docker compose --project-name substack-autopilot -f compose.autopilot.yaml run --rm --user root autopilot chown -R appuser:appuser /data
docker compose --project-name substack-autopilot -f compose.autopilot.yaml up -d
```

Before an upgrade, stop only this worker and back up its volume to the
deployment directory. Stopping it first gives SQLite a consistent snapshot and
does not affect other Compose projects:

```bash
docker compose --project-name substack-autopilot -f compose.autopilot.yaml stop autopilot
VOLUME="$(docker volume ls --filter label=com.docker.compose.project=substack-autopilot --filter label=com.docker.compose.volume=autopilot-data -q)"
test -n "$VOLUME"
docker run --rm -v "$VOLUME:/data:ro" -v "$PWD:/backup" alpine tar -czf /backup/substack-autopilot-backup.tar.gz -C /data .
docker compose --project-name substack-autopilot -f compose.autopilot.yaml start autopilot
```

The temporary `alpine` image may need to be pulled the first time. Protect the
backup because its SQLite database and artifacts contain publication content.
Copy it to durable storage or include it in the VM's encrypted backup policy.

Upgrade and rebuild in place:

```bash
cd /opt/substack-gateway-oss
git pull --ff-only
docker compose --project-name substack-autopilot -f compose.autopilot.yaml up --build -d
docker compose --project-name substack-autopilot -f compose.autopilot.yaml logs --tail=200 autopilot
```

To roll back application code, check out a previously tested tag or commit and
rebuild with the same project name:

```bash
cd /opt/substack-gateway-oss
git checkout --detach COMMIT_SHA
docker compose --project-name substack-autopilot -f compose.autopilot.yaml up --build -d
```

Replace `COMMIT_SHA` with the tested revision. Preserve `.env.autopilot` and the
named volume. If a release changes the database incompatibly, stop the worker
and restore the matching volume backup before starting the older version.

## MCP

The MCP server is mounted at `/mcp` and served over streamable HTTP.

Public OSS MCP tools include:

- `get_note`
- `get_post`
- `get_post_comments`
- `get_profile`
- `get_profile_posts`
- `get_profile_notes`

## Project Layout

Core application code lives in `src/gateway_oss/`.

- `api/v1/`: FastAPI route handlers
- `mcp/`: FastMCP tool surface and transport integration
- `services/`: shared business logic
- `client/`: Substack HTTP client wrappers
- `models/`: schemas and pagination models
- `converters/`: Markdown conversion
- `extensions/`: runtime extension hooks

## Configuration

Application settings are environment-driven via the `SUBSTACK_GATEWAY_` prefix.

Common examples include:

- `SUBSTACK_GATEWAY_LOG_LEVEL`
- `SUBSTACK_GATEWAY_SUBSTACK_BASE_URL`
- `SUBSTACK_GATEWAY_SUBSTACK_TIMEOUT_SEC`

## Validation

```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check .
uv build
uv run pytest tests/
uv run behave features/
```

## Documentation

The repository includes MkDocs and Read the Docs configuration:

- [Docs home](docs/index.md)
- [Introduction](docs/introduction.md)
- [Installation guide](docs/installation.md)
- [Authentication](docs/authentication.md)
- [API reference](docs/api-reference.md)
- [MCP documentation](docs/mcp.md)
- [Development guide](docs/development.md)
- [Contributing guide](CONTRIBUTING.md)

Read the Docs can build the site directly from `.readthedocs.yaml` and `mkdocs.yml`.

## Author

Built by [Jakub Slys](https://iam.slys.dev) — Backend Engineer building
distributed systems for telecoms, running a self-hosted Kubernetes homelab,
and building AI automation pipelines with n8n, MCP, and Claude.

This gateway is the backend I use to automate my own Substack at
[iam.slys.dev](https://iam.slys.dev), where I write about system design,
machine learning fundamentals, and the AI tools I actually build and run
in production.

If you want to understand how this gateway works under the hood — the
architecture decisions, what I got wrong the first time, and how it fits
into a full content automation stack — that's what the newsletter is for.

→ [iam.slys.dev](https://iam.slys.dev)
