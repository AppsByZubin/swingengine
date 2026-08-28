#!/usr/bin/env bash
set -Eeuo pipefail

# Creates/updates the SwingEngine database on the local PostgreSQL server.
# The script uses the local postgres system account, so no database password
# or interactive psql login is required.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Support both the canonical scripts/setup_database.sh location and a copy
# placed directly in the repository root.
if [[ -r "${SCRIPT_DIR}/database/schema.sql" ]]; then
    SCHEMA_FILE="${SCRIPT_DIR}/database/schema.sql"
elif [[ -r "${SCRIPT_DIR}/../database/schema.sql" ]]; then
    SCHEMA_FILE="$(cd -- "${SCRIPT_DIR}/.." && pwd)/database/schema.sql"
else
    echo "Error: database/schema.sql was not found relative to ${SCRIPT_DIR}" >&2
    echo "Keep the database directory together with the SwingEngine repository." >&2
    exit 1
fi

DATABASE_NAME="${SWINGENGINE_DATABASE_NAME:-swingengine}"
POSTGRES_ADMIN_USER="${POSTGRES_ADMIN_USER:-postgres}"
DATABASE_OWNER="${SWINGENGINE_DATABASE_OWNER:-${POSTGRES_ADMIN_USER}}"
MAINTENANCE_DATABASE="${POSTGRES_MAINTENANCE_DATABASE:-postgres}"
POSTGRES_OS_USER="${POSTGRES_OS_USER:-postgres}"
APP_ROLE="${SWINGENGINE_DATABASE_APP_ROLE:-}"

fail() {
    echo "Error: $*" >&2
    exit 1
}

validate_identifier() {
    local value="$1"
    local label="$2"

    if [[ ! "${value}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        fail "${label} must match ^[A-Za-z_][A-Za-z0-9_]*$"
    fi
}

validate_identifier "${DATABASE_NAME}" "SWINGENGINE_DATABASE_NAME"
validate_identifier "${DATABASE_OWNER}" "SWINGENGINE_DATABASE_OWNER"
validate_identifier "${POSTGRES_ADMIN_USER}" "POSTGRES_ADMIN_USER"
validate_identifier "${MAINTENANCE_DATABASE}" \
    "POSTGRES_MAINTENANCE_DATABASE"
validate_identifier "${POSTGRES_OS_USER}" "POSTGRES_OS_USER"
if [[ -n "${APP_ROLE}" ]]; then
    validate_identifier "${APP_ROLE}" "SWINGENGINE_DATABASE_APP_ROLE"
fi

PSQL_BIN="$(command -v psql || true)"
[[ -n "${PSQL_BIN}" ]] \
    || fail "psql is not installed or is not available in PATH"

id "${POSTGRES_OS_USER}" >/dev/null 2>&1 \
    || fail "PostgreSQL system user does not exist: ${POSTGRES_OS_USER}"

run_as_postgres() {
    if [[ "$(id -un)" == "${POSTGRES_OS_USER}" ]]; then
        "${PSQL_BIN}" "$@"
    elif [[ "${EUID}" -eq 0 ]]; then
        command -v runuser >/dev/null 2>&1 \
            || fail "runuser is required when this script is run as root"
        (
            cd /
            runuser -u "${POSTGRES_OS_USER}" -- "${PSQL_BIN}" "$@"
        )
    else
        command -v sudo >/dev/null 2>&1 \
            || fail "sudo is required to run psql as ${POSTGRES_OS_USER}"
        (
            cd /
            sudo -u "${POSTGRES_OS_USER}" -- "${PSQL_BIN}" "$@"
        )
    fi
}

echo "Applying SwingEngine schema to database \"${DATABASE_NAME}\"..."

PSQL_SET_ARGS=(
    --set=ON_ERROR_STOP=on
    --set="swingengine_database=${DATABASE_NAME}"
    --set="swingengine_owner=${DATABASE_OWNER}"
)
if [[ -n "${APP_ROLE}" ]]; then
    PSQL_SET_ARGS+=(--set="swingengine_app_role=${APP_ROLE}")
fi

# The invoking shell opens the SQL file before switching users. This also
# works when the repository is inside a home directory that postgres cannot
# traverse.
run_as_postgres \
    -X \
    --username="${POSTGRES_ADMIN_USER}" \
    --dbname="${MAINTENANCE_DATABASE}" \
    "${PSQL_SET_ARGS[@]}" \
    < "${SCHEMA_FILE}"

echo "SwingEngine database setup completed successfully."
