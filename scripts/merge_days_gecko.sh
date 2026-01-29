#!/bin/bash
set -euo pipefail

# Usage:
#   ./merge_days_gecko.sh /path/to/BGB4/data /path/to/output_dir
#
# Example:
#   ./concat_days.sh /Volumes/BGB4/data ./local_sds/BGB4

INPUT_DIR="$1"
OUTPUT_DIR="$2"

mkdir -p "$OUTPUT_DIR"

echo "Input directory : $INPUT_DIR"
echo "Output directory: $OUTPUT_DIR"
echo

# Traverse YYYY/MM/DD directories
find "$INPUT_DIR" -type d | while read DAYDIR; do

    # Match paths ending in /YYYY/MM/DD
    if [[ "$DAYDIR" =~ /([0-9]{4})/([0-9]{2})/([0-9]{2})$ ]]; then
        YYYY="${BASH_REMATCH[1]}"
        MM="${BASH_REMATCH[2]}"
        DD="${BASH_REMATCH[3]}"

        # Compute day of year (DOY)
        # macOS date format
        JJJ=$(date -j -f "%Y-%m-%d" "$YYYY-$MM-$DD" "+%j")

        echo "Processing day $YYYY-$MM-$DD  (DOY $JJJ)"

        OUTFILE="$OUTPUT_DIR/${YYYY}.${JJJ}.rawcat.mseed"

        # Count files
        FILECOUNT=$(find "$DAYDIR" -type f -name "*.ms" | wc -l | tr -d " ")

        if [[ "$FILECOUNT" -eq 0 ]]; then
            echo "  (No MiniSEED files found; skipping)"
            echo
            continue
        fi

        echo "  Found $FILECOUNT files"
        echo "  Writing concatenated output → $OUTFILE"

        # Create/overwrite output file
        : > "$OUTFILE"

        # Concatenate files safely (handles spaces)
        find "$DAYDIR" -type f -name "*.ms" | sort | \
        while IFS= read -r f; do
            if [[ ! -s "$f" ]]; then
                echo "    Skipping empty file: $f"
                continue
            fi
            cat "$f" >> "$OUTFILE"
        done

        echo "  Done."
        echo
    fi
done

echo "All days processed."
