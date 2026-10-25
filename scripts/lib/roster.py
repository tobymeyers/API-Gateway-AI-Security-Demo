"""Parse and validate a gameday roster CSV (table_id,prompt) into a list
of {"table_id": str, "prompt": str} dicts, written out as one JSON object
per line for the calling shell script to iterate over with a plain `read`.

`parse_roster` is pure (string in, data out) and unit-tested directly
against in-memory CSV text - see tests/test_roster.py.
"""
import argparse
import csv
import io
import json
import re
import sys

# table_id ends up embedded in generated step names (run_step/
# record_bg_result interpolate it into a Python string literal inside a
# bash double-quoted -c script), DynamoDB keys, and CloudWatch dimension
# values - keep it to characters that are safe everywhere it's used
# rather than risking a quote/backslash corrupting the bookkeeping JSON.
TABLE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class RosterError(ValueError):
    pass


def parse_roster(csv_text):
    """Returns a list of {"table_id": str, "prompt": str} dicts.
    Raises RosterError with a human-readable message on any validation
    failure (missing columns, empty field, invalid table_id, no rows)."""
    reader = csv.DictReader(io.StringIO(csv_text))
    missing = {"table_id", "prompt"} - set(reader.fieldnames or [])
    if missing:
        raise RosterError(
            f"roster is missing required column(s): {sorted(missing)}. "
            f"Found columns: {reader.fieldnames}"
        )

    rows = []
    for i, row in enumerate(reader, start=2):  # 1-indexed + header line
        table_id = (row.get("table_id") or "").strip()
        prompt = (row.get("prompt") or "").strip()
        if not table_id or not prompt:
            raise RosterError(f"roster line {i} has an empty table_id or prompt.")
        if not TABLE_ID_RE.match(table_id):
            raise RosterError(
                f"roster line {i} has an invalid table_id {table_id!r} - "
                f"only letters, digits, '.', '_', '-' are allowed."
            )
        rows.append({"table_id": table_id, "prompt": prompt})

    if not rows:
        raise RosterError("roster has no data rows.")

    return rows


def find_duplicate_table_ids(rows):
    seen = set()
    return sorted({r["table_id"] for r in rows if r["table_id"] in seen or seen.add(r["table_id"])})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roster_path")
    parser.add_argument("out_path", help="Where to write the parsed roster as JSON-lines.")
    args = parser.parse_args()

    # utf-8-sig: a roster CSV saved from Excel often carries a UTF-8 BOM,
    # which would otherwise make the header check fail with a confusing
    # "missing column" error even though the column is right there.
    with open(args.roster_path, encoding="utf-8-sig") as fh:
        csv_text = fh.read()

    try:
        rows = parse_roster(csv_text)
    except RosterError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)

    dupes = find_duplicate_table_ids(rows)
    if dupes:
        print(f"WARNING: duplicate table_id(s) in roster (will run both occurrences): {dupes}", file=sys.stderr)

    with open(args.out_path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    print(f"Parsed {len(rows)} table(s) from {args.roster_path}.")


if __name__ == "__main__":
    main()
