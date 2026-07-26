# SwingEngine

SwingEngine currently provides a small Slack command interface. It uses Slack
Socket Mode, so it can receive commands without a public web server or request
URL.

## Slack setup

1. Open Slack's app management page and create an app **from an app manifest**.
2. Paste the contents of `slack-manifest.yaml`, choose the workspace, and create
   the app.
3. Under **Basic Information → App-Level Tokens**, generate a token with the
   `connections:write` scope. This is the `xapp-...` token.
4. Under **OAuth & Permissions**, install the app to the workspace and copy its
   `xoxb-...` bot token.
5. If Slack asks, reinstall the app after changing its manifest or OAuth scopes.

Keep both tokens out of source control.

## Install and run

Initialize Conda if it is not already available in the shell, then use the
`swingengine` environment:

```bash
source /home/amit/anaconda3/etc/profile.d/conda.sh
conda deactivate
conda activate swingengine
python -m pip install -r requirements-dev.txt
```

Export the Slack tokens and start the blocking Socket Mode listener:

```bash
export SLACK_BOT_TOKEN='xoxb-...'
export SLACK_APP_TOKEN='xapp-...'
python main.py
```

The app responds privately to the user who invokes one of these commands:

```text
/swingengine help
/swingengine ping
/swingengine status
/swingengine auth status
/swingengine auth request
```

## Upstox token rotation

SwingEngine can request the standard Upstox access token each morning and
receive the approved token through an HTTPS notifier webhook. Upstox still
requires the account holder to approve the request in the Upstox app or
WhatsApp.

Configure these static credentials through a secret store:

```text
UPSTOX_API_KEY
UPSTOX_API_SECRET
UPSTOX_EXPECTED_USER_ID
```

Enable the workflow and configure its persistent state file:

```bash
export UPSTOX_TOKEN_ROTATION_ENABLED=true
export UPSTOX_TOKEN_FILE=/var/lib/swingengine/upstox-token.json
export UPSTOX_TOKEN_REQUEST_TIME=07:30
export UPSTOX_TOKEN_TIMEZONE=Asia/Kolkata
export UPSTOX_WEBHOOK_ENABLED=true
export UPSTOX_WEBHOOK_HOST=0.0.0.0
export UPSTOX_WEBHOOK_PORT=8080
export UPSTOX_WEBHOOK_PATH=/webhooks/upstox/token
```

The webhook verifies a received token against the Upstox profile API and
checks its `client_id` and `user_id` before storing it. The state file is
atomically replaced with mode `0600`; mount its parent directory on persistent,
encrypted storage in production. Never log or commit the file.

Register this public HTTPS notifier URL in Upstox Developer Apps:

```text
https://<your-host>/webhooks/upstox/token
```

Optional settings include:

```text
UPSTOX_API_BASE_URL=https://api.upstox.com
UPSTOX_TOKEN_REQUEST_TIMEOUT_SECONDS=15
UPSTOX_TOKEN_RETRY_INTERVAL_SECONDS=300
UPSTOX_SCHEDULER_POLL_INTERVAL_SECONDS=30
UPSTOX_VERIFY_WEBHOOK_TOKEN=true
```

## Add commands

Business commands belong in `slack/commands.py`. Add a handler returning
an `ephemeral(...)` response, then register it in `build_router()`. Keeping that
layer independent of Slack Bolt makes command behavior quick to test.

Run the tests with:

```bash
pytest -q
```

## Container image

Build and run the service locally with Docker:

```bash
docker build -t swingengine:local .
docker run --rm \
  -e SLACK_BOT_TOKEN \
  -e SLACK_APP_TOKEN \
  -e UPSTOX_TOKEN_ROTATION_ENABLED \
  -e UPSTOX_API_KEY \
  -e UPSTOX_API_SECRET \
  -e UPSTOX_EXPECTED_USER_ID \
  -p 8080:8080 \
  -v swingengine-state:/var/lib/swingengine \
  swingengine:local
```

The container runs as the non-root user `10001:10001`. It needs outbound
network access to Slack and Upstox. When token rotation is enabled, expose only
the notifier webhook through an HTTPS reverse proxy.

The GitHub Actions workflow publishes `bizzkpm/swingengine` to Docker Hub and
updates `helm/swingengine/values.yaml` in the `AppsByZubin/botyard` repository.
Configure these repository secrets before running the workflow:

- `DOCKERHUB_USERNAME`
- `DOCKERHUB_TOKEN`
- `BOTYARD_REPO_TOKEN` (write access to `AppsByZubin/botyard`)
