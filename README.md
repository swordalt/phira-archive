# Phira Archive

An unofficial automated archive that scrapes chart metadata from [Phira](https://phira.moe)'s API, using GitHub Actions.

## How It Works
The archival process is split into two jobs:
- The `poll` job runs hourly, comparing the first page of results across all four divisions.
  - *There should not be more than 10-20 charts being pushed to the homepage per hour.*
- The `sweep` job runs weekly, going through all charts across all four divisions.
  - *This catches any stray charts as well as previously-delisted charts.*

Files themselves (illustration, audio preview, and chart file) are not archived. The API url for such resources remain active even after the chart is removed from Phira, meaning preserving metadata (which contains these direct URLs) is enough to recover files in the future.

The exception is charts recovered from a [local backup](#local-backups) that no longer exist on Phira: their files are kept as GitHub release assets, since there is no API URL to point to.

## Repository Layout
Archived information is found within `/data`. Everything else is for GitHub Actions to do its job.

Charts are organized into subdirectories based on their numerical IDs, mod 1000. (Ex. Find `#77769` in `/data/charts/77`.)

Files and what they are:
| Path | What it is |
|---|---|
| `/data/charts/<id%1000>/<id>.json` | One chart's metadata & archive metadata (stored in `_archive` key). |
| `/data/index.jsonl` | All chart data within the archive. For reference only; it is very large! |
| `/data/log/YYYY-MM.jsonl` | Event log: `new`, `updated`, `missing` |
| `/data/state.json` | Status: per-division count, last run time, etc. |

## Comparison
In addition to storing chart metadata from the API, the following is also kept for reference:
```json
"_archive": {
  "firstSeen": "2026-09-21T01:59:46Z",
  "lastChanged": "2026-09-21T01:59:46Z",
  "divisions": ["visual"],
  "revisions": 1
}
```

Something is rewritten upon a change, except for `rating` and `ratingCount` as those are volatile. Use git to view history:

```bash
git log -p --follow data/charts/78/78514.json
```

## Local Backups
`scripts/backup.py` imports charts from a backup of the Phira client's data folder, where each chart is a folder named by its ID containing the chart files and an in-game `info.yml`. That `info.yml` has no API data (no `file` / `preview` / `illustration` URLs), so the most recent version always wins:

1. Already archived from the API: skipped.
2. Still on Phira (`/chart/{id}` answers): archived from the live API, nothing uploaded.
3. Gone from Phira (404): metadata comes from `info.yml`, and the folder is zipped and uploaded to a release labeled with its ID range (`backup-22000-22999` = charts 22000–22999).

Records from a backup carry `"source": "local-backup"` and a `localBackup` block (release asset URL, size, sha256, file list, and the in-game-only `info.yml` fields) inside `_archive`. If such a chart ever reappears on Phira, `poll`/`sweep` overwrite it with the API version and keep the `localBackup` pointer. Commits from this script start with `backup:` and say they come from a local backup.

```bash
python -m pip install pyyaml   # backup.py only; poll/sweep stay dependency-free
python scripts/backup.py "path/to/Phira Backup" --dry-run   # report only
python scripts/backup.py "path/to/Phira Backup" --commit    # upload via gh, write, commit (no push)
```

## Phira API Notes
- `order` accepts both `id` and `-id` only via direct API calls; not within the user-facing webpage.
- `pageNum` is capped at 30.
