#!/bin/bash

: "${BACKUP_FOLDER:?BACKUP_FOLDER is not set.}"

BACKUP_NAME=$(date +%Y%m%d%H%M%S%3N)
BACKUP_PATH="$BACKUP_FOLDER/$BACKUP_NAME"
FRAPPE_PATH="/home/frappe/frappe-bench/sites"

mkdir -p $BACKUP_PATH

cp $FRAPPE_PATH/common_site_config.json $BACKUP_PATH/common_site_config.json

for dir in "$FRAPPE_PATH"/*; do
    if [ -d "$dir" ] && [ "$(basename "$dir")" != "assets" ]; then
        bench --site $(basename "$dir") backup --backup-path "$dir/db_backup";
        find "$dir/db_backup" -type f -name "*.json" -exec rm -f {} +
        cp -r "$dir" "$BACKUP_PATH/"
        rm -rf "$BACKUP_PATH/$(basename "$dir")/locks"
        rm -rf "$BACKUP_PATH/$(basename "$dir")/logs"
        rm -rf "$dir/db_backup"
    fi
done

ls -d $BACKUP_FOLDER/* | sort -r | tail -n +$(( ${MAX_BACKUP_COUNT:-4} + 1 )) | xargs -r rm -rf

ls -l $BACKUP_PATH
echo "Backup completed [$BACKUP_PATH]"