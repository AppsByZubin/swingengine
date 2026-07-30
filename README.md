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

The app responds privately to the user who invokes one of these commands,
except CSV exports, which it uploads to the conversation:

```text
/swingengine help
/swingengine ping
/swingengine status
/swingengine auth status
/swingengine auth set <token>
/swingengine instrument refresh
/swingengine instrument search sun
/swingengine asset add SUNPHARMA
/swingengine asset delete SUNPHARMA
/swingengine asset list
/swingengine asset list file
/swingengine asset upload
/swingengine tracker add SUNPHARMA
/swingengine tracker delete SUNPHARMA
/swingengine tracker list
/swingengine tracker list file
/swingengine tracker upload
/swingengine tracker asset evaluate
```

## NSE instrument search

Refresh the local Upstox NSE instrument catalog before the first search:

```text
/swingengine instrument refresh
```

The command downloads
`https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz`,
unpacks and validates it, then atomically replaces the existing JSON catalog.
If the download or JSON is invalid, the previous catalog remains available.

Searches are case-insensitive and match trading symbols, names, asset and
underlying symbols, instrument keys, and ISINs:

```text
/swingengine instrument search sun
```

NSE cash-market instruments are shown before related derivatives, and output
is capped at 20 results by default. The catalog is cached in memory after it is
first loaded.

Save an exact NSE trading symbol from the catalog, then add the saved asset to
the tracker:

```text
/swingengine asset add SUNPHARMA
/swingengine tracker add SUNPHARMA
```

Trading symbols received from Slack are normalized to uppercase before the
NSE catalog lookup and database operation. Duplicate asset and tracker
requests are left unchanged and reported as already present.

`asset list` shows saved names, trading symbols, and instrument keys.
`tracker list` joins tracked rows to their asset names and symbols. An asset
must be removed from the tracker before it can be deleted from the saved asset
table.

Add `file` to either list command to generate a CSV file and upload it to the
Slack conversation:

```text
/swingengine asset list file
/swingengine tracker list file
```

Use `/swingengine asset upload` to open a Slack file picker and apply asset
adds and deletes in bulk. Upload one UTF-8 CSV of no more than 1 MB or 1,000
data rows with exactly these columns:

```csv
name,action
reliance,add
tcs,delete
infosys,add
```

Trading symbols are normalized to uppercase. Add rows must exactly match a
symbol in the local NSE catalog. Existing assets are reported as already
present; delete rows fail when the asset does not exist or is still tracked.
The app sends the requesting user a private summary after processing all rows.

At startup, SwingEngine creates `input` and `output` under its runtime file
directory. Source-checkout runs default to `files`, while the container uses
the writable persistent path `/var/lib/swingengine/files`. Override either
with `SWINGENGINE_FILES_DIR`. Uploaded asset CSVs are stored in `input`. CSV
snapshots are written atomically to `output/asset-list.csv` and
`output/tracker-list.csv` before being uploaded. Empty lists produce a CSV
containing only its column headings. Tracker exports contain the asset name,
trading symbol, momentum/trade/approval flags, allocated amount, and added
date; internal tracker and asset IDs are omitted.

The Slack administrator configured by `SLACK_ALERT_USER_ID` can update tracker
approval and allocation values by exporting, editing, and uploading the tracker
CSV:

```text
/swingengine tracker list file
/swingengine tracker upload
```

Keep the seven-column export header unchanged. SwingEngine uses
`trading_symbol` to find each tracker row and updates only
`is_approved_for_trade` and `amount_allocated`; changes to the other exported
columns are ignored. Approval must be `True` or `False`. When
`is_approved_for_trade` is `True`, `amount_allocated` must be greater than
`5000`. An unapproved row may use any nonnegative allocation.

## Tracker momentum evaluation

At 4 PM `Asia/Kolkata` on weekdays, SwingEngine evaluates every saved asset
that is not yet tracked and every tracked asset where
`is_trade_created = FALSE`. It requests the previous 200 calendar days of
daily Upstox candles and combines the V3 historical response with the V3
intraday daily candle so the current trading day is included.

The calculation matches
`visualizer/notebooks/swingengine/ema_crossover.ipynb`: EMA 21 uses
`adjust=False`, SMA 50 is a rolling mean, and each angle is the arctangent of
the average price-point slope over three bars, clipped to `[-10, 10]`.
Momentum requires both strict conditions:

```text
ema_21_angle > 70
sma_50_angle > 50
```

A qualifying untracked asset is inserted into `tracker`. A qualifying pending
entry is refreshed. Both receive `has_momentum = TRUE`,
`is_trade_created = FALSE`, `is_approved_for_trade = FALSE`, and the current
date. A pending entry that no longer qualifies has momentum and approval
cleared. Rows with a created trade are not changed.

Run the same evaluation on demand:

```text
/swingengine tracker asset evaluate
```

The scheduler is enabled by default. Its settings can be overridden:

```bash
export SWINGENGINE_TRACKER_EVALUATION_ENABLED=true
export SWINGENGINE_TRACKER_EVALUATION_TIME=16:00
export SWINGENGINE_TRACKER_EVALUATION_TIMEZONE=Asia/Kolkata
export SWINGENGINE_TRACKER_EVALUATION_LOOKBACK_DAYS=200
export SWINGENGINE_TRACKER_EMA_ANGLE_THRESHOLD=70
export SWINGENGINE_TRACKER_SMA_ANGLE_THRESHOLD=50
export SWINGENGINE_TRACKER_EVALUATION_RETRY_INTERVAL_SECONDS=300
export SWINGENGINE_TRACKER_EVALUATION_POLL_INTERVAL_SECONDS=30
```

The evaluator uses the access token stored by the Upstox token-management
workflow. Assets without an `instrument_key` are reported as failed and left
unchanged.

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

The Slack app needs the `chat:write`, `commands`, `files:read`, and
`files:write` bot scopes. `files:read` is required for asset and tracker imports
and `files:write` is required for list exports. Reinstall the app after applying
the updated manifest. Add the SwingEngine bot to each channel where it should
import or export CSV files; private channels require an explicit invitation.
`SLACK_ALERT_USER_ID` also controls which Slack user may run `auth set` and
`tracker upload`.

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

The Slack asset and tracker commands connect at runtime with:

```bash
export SWINGENGINE_DATABASE_URL='postgresql://swingengine_app:password@postgres:5432/swingengine'
export SWINGENGINE_DATABASE_CONNECT_TIMEOUT_SECONDS=10
```

Use a PostgreSQL role with `SELECT`, `INSERT`, `UPDATE`, and `DELETE`
privileges on the `assets` and `tracker` tables, plus usage on their identity
sequences. `UPDATE` on `tracker` is required by momentum reevaluation. Keep
credentials out of source control.

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
  -e SWINGENGINE_DATABASE_URL \
  -v swingengine-state:/var/lib/swingengine \
  swingengine:local
```

The container runs as the non-root user `10001:10001`. It needs outbound
network access to Slack and Upstox. No inbound port or domain is required for
the manual Slack workflow because commands use Slack Socket Mode. Runtime CSV
files are stored under `/var/lib/swingengine/files`, which must remain writable
when the container root filesystem is read-only.

The GitHub Actions workflow publishes `bizzkpm/swingengine` to Docker Hub and
updates `helm/swingengine/values.yaml` in the `AppsByZubin/botyard` repository.
Configure these repository secrets before running the workflow:

- `DOCKERHUB_USERNAME`
- `DOCKERHUB_TOKEN`
- `BOTYARD_REPO_TOKEN` (write access to `AppsByZubin/botyard`)
