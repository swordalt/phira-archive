"""On-disk layout of the archive: chart records, index, event log, state."""

import json
import os
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CHARTS = os.path.join(DATA, "charts")
LOGS = os.path.join(DATA, "log")
INDEX = os.path.join(DATA, "index.jsonl")
STATE = os.path.join(DATA, "state.json")

SHARD_SIZE = 1000

# Fields that drift constantly on their own and are not worth a commit.
VOLATILE = ("rating", "ratingCount")

INDEX_FIELDS = (
    "id", "name", "level", "difficulty", "charter", "composer",
    "uploader", "created", "chartUpdated",
)


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_path(chart_id):
    return os.path.join(CHARTS, str(int(chart_id) // SHARD_SIZE), "{}.json".format(chart_id))


def known_ids():
    """Every archived chart id, from directory names only - no JSON parsing."""
    ids = set()
    if not os.path.isdir(CHARTS):
        return ids
    for shard in os.listdir(CHARTS):
        shard_dir = os.path.join(CHARTS, shard)
        if not os.path.isdir(shard_dir):
            continue
        for name in os.listdir(shard_dir):
            if name.endswith(".json"):
                ids.add(int(name[:-5]))
    return ids


def load(chart_id):
    path = record_path(chart_id)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def iter_records():
    for chart_id in sorted(known_ids()):
        record = load(chart_id)
        if record is not None:
            yield record


def save(record):
    path = record_path(record["id"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(record, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")


def changed_fields(old, new):
    """Keys that differ, ignoring archive bookkeeping and volatile rating drift."""
    skip = ("_archive",) + VOLATILE
    keys = (set(old) | set(new)) - set(skip)
    return sorted(key for key in keys if old.get(key) != new.get(key))


def merge(old, new, division):
    """Fold a freshly fetched payload into a stored record. Returns (record, changed)."""
    stamp = now()
    # division is None when a chart is fetched by id alone (e.g. a backup import).
    added = {division} if division else set()
    if old is None:
        record = dict(new)
        record["_archive"] = {
            "firstSeen": stamp,
            "lastChanged": stamp,
            "divisions": sorted(added),
            "revisions": 1,
        }
        return record, ["*new*"]

    archive = dict(old.get("_archive") or {})
    divisions = sorted(set(archive.get("divisions") or []) | added)
    changed = changed_fields(old, new)
    moved = divisions != (archive.get("divisions") or [])
    if not changed and not moved:
        return old, []

    record = dict(new)
    # Keep any extra bookkeeping (e.g. localBackup) - only the core keys move.
    record["_archive"] = dict(archive)
    record["_archive"].update({
        "firstSeen": archive.get("firstSeen", stamp),
        "lastChanged": stamp if changed else archive.get("lastChanged", stamp),
        "divisions": divisions,
        "revisions": archive.get("revisions", 1) + (1 if changed else 0),
    })
    # A backup-only chart that turns up on the API again: the live API wins.
    if archive.get("source") == "local-backup":
        record["_archive"]["source"] = "api"
    # A chart can gain a division without any other field moving; that is still
    # worth recording, so report it as a change of its own.
    return record, changed + (["_divisions"] if moved else [])


def index_line(record):
    line = {field: record.get(field) for field in INDEX_FIELDS}
    line["divisions"] = (record.get("_archive") or {}).get("divisions") or []
    return json.dumps(line, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def rebuild_index():
    """Rewrite index.jsonl from disk, id-ascending. Returns per-division counts."""
    os.makedirs(DATA, exist_ok=True)
    counts = {}
    with open(INDEX, "w", encoding="utf-8", newline="\n") as handle:
        for record in iter_records():
            handle.write(index_line(record) + "\n")
            # Backup-only charts have no known division and are counted by backup.py.
            for division in (record.get("_archive") or {}).get("divisions") or []:
                counts[division] = counts.get(division, 0) + 1
    return counts


def append_events(events):
    if not events:
        return
    os.makedirs(LOGS, exist_ok=True)
    path = os.path.join(LOGS, datetime.now(timezone.utc).strftime("%Y-%m") + ".jsonl")
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def load_state():
    if not os.path.exists(STATE):
        return {"divisions": {}}
    with open(STATE, encoding="utf-8") as handle:
        return json.load(handle)


def save_state(state):
    os.makedirs(DATA, exist_ok=True)
    with open(STATE, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(state, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
