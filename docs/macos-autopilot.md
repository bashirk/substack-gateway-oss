# Run Content Autopilot on macOS

This guide runs `substack-autopilot` continuously on a Mac without Docker. A
per-user `launchd` agent starts the worker when you log in, restarts it after an
unexpected failure, and stores its logs and persistent state inside the project.

## Requirements

- macOS with a user account that remains logged in
- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- a local clone of this repository in a non-protected location such as
  `~/Developer/substack-gateway-oss`
- an Azure OpenAI text deployment and, optionally, an image deployment
- a signed-in Substack publication session
- optionally, a signed-in X session for X Autopilot

The Mac must be awake and connected to the internet when a slot is due. A user
LaunchAgent does not run before login and cannot publish while the Mac is asleep.
Do not run another Autopilot instance against the same schedule, including a
Docker or VM deployment.

Do not place the working clone under `~/Documents`, `~/Desktop`, or
`~/Downloads`. macOS privacy controls can let an interactive Terminal access
those folders while denying the same access to a background LaunchAgent, causing
`Operation not permitted`. Prefer:

```bash
mkdir -p ~/Developer
cd ~/Developer
git clone <repository-url> substack-gateway-oss
cd substack-gateway-oss
```

## 1. Install dependencies

From the repository root:

```bash
uv sync --dev
command -v uv
```

The installer records the absolute `uv` path because `launchd` does not load your
interactive shell configuration or its `PATH`.

## 2. Configure credentials and schedules

Create the local environment file and restrict access to it:

```bash
cp autopilot.env.example .env.autopilot
chmod 600 .env.autopilot
```

Set at least:

```dotenv
SUBSTACK_GATEWAY_TOKEN=<base64-encoded-credential-json>
SUBSTACK_AUTOPILOT_WRITER_ID=<numeric-user-id>
SUBSTACK_AUTOPILOT_TOPICS="artificial intelligence|software engineering|building products"
SUBSTACK_AUTOPILOT_CONTEXT="Write in a practical voice for technical founders."
SUBSTACK_AUTOPILOT_TIMEZONE=America/New_York
SUBSTACK_AUTOPILOT_NEWSLETTER_MODE=draft_only
AZURE_OPENAI_ENDPOINT=https://example.openai.azure.com
AZURE_OPENAI_KEY=<secret>
AZURE_OPENAI_DEPLOYMENT_NAME=<text-deployment>
```

For local persistence, use absolute paths:

```dotenv
SUBSTACK_AUTOPILOT_STATE_PATH=/absolute/path/to/substack-gateway-oss/data/autopilot.sqlite3
SUBSTACK_AUTOPILOT_ARTIFACT_DIR=/absolute/path/to/substack-gateway-oss/data/artifacts
```

If newsletter images are wanted, also set:

```dotenv
SUBSTACK_AUTOPILOT_IMAGES_ENABLED=true
AZURE_OPENAI_IMAGE_DEPLOYMENT_NAME=<image-deployment>
AZURE_OPENAI_IMAGE_SIZE=1536x1024
AZURE_OPENAI_IMAGE_QUALITY=medium
```

Keep pipe-separated values quoted. Do not use `source .env.autopilot`; let `uv`
parse it with `--env-file`. Never commit this file.

### Optional X configuration

X Autopilot uses the unofficial [Twikit](https://github.com/d60/twikit) browser
API wrapper. X can change these private APIs without notice, and automated use
may trigger verification, cookie expiry, or rate limits. The worker only creates
posts; it does not automate likes, follows, replies, DMs, or other engagement.

With X signed in, export a Netscape-format `cookies.txt` using your local browser
extension. Import only `auth_token` and `ct0` into an ignored mode-`0600` file:

```bash
uv run python scripts/import-x-cookies.py \
  /path/to/x-cookies.txt \
  data/x-cookies.json
```

The importer accepts `x.com` and legacy `twitter.com` cookie domains and never
prints cookie values. If creating the file manually, use complete, unshortened
values:

```json
{
  "auth_token": "YOUR_COMPLETE_VALUE",
  "ct0": "YOUR_COMPLETE_VALUE"
}
```

Then run `chmod 600 data/x-cookies.json`. The ignored `data/` directory prevents
the file from being committed, but it remains a sensitive login credential.

Start with artifact-only mode and an absolute cookie path:

```dotenv
X_AUTOPILOT_ENABLED=true
X_AUTOPILOT_COOKIES_PATH=/absolute/path/to/substack-gateway-oss/data/x-cookies.json
X_AUTOPILOT_INTERVAL_HOURS=3
X_AUTOPILOT_IMAGES_ENABLED=false
X_AUTOPILOT_MODE=artifact_only
```

X scheduling is independent of notes and newsletters. Setting
`X_AUTOPILOT_IMAGES_ENABLED=true` generates and uploads one image per X slot and
requires the Azure image deployment settings. Live posting is deliberately
gated by both:

```dotenv
X_AUTOPILOT_MODE=publish
X_AUTOPILOT_PUBLISH_CONFIRMATION=PUBLISH_TO_X
```

Generated X text is requested below 260 characters and validated at no more than
280 Unicode code points. The worker persists an uploaded media ID before tweet
creation and reuses it after safe pre-create failures. Once tweet creation is
attempted, any exception becomes `publishing_unknown` and is not retried
automatically, preventing an uncertain request from creating duplicate posts.

`SUBSTACK_GATEWAY_TOKEN` is base64-encoded compact JSON, not a value issued under
that name by Substack:

```json
{
  "publication_url": "https://example.substack.com",
  "substack_sid": "s%3A..."
}
```

Get the complete `substack.sid` value from browser developer tools or a local
cookies.txt export. Preserve percent encoding, do not include a terminal prompt
marker, and reject values shortened with `...`. `connect_sid` is optional and
must not be invented when the cookie is absent. See [Authentication](authentication.md)
for credential details.

Find `SUBSTACK_AUTOPILOT_WRITER_ID` in a successful
`GET /api/v1/drafts/<draft-id>` response at `postBylines[0].user_id`.

## 3. Test one cycle

Keep newsletter mode set to `draft_only`, then run:

```bash
uv run --env-file .env.autopilot substack-autopilot --once
```

A successful image-bearing newsletter draft normally logs this sequence:

```text
POST /api/v1/drafts → 200
POST /api/v1/image → 200
PUT /api/v1/drafts/<draft-id> → 200
```

Review the generated note, newsletter, and `data/artifacts/x_post/` output before
enabling continuous operation. A note is published immediately when notes are
enabled; `draft_only` applies to newsletters, not notes. Set
`SUBSTACK_AUTOPILOT_NOTES_ENABLED=false` during a newsletter-only test if
publishing a note is not desired.

For a controlled X live test, first stop the LaunchAgent so two workers cannot
use the same SQLite database:

```bash
launchctl bootout gui/$(id -u)/com.substack-gateway.autopilot
X_AUTOPILOT_MODE=publish \
X_AUTOPILOT_PUBLISH_CONFIRMATION=PUBLISH_TO_X \
uv run --env-file .env.autopilot substack-autopilot --once
launchctl bootstrap gui/$(id -u) \
  ~/Library/LaunchAgents/com.substack-gateway.autopilot.plist
```

This posts every currently due, nonterminal X slot, not arbitrary test text.
Inspect the generated artifact before changing to live mode. Do not run this
one-off command while the LaunchAgent is active.

## 4. Install the LaunchAgent

Make the scripts executable and run the installer as your normal macOS user, not
with `sudo`:

```bash
chmod +x scripts/*.sh
./scripts/install-launchd-agent.sh
```

The installer refuses to load the service from macOS privacy-protected
`Documents`, `Desktop`, or `Downloads` folders. Move the clone to `~/Developer`
first if necessary.

The installer:

- discovers and records the absolute `uv` executable
- creates `~/Library/LaunchAgents/com.substack-gateway.autopilot.plist`
- validates the plist with `plutil`
- starts the worker immediately
- writes logs to `data/logs/`
- reads secrets at runtime from `.env.autopilot` instead of copying them into the
  plist

The worker uses `RunAtLoad` and is restarted only after an unsuccessful exit.
The long-running worker itself polls according to
`SUBSTACK_AUTOPILOT_POLL_SECONDS`; `launchd` does not schedule individual posts.

## Operate the service

Check status:

```bash
launchctl print gui/$(id -u)/com.substack-gateway.autopilot
```

Follow logs:

```bash
tail -f data/logs/autopilot.log data/logs/autopilot.error.log
```

Restart after changing `.env.autopilot` or updating code:

```bash
launchctl kickstart -k gui/$(id -u)/com.substack-gateway.autopilot
```

Stop it temporarily:

```bash
launchctl bootout gui/$(id -u)/com.substack-gateway.autopilot
```

Start it again after a temporary stop:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.substack-gateway.autopilot.plist
```

Remove the LaunchAgent while preserving SQLite, artifacts, and logs:

```bash
./scripts/uninstall-launchd-agent.sh
```

Rerun `./scripts/install-launchd-agent.sh` after moving the repository or when
`command -v uv` changes, because the generated plist contains absolute paths.

## Sleep, login, and continuous operation

A LaunchAgent runs only in the logged-in user's GUI session. Closing the Terminal
window does not stop it, but logging out or shutting down does. macOS suspends the
worker during sleep; after wake, it resumes and processes due slots according to
the application's schedule and persisted SQLite state.

For reliable operation:

- keep the Mac powered and connected
- configure System Settings → Lock Screen or Energy settings to prevent automatic
  sleep when appropriate
- use `caffeinate` only for temporary runs; it is not configured by this project
- ensure exactly one Autopilot instance is active

## State, backups, and updates

The recommended local paths are:

```text
data/autopilot.sqlite3
data/artifacts/
data/logs/
```

Stop the LaunchAgent before making a consistent SQLite backup:

```bash
launchctl bootout gui/$(id -u)/com.substack-gateway.autopilot
cp -R data data.backup
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.substack-gateway.autopilot.plist
```

Protect backups: generated drafts may be private and the environment file
contains session and Azure credentials. The ignored `data/` directory is local
runtime state and should not be committed.

To update:

```bash
launchctl bootout gui/$(id -u)/com.substack-gateway.autopilot
git pull --ff-only
uv sync --dev
./scripts/install-launchd-agent.sh
```

## Troubleshooting

### Service is not found

Install it again as the logged-in user:

```bash
./scripts/install-launchd-agent.sh
```

Do not use `sudo`; root and your GUI user have different `launchd` domains.

### `Operation not permitted` for the project or runner

Stop the agent and move or clone the repository from `Documents`, `Desktop`, or
`Downloads` to a location such as `~/Developer/substack-gateway-oss`. Copy the
ignored `.env.autopilot` and `data/` directory securely, update their absolute
paths in `.env.autopilot`, and run the installer from the new clone. Granting
Full Disk Access to a shell is less reliable and broader than necessary.

### `uv is not executable`

Run `command -v uv`, then rerun the installer. Homebrew paths can differ between
Intel and Apple Silicon Macs.

### Dotenv parsing fails

Quote values containing spaces or pipes:

```dotenv
SUBSTACK_AUTOPILOT_TOPICS="AI|software engineering|building products"
```

An empty or non-base64 `SUBSTACK_GATEWAY_TOKEN` can subsequently appear as a JSON
decoding failure. Validate the environment with the one-cycle command before
installing the agent.

### Substack returns 401 or 403 JSON

Refresh `substack.sid` from a current signed-in browser session, rebuild the
base64 token, update `.env.autopilot`, and restart the service. Treat the cookie
as a password.

### Substack returns a Cloudflare HTML challenge

If the response is HTML containing `Just a moment...`, the source IP or client is
being challenged before Substack authentication. Running from the same local Mac
and network as the browser may work where a cloud VM does not. Copying
`cf_clearance` is not a reliable unattended fix.

### A slot is `publishing_unknown`

Do not blindly retry it. Substack may have accepted the write while the response
was lost. Inspect Substack and reconcile SQLite state only after confirming
whether the note, draft, image, or publication exists.

### Logs repeat rapidly

Inspect `data/logs/autopilot.error.log`. `KeepAlive` restarts a crashing process,
so configuration errors can produce repeated launches. Temporarily stop the
agent with `launchctl bootout`, fix `.env.autopilot`, test with `--once`, and run
the installer again.
