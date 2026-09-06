#!/usr/bin/env bash
# Local daily backup of the PIA database (the eligibility memory is the
# irreplaceable part). Keeps the last 14 dumps. Run via Task Scheduler:
#   schtasks /Create /SC DAILY /ST 09:30 /TN "PIA backup" /TR "C:\Program Files\Git\bin\bash.exe -c 'cd /e/agent/placement-intelligence/infrastructure && ./backup-db.sh'"
set -euo pipefail
cd "$(dirname "$0")"
BACKUP_DIR="../backups"
mkdir -p "$BACKUP_DIR"
STAMP=$(date +%Y%m%d_%H%M)
docker exec pia-postgres-1 pg_dump -U pia -d pia -Fc > "$BACKUP_DIR/pia_$STAMP.dump"
# rotate: keep newest 14
ls -1t "$BACKUP_DIR"/pia_*.dump 2>/dev/null | tail -n +15 | xargs -r rm --
echo "backup written: $BACKUP_DIR/pia_$STAMP.dump"
