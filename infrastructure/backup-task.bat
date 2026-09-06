@echo off
REM PIA daily database backup (rotation + logging inside backup-db.sh)
"C:\Program Files\Git\bin\bash.exe" -lc "cd /e/agent/placement-intelligence/infrastructure && ./backup-db.sh >> ../backups/backup.log 2>&1"
