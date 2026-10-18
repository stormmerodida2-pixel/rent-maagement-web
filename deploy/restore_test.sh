#!/usr/bin/env bash
# Prove the latest OFF-SITE backup can actually be restored. Run from a computer with admin AWS CLI access:
#
#   deploy/restore_test.sh            # uses the `ssh rentipay` host (Session Manager)
#   deploy/restore_test.sh other-host
#
# 1. Finds the newest database and files backups in the S3 bucket and makes 15-minute download links for them
#    (the server itself can only upload to the bucket, never read it).
# 2. On the server: downloads both and decrypts them there, so BACKUP_ENCRYPTION_KEY never leaves the server.
# 3. Restores the database into a throwaway Postgres container and compares row counts with the live database;
#    unpacks the files archive and compares the file count with the live upload folders.
# 4. Deletes the throwaway container and every downloaded or decrypted file, even if something fails.
#
# Nothing live is touched. Counts can differ a little: the backup is from 02:00 and the site has moved on since.
set -euo pipefail

HOST=${1:-rentipay}
REGION=ap-south-1

env_value() { ssh "$HOST" "grep -E '^$1=' app/.env_prod | cut -d= -f2-"; }
BUCKET=$(env_value BACKUP_S3_BUCKET)
BUCKET_REGION=$(env_value BACKUP_S3_REGION)
[ -n "$BUCKET" ] || { echo "BACKUP_S3_BUCKET isn't set on $HOST: off-site backups aren't set up."; exit 1; }

latest() {  # latest <name pattern>
  aws s3 ls "s3://$BUCKET/backups/" --region "$BUCKET_REGION" | awk '{print $4}' | { grep -E "$1" || true; } | sort | tail -1
}
DB_KEY=$(latest '^rentals-[0-9]{8}-[0-9]{6}\.dump\.enc$')
FILES_KEY=$(latest '^rentals-files-[0-9]{8}-[0-9]{6}\.tar\.gz\.enc$')
[ -n "$DB_KEY" ] || { echo "No database backup found in s3://$BUCKET/backups/."; exit 1; }  # a files backup is optional
echo "Testing s3://$BUCKET/backups/$DB_KEY"
[ -n "$FILES_KEY" ] && echo "    and s3://$BUCKET/backups/$FILES_KEY"
DB_URL=$(aws s3 presign "s3://$BUCKET/backups/$DB_KEY" --region "$BUCKET_REGION" --expires-in 900)
FILES_URL=""
[ -n "$FILES_KEY" ] && FILES_URL=$(aws s3 presign "s3://$BUCKET/backups/$FILES_KEY" --region "$BUCKET_REGION" --expires-in 900)

# The script and the links go over stdin, so the links never show up in the server's process list.
{
printf 'DB_URL=%q\nFILES_URL=%q\n' "$DB_URL" "$FILES_URL"
cat <<'REMOTE'
set -euo pipefail
cd ~/app
WORK=$(mktemp -d /tmp/restore-test.XXXXXX)
chmod 777 "$WORK"  # the app container runs as its own user
NAME=restore-test-db
cleanup() {
  docker rm -f "$NAME" > /dev/null 2>&1 || true
  sudo rm -rf "$WORK"
}
trap cleanup EXIT
compose() { docker compose --env-file .env_prod "$@"; }
decrypt() { compose run --rm --no-deps -T -v "$WORK:/work" web python manage.py decrypt_backup "/work/$1" -o "/work/$2"; }

echo "== Database =="
curl -fsS -m 300 -o "$WORK/db.enc" "$DB_URL"
echo "Downloaded $(du -h "$WORK/db.enc" | cut -f1)."
decrypt db.enc db.dump

docker run -d --name "$NAME" -e POSTGRES_PASSWORD=restore-test -v "$WORK:/work:ro" postgres:17-alpine \
  -c shared_buffers=32MB -c max_connections=10 > /dev/null
for _ in $(seq 60); do docker exec "$NAME" pg_isready -U postgres -q 2>/dev/null && break; sleep 1; done
docker exec "$NAME" psql -U postgres -qc 'CREATE DATABASE restored' > /dev/null
docker exec "$NAME" pg_restore --no-owner --exit-on-error -U postgres -d restored /work/db.dump
echo "Restored into a throwaway database without errors."

TABLES="auth_user rentals_property rentals_unit rentals_tenant rentals_lease rentals_rentcharge payments_payment payments_expense maintenance_maintenancerequest audit_auditentry"
count_sql=""
for t in $TABLES; do count_sql="$count_sql SELECT '$t', count(*) FROM $t UNION ALL"; done
count_sql="${count_sql% UNION ALL};"
restored=$(docker exec "$NAME" psql -U postgres -d restored -AtF' ' -c "$count_sql")
live=$(compose exec -T db psql -U rentals -d rentals -AtF' ' -c "$count_sql")
printf '%-34s %10s %10s\n' TABLE BACKUP LIVE
bad=0
while read -r table n; do
  now=$(echo "$live" | awk -v t="$table" '$1 == t {print $2}')
  flag=""
  if [ "$now" -gt 0 ] && [ "$n" -eq 0 ]; then flag="  <- empty in the backup!"; bad=1; fi
  printf '%-34s %10s %10s%s\n' "$table" "$n" "$now" "$flag"
done <<< "$restored"

echo "== Uploaded files =="
if [ -n "$FILES_URL" ]; then
  curl -fsS -m 300 -o "$WORK/files.enc" "$FILES_URL"
  decrypt files.enc files.tar.gz
  in_backup=$(tar -tzf "$WORK/files.tar.gz" | grep -vc '/$' || true)
  live_files=$(compose exec -T web sh -c 'find /app/media /app/private_media -type f 2>/dev/null | wc -l')
  echo "Files archive opens: $in_backup files in the backup, $live_files on the server now."
else
  echo "No files backup yet (made when there are uploaded photos)."
fi

if [ "$bad" -eq 0 ]; then echo "RESTORE TEST PASSED"; else echo "RESTORE TEST FAILED: see the table above"; exit 1; fi
REMOTE
} | ssh "$HOST" 'bash -c "$(cat)"'  # read it all first: commands inside mustn't eat the script
