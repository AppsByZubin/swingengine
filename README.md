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
/swingengine auth set <token>
```

## Upstox token management

SwingEngine can keep the access token in a persistent local state file and
validate it against Upstox every three hours. The validation uses the Upstox
profile API. If the token is missing, expired, or rejected, SwingEngine sends
a persistent private Slack alert to the configured user.

Configure the expected Upstox account and Slack alert destination:

```text
UPSTOX_EXPECTED_USER_ID
SLACK_ALERT_USER_ID
```

Enable manual token management and configure its persistent state file:

```bash
export UPSTOX_TOKEN_MANAGEMENT_ENABLED=true
export UPSTOX_TOKEN_MONITOR_ENABLED=true
export UPSTOX_TOKEN_CHECK_INTERVAL_SECONDS=10800
export UPSTOX_TOKEN_ROTATION_ENABLED=false
export UPSTOX_WEBHOOK_ENABLED=false
export UPSTOX_TOKEN_FILE=/var/lib/swingengine/upstox-token.json
export UPSTOX_TOKEN_TIMEZONE=Asia/Kolkata
```

Generate the access token in Upstox, then submit it from the authorized Slack
account:

```text
/swingengine auth set <token>
```

SwingEngine validates the submitted token with the Upstox profile API before
storing it. The token is redacted from application logs. The state file is
atomically replaced with mode `0600`; mount its parent directory on persistent,
encrypted storage in production. Never log or commit the file.

The Slack app needs the `chat:write` and `commands` bot scopes. Reinstall it
after applying the updated manifest. `SLACK_ALERT_USER_ID` also controls which
Slack user may run `auth set`.

The notifier-webhook implementation remains available for later use. It is
inactive while `UPSTOX_TOKEN_ROTATION_ENABLED` and `UPSTOX_WEBHOOK_ENABLED`
are both `false`.

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
  -e SLACK_ALERT_USER_ID \
  -e UPSTOX_TOKEN_MANAGEMENT_ENABLED \
  -e UPSTOX_TOKEN_MONITOR_ENABLED \
  -e UPSTOX_EXPECTED_USER_ID \
  -v swingengine-state:/var/lib/swingengine \
  swingengine:local
```

The container runs as the non-root user `10001:10001`. It needs outbound
network access to Slack and Upstox. No inbound port or domain is required for
the manual Slack workflow because commands use Slack Socket Mode.

The GitHub Actions workflow publishes `bizzkpm/swingengine` to Docker Hub and
updates `helm/swingengine/values.yaml` in the `AppsByZubin/botyard` repository.
Configure these repository secrets before running the workflow:

- `DOCKERHUB_USERNAME`
- `DOCKERHUB_TOKEN`
- `BOTYARD_REPO_TOKEN` (write access to `AppsByZubin/botyard`)
