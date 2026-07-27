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
/swingengine asset refresh
/swingengine asset search sun
```

## NSE asset search

Refresh the local Upstox NSE instrument catalog before the first search:

```text
/swingengine asset refresh
```

The command downloads
`https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz`,
unpacks and validates it, then atomically replaces the existing JSON catalog.
If the download or JSON is invalid, the previous catalog remains available.

Searches are case-insensitive and match trading symbols, names, asset and
underlying symbols, instrument keys, and ISINs:

```text
/swingengine asset search sun
```

NSE cash-market instruments are shown before related derivatives, and output
is capped at 20 results by default. The catalog is cached in memory after it is
first loaded.

The defaults work with the persistent volume described below. They can be
overridden when needed:

```bash
export UPSTOX_ASSET_FILE=/var/lib/swingengine/NSE.json
export UPSTOX_ASSET_URL=https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz
export UPSTOX_ASSET_REQUEST_TIMEOUT_SECONDS=30
export UPSTOX_ASSET_SEARCH_LIMIT=20
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

## PostgreSQL database

Run the setup script on the server that already hosts PostgreSQL for
Botsquadron:

```bash
./setup_database.sh
```

The script automatically runs `psql` through the local `postgres` system
account, creates the `swingengine` database if it does not exist, and applies
[`database/schema.sql`](database/schema.sql). It may request the server user's
`sudo` password, but it does not require a PostgreSQL password or an
interactive database login. It is also safe to run the whole setup script as
root:

```bash
sudo ./setup_database.sh
```

The default database and owner are `swingengine` and `postgres`. They can be
overridden for a different installation:

```bash
SWINGENGINE_DATABASE_NAME=another_name \
SWINGENGINE_DATABASE_OWNER=existing_postgres_role \
./setup_database.sh
```

Both scripts stop on the first error. The SQL prevents concurrent schema
deployments and applies table changes in a transaction. It is safe to rerun
unchanged. As requirements evolve, keep the `CREATE TABLE IF NOT EXISTS`
statements for new installations and append explicit, rerunnable changes such
as `ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...` inside the existing
transaction. Editing a column inside `CREATE TABLE IF NOT EXISTS` does not
update an existing table. Back up production before destructive or
data-converting changes.

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
