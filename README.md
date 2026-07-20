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
  swingengine:local
```

The container runs as the non-root user `10001:10001`. It only needs outbound
network access to Slack because Socket Mode does not expose an HTTP service.

The GitHub Actions workflow publishes `bizzkpm/swingengine` to Docker Hub and
updates `helm/swingengine/values.yaml` in the `AppsByZubin/botyard` repository.
Configure these repository secrets before running the workflow:

- `DOCKERHUB_USERNAME`
- `DOCKERHUB_TOKEN`
- `BOTYARD_REPO_TOKEN` (write access to `AppsByZubin/botyard`)
