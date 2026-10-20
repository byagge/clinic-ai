#!/bin/bash
# Bootstrap executed once by the Postgres image entrypoint.
#
# The entrypoint runs an executable script as a process and sources a
# non-executable one. Either way, psql variables are passed with -v so a
# password never has to be interpolated through a psql backtick.
set -euo pipefail

app_user="${ARIX_APP_USER:-arix_app}"
app_password="${ARIX_APP_PASSWORD:-arix}"

psql -v ON_ERROR_STOP=1 \
  --username "${POSTGRES_USER}" \
  --dbname "${POSTGRES_DB}" \
  -v app_user="${app_user}" \
  -v app_password="${app_password}" \
  -f /usr/local/share/arix/init-db.sql
