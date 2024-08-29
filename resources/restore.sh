#!/bin/bash

FRAPPE_PATH="/home/frappe/frappe-bench/sites"
DB_HOST=

# Check if BACKUP_FOLDER and FRAPPE_PATH are set
if [ -z "$BACKUP_FOLDER" ] || [ -z "$FRAPPE_PATH" ]; then
  echo "Both BACKUP_FOLDER environment variable must be set."
  exit 1
fi

bench_params=()
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --db-host) DB_HOST="$2"; shift ;;
        --*) bench_params+=("$1 $2"); shift ;;
        *) break ;;
    esac
    shift
done

bench_params_string="${bench_params[@]}"


# List all folders in the BACKUP_FOLDER path
echo "Please select a folder from the list below:"
folders=($(ls -d "$BACKUP_FOLDER"/*/ | sort))
select folder in "${folders[@]}"; do
    if [ -n "$folder" ] && [ -d "$folder" ]; then
        echo "You selected: $folder"
        break
    else
        echo "Invalid selection. Please try again."
    fi
done

# Copy files from the selected folder to FRAPPE_PATH
if [ -n "$folder" ]; then
    echo "Copying files from $folder to $FRAPPE_PATH..."
    cp -r "$folder"/* "$FRAPPE_PATH/"
    echo "File copy completed."

    # Check for db_backup directories and extract .sql.gz files
    for db_backup_dir in "$FRAPPE_PATH"/*/db_backup; do
        if [ -d "$db_backup_dir" ]; then
            echo "Extracting .sql.gz files in $db_backup_dir..."
            gunzip -k "$db_backup_dir"/*.sql.gz
            echo "Extraction completed in $db_backup_dir."

            SITE_FOLDER=$(dirname "$db_backup_dir")

            jq --arg db_host "$DB_HOST" '.db_host = $db_host' "$SITE_FOLDER/site_config.json" > tmp.$$.json && mv tmp.$$.json "$SITE_FOLDER/site_config.json"

            for sql_file in $(ls $db_backup_dir/*.sql | sort); do
                bench --site $(basename "$SITE_FOLDER") restore $bench_params_string $sql_file
            done
            rm -rf $db_backup_dir
        fi
    done

else
    echo "No folder selected. Operation cancelled."
    exit 1
fi