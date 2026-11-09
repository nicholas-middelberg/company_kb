#!/usr/bin/env bash
#
# Applies forward-only SQL migrations in order (ADR-0029).
#
# Usage:
#   scripts/migrate.sh            apply every pending migration
#   scripts/migrate.sh --status   list applied and pending, change nothing
#
# Milestone 1 talks to the local Docker Postgres. Milestone 4 swaps the
# transport for a direct connection to RDS; the ledger and the .sql files are
# unchanged by that.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIGRATIONS_DIR="$REPO_ROOT/src/kb/db/migrations"
PG_USER="${KB_PG_USER:-kb}"
PG_DATABASE="${KB_PG_DATABASE:-company_kb}"
SERVICE="postgres"

# Transport, not mechanism. ADR-0029 anticipated this: Milestone 4 needs the
# same ledger and the same .sql files applied over a direct connection to RDS
# rather than through docker compose. CI uses `direct` against a service
# container, which is the same path.
run_psql() {
  if [[ "${KB_MIGRATE_TRANSPORT:-docker}" == "direct" ]]; then
    psql "${DATABASE_URL:?DATABASE_URL is required for the direct transport}" \
      -v ON_ERROR_STOP=1 "$@"
  else
    docker compose --project-directory "$REPO_ROOT" exec -T "$SERVICE" \
      psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d "$PG_DATABASE" "$@"
  fi
}

checksum() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d' ' -f1
  else
    sha256sum "$1" | cut -d' ' -f1
  fi
}

require_database() {
  if ! run_psql -c 'SELECT 1' >/dev/null 2>&1; then
    echo "error: cannot reach Postgres." >&2
    echo "       docker transport: docker compose up -d" >&2
    echo "       direct transport: set DATABASE_URL and KB_MIGRATE_TRANSPORT=direct" >&2
    exit 1
  fi
}

ensure_ledger() {
  # client_min_messages: IF NOT EXISTS emits a NOTICE on every run after the
  # first, which is noise on an otherwise quiet command.
  run_psql -q <<'SQL'
SET client_min_messages = warning;
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     text        PRIMARY KEY,
    checksum    text        NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
SQL
}

applied_checksum() {
  run_psql -tAq -c \
    "SELECT checksum FROM schema_migrations WHERE version = '$1'"
}

main() {
  local status_only=0
  [[ "${1:-}" == "--status" ]] && status_only=1

  require_database
  ensure_ledger

  shopt -s nullglob
  local files=("$MIGRATIONS_DIR"/*.sql)
  if [[ ${#files[@]} -eq 0 ]]; then
    echo "no migrations found in $MIGRATIONS_DIR" >&2
    exit 1
  fi

  local pending=0
  for file in "${files[@]}"; do
    local version sum existing
    version="$(basename "$file" .sql)"
    sum="$(checksum "$file")"
    existing="$(applied_checksum "$version" | tr -d '[:space:]')"

    if [[ -n "$existing" ]]; then
      if [[ "$existing" != "$sum" ]]; then
        # A forward-only migration that changed after being applied means the
        # database and the repository disagree about history. Refuse rather
        # than guess which one is right.
        echo "error: $version was applied but its file has changed." >&2
        echo "       applied checksum: $existing" >&2
        echo "       current checksum: $sum" >&2
        echo "       Migrations are never edited once applied (ADR-0029)." >&2
        echo "       Write a new migration instead." >&2
        exit 1
      fi
      echo "  applied   $version"
      continue
    fi

    pending=$((pending + 1))
    if [[ $status_only -eq 1 ]]; then
      echo "  PENDING   $version"
      continue
    fi

    echo "  applying  $version"
    # The .sql file carries its own BEGIN/COMMIT, so a failure rolls back the
    # whole migration. The ledger row is written only after it commits.
    run_psql -q < "$file"
    run_psql -q -c \
      "INSERT INTO schema_migrations (version, checksum) VALUES ('$version', '$sum')"
  done

  if [[ $status_only -eq 1 ]]; then
    echo "$pending pending"
  elif [[ $pending -eq 0 ]]; then
    echo "nothing to apply"
  else
    echo "applied $pending migration(s)"
  fi
}

main "$@"
