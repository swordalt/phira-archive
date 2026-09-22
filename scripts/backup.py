"""Import charts from a local Phira client backup.

A backup of Phira's appdata keeps one folder per chart, named by its id, with
the chart, music, illustration and an in-game info.yml. That info.yml carries no
API data (no file/preview/illustration URLs), so for charts gone from Phira the
local files are the only copy left.

Priority is always the most recent version:
  1. already archived from the API   -> skipped, the API record wins
  2. /chart/{id} answers             -> archived from the live API, no upload
  3. /chart/{id} is 404              -> archived from the backup: the folder is
                                        zipped and uploaded to a GitHub release
                                        (one per 1000-id shard), the record
                                        points at it under _archive.localBackup

Needs PyYAML and an authenticated gh CLI. Local use only - not run by CI.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone

import phira
import store

SOURCE = "local-backup"

# info.yml key -> archive record key, for the fields the API also has.
MAPPED = {
    "name": "name", "level": "level", "difficulty": "difficulty",
    "charter": "charter", "composer": "composer", "illustrator": "illustrator",
    "uploader": "uploader", "tags": "tags", "intro": "description",
    "created": "created", "updated": "updated", "chartUpdated": "chartUpdated",
}
TEXT = ("name", "level", "charter", "composer", "illustrator", "intro", "tip")

ZIP_TIME = (1980, 1, 1, 0, 0, 0)  # fixed, so the same folder always zips to the same bytes


def yaml_loader():
    try:
        import yaml
    except ImportError:
        print("backup.py needs PyYAML: python -m pip install pyyaml", file=sys.stderr)
        sys.exit(2)

    class Loader(yaml.SafeLoader):
        """SafeLoader that keeps timestamps as text and only true/false as bools."""

    drop = ("tag:yaml.org,2002:timestamp", "tag:yaml.org,2002:bool")
    Loader.yaml_implicit_resolvers = {
        first: [(tag, rx) for tag, rx in resolvers if tag not in drop]
        for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    Loader.add_implicit_resolver("tag:yaml.org,2002:bool",
                                 re.compile(r"^(?:true|false)$"), list("tf"))
    return lambda text: yaml.load(text, Loader=Loader)


def shard_range(chart_id):
    low = int(chart_id) // store.SHARD_SIZE * store.SHARD_SIZE
    return low, low + store.SHARD_SIZE - 1


def release_tag(chart_id):
    return "backup-{}-{}".format(*shard_range(chart_id))


def parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class Chart:
    """One chart folder in the backup."""

    def __init__(self, path, chart_id, info):
        self.path = path
        self.id = chart_id
        self.info = info
        self.files = []  # [{"path", "size"}], sorted, forward slashes
        for root, dirs, names in os.walk(path):
            dirs.sort()
            for name in sorted(names):
                full = os.path.join(root, name)
                rel = os.path.relpath(full, path).replace(os.sep, "/")
                self.files.append({"path": rel, "size": os.path.getsize(full)})
        self.size = sum(entry["size"] for entry in self.files)

    def pack(self, out_dir):
        """Deterministic stored zip of the folder. Returns (zip path, sha256, per-file sha256)."""
        target = os.path.join(out_dir, "{}.zip".format(self.id))
        digests = {}
        with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as archive:
            for entry in self.files:
                info = zipfile.ZipInfo(entry["path"], date_time=ZIP_TIME)
                info.external_attr = 0o644 << 16
                info.file_size = entry["size"]
                digest = hashlib.sha256()
                with open(os.path.join(self.path, *entry["path"].split("/")), "rb") as src, \
                        archive.open(info, "w") as dst:
                    for block in iter(lambda: src.read(1 << 20), b""):
                        digest.update(block)
                        dst.write(block)
                digests[entry["path"]] = digest.hexdigest()
        digest = hashlib.sha256()
        with open(target, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        return target, digest.hexdigest(), digests

    def record(self, repo, packed=None, old=None):
        """Archive record built from info.yml. packed = (size, sha256, per-file digests)."""
        record = {"id": self.id}
        for key, field in MAPPED.items():
            if key in self.info:
                record[field] = self.info[key]
        rest = {key: value for key, value in self.info.items()
                if key not in MAPPED and key != "id"}

        tag = release_tag(self.id)
        asset = "{}.zip".format(self.id)
        files = [dict(entry) for entry in self.files]
        local = {
            "release": tag,
            "asset": asset,
            "url": "https://github.com/{}/releases/download/{}/{}".format(repo, tag, asset),
            "files": files,
            "info": rest,
        }
        if packed:
            local["size"], local["sha256"], digests = packed
            for entry in files:
                entry["sha256"] = digests[entry["path"]]

        stamp = store.now()
        previous = (old or {}).get("_archive") or {}
        record["_archive"] = {
            "source": SOURCE,
            "firstSeen": previous.get("firstSeen", stamp),
            "lastChanged": stamp,
            "divisions": previous.get("divisions") or [],
            "revisions": previous.get("revisions", 0) + 1,
            "localBackup": local,
        }
        return record


def scan(backup_dir, load_yaml):
    charts, skipped, broken = [], [], []
    for name in sorted(os.listdir(backup_dir), key=lambda n: (not n.isdigit(), n.zfill(12))):
        path = os.path.join(backup_dir, name)
        info_path = os.path.join(path, "info.yml")
        if not os.path.isdir(path):
            continue
        if not name.isdigit() or not os.path.isfile(info_path):
            skipped.append(name)
            continue
        try:
            with open(info_path, encoding="utf-8-sig") as handle:
                info = load_yaml(handle.read())
            if not isinstance(info, dict):
                raise ValueError("info.yml is not a mapping")
        except Exception as exc:  # noqa: BLE001 - report every bad file, keep going
            broken.append((name, "{}: {}".format(type(exc).__name__, exc).splitlines()[0]))
            continue
        # A numeric-looking string that YAML typed as a number: keep it as text.
        for key in TEXT:
            if info.get(key) is not None and not isinstance(info[key], str):
                info[key] = str(info[key])
        charts.append(Chart(path, int(name), info))
    return charts, skipped, broken


class Release:
    """Thin gh wrapper for the backup-* releases."""

    def __init__(self, repo):
        self.repo = repo
        self.assets = {}  # tag -> {name: size}, or None when the release does not exist

    def gh(self, *args):
        return subprocess.run(("gh",) + args + ("--repo", self.repo),
                              check=True, capture_output=True, text=True, encoding="utf-8")

    def existing(self, tag):
        if tag not in self.assets:
            try:
                out = self.gh("release", "view", tag, "--json", "assets").stdout
                self.assets[tag] = {a["name"]: a["size"] for a in json.loads(out)["assets"]}
            except subprocess.CalledProcessError:
                self.assets[tag] = None
        return self.assets[tag]

    def ensure(self, chart_id):
        tag = release_tag(chart_id)
        if self.existing(tag) is None:
            low, high = shard_range(chart_id)
            self.gh("release", "create", tag,
                    "--title", "Local backup: charts {}–{}".format(low, high),
                    "--notes", "Chart files for ids {}–{}, recovered from a local Phira "
                    "client backup - not from the Phira API. Only charts that no longer "
                    "exist on Phira are uploaded here. See data/charts/<shard>/<id>.json "
                    "(_archive.localBackup) for checksums.".format(low, high))
            self.assets[tag] = {}
        return tag

    def upload(self, chart_id, path, size, clobber=False):
        tag = self.ensure(chart_id)
        name = os.path.basename(path)
        if not clobber and self.assets[tag].get(name) == size:
            return False
        self.gh("release", "upload", tag, path, *(("--clobber",) if clobber else ()))
        self.assets[tag][name] = size
        return True


def default_repo():
    try:
        url = subprocess.run(("git", "remote", "get-url", "origin"), cwd=store.ROOT,
                             check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    match = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
    return match.group(1) if match else None


def classify(charts, client, offline):
    """Sort every chart by where its newest version comes from."""
    plan = {"archived-api": [], "api": [], "local": [], "archived-local": [],
            "stale-local": [], "failed": []}
    drift = 0
    known = store.known_ids()
    for count, chart in enumerate(charts, 1):
        old = store.load(chart.id) if chart.id in known else None
        from_backup = old is not None and (old.get("_archive") or {}).get("source") == SOURCE
        if old is not None and not from_backup:
            plan["archived-api"].append((chart, old, None))
            if str(old.get("updated")) != str(chart.info.get("updated")):
                drift += 1
            continue

        payload = None
        if not offline:
            try:
                payload = client.get_chart(chart.id)
            except phira.NotFound:
                pass
            except phira.ApiError as exc:
                plan["failed"].append((chart, old, str(exc)))
                continue
            if count % 100 == 0:
                print("   ... checked {} / {}".format(count, len(charts)), file=sys.stderr)

        if payload is not None:
            plan["api"].append((chart, old, payload))
        elif old is None:
            plan["local"].append((chart, old, None))
        else:
            newer = parse_time(chart.info.get("updated"))
            stored = parse_time(old.get("updated"))
            key = "archived-local" if newer and (stored is None or newer > stored) else "stale-local"
            plan[key].append((chart, old, None))
    return plan, drift


def human(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "{:.1f} {}".format(size, unit) if unit != "B" else "{} B".format(size)
        size /= 1024.0


def report(plan, drift, skipped, broken, total):
    print("scanned {} chart folder(s)".format(total))
    print("  archived-api   {:5}  skipped - archive already has the API version"
          " ({} with a different 'updated' in the backup)".format(len(plan["archived-api"]), drift))
    print("  api            {:5}  alive on Phira - archived from the live API".format(len(plan["api"])))
    print("  local          {:5}  404 on Phira - archived from the backup".format(len(plan["local"])))
    print("  archived-local {:5}  earlier backup import, this backup is newer - re-imported".format(
        len(plan["archived-local"])))
    print("  stale-local    {:5}  earlier backup import, not newer - skipped".format(
        len(plan["stale-local"])))
    print("  failed         {:5}  API error - skipped, rerun later".format(len(plan["failed"])))
    for chart, _, error in plan["failed"][:10]:
        print("     {} {}".format(chart.id, error))
    if skipped:
        print("  not a chart folder: {}".format(", ".join(skipped[:20])))
    if broken:
        print("  unreadable info.yml: {}".format(len(broken)))
        for name, error in broken[:20]:
            print("     {} {}".format(name, error))

    uploads = plan["local"] + plan["archived-local"]
    if uploads:
        shards = {}
        for chart, _, _ in uploads:
            entry = shards.setdefault(release_tag(chart.id), [0, 0])
            entry[0] += 1
            entry[1] += chart.size
        print("uploads: {} chart(s), {}".format(len(uploads), human(sum(c.size for c, _, _ in uploads))))
        for tag in sorted(shards, key=lambda t: int(t.split("-")[1])):
            print("  {:22} {:4} chart(s) {:>10}".format(tag, shards[tag][0], human(shards[tag][1])))
        largest = sorted(uploads, key=lambda item: item[0].size, reverse=True)[:5]
        print("  largest: " + ", ".join("{} ({})".format(c.id, human(c.size)) for c, _, _ in largest))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("backup_dir", help="folder holding one sub-folder per chart id")
    parser.add_argument("--dry-run", action="store_true", help="report, write and upload nothing")
    parser.add_argument("--offline", action="store_true",
                        help="dry run only: skip the API check, treat unarchived charts as local")
    parser.add_argument("--hash", action="store_true",
                        help="dry run: also zip + sha256 every upload into a temp dir")
    parser.add_argument("--commit", action="store_true", help="git commit data/ afterwards")
    parser.add_argument("--delay", type=float, default=0.3, help="seconds between API requests")
    parser.add_argument("--repo", default=None, help="OWNER/NAME for releases (default: origin)")
    args = parser.parse_args(argv)
    # Chart names are mostly CJK; a Windows console defaults to cp1252.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if args.offline and not args.dry_run:
        parser.error("--offline is only allowed with --dry-run: the live API must be checked "
                     "before anything is archived from a backup")
    if not os.path.isdir(args.backup_dir):
        parser.error("not a directory: " + args.backup_dir)
    repo = args.repo or default_repo()
    if not repo:
        parser.error("could not work out the GitHub repo from origin; pass --repo OWNER/NAME")

    charts, skipped, broken = scan(args.backup_dir, yaml_loader())
    client = phira.Client(delay=args.delay)
    plan, drift = classify(charts, client, args.offline)
    report(plan, drift, skipped, broken, len(charts) + len(broken))

    work = tempfile.mkdtemp(prefix="phira-backup-")
    records, events = [], []
    try:
        for chart, old, payload in plan["api"]:
            record, changed = store.merge(old, payload, None)
            if changed:
                records.append(record)
                events.append({"ts": store.now(), "event": "new" if old is None else "updated",
                               "id": chart.id, "division": None, "changed": changed,
                               "source": "api"})

        uploads = [(c, o, False) for c, o, _ in plan["local"]] + \
                  [(c, o, True) for c, o, _ in plan["archived-local"]]
        release = None if args.dry_run else Release(repo)
        uploaded = 0
        for count, (chart, old, clobber) in enumerate(uploads, 1):
            packed = None
            if not args.dry_run or args.hash:
                path, digest, digests = chart.pack(work)
                size = os.path.getsize(path)
                packed = (size, digest, digests)
                if release is not None:
                    if release.upload(chart.id, path, size, clobber=clobber):
                        uploaded += 1
                    print("   [{}/{}] {} -> {}".format(count, len(uploads), chart.id,
                                                      release_tag(chart.id)), file=sys.stderr)
                os.remove(path)
            record = chart.record(repo, packed, old)
            records.append(record)
            events.append({"ts": store.now(), "event": "new" if old is None else "updated",
                           "id": chart.id, "division": None,
                           "changed": ["*new*"] if old is None else store.changed_fields(old, record),
                           "source": SOURCE})
            # Save as we go: the asset is up, so the record may point at it.
            if not args.dry_run:
                store.save(record)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    local_count = len(plan["local"]) + len(plan["archived-local"])
    if args.dry_run:
        print("dry run: would write {} record(s), {} event(s)".format(len(records), len(events)))
        for event in events[:20]:
            print("  {event} {id} [{source}] {changed}".format(**event))
        if len(events) > 20:
            print("  ... and {} more".format(len(events) - 20))
        sample = next((r for r in records if r["_archive"].get("source") == SOURCE), None)
        if sample:
            sample = json.loads(json.dumps(sample))
            files = sample["_archive"]["localBackup"]["files"]
            if len(files) > 4:
                sample["_archive"]["localBackup"]["files"] = files[:4] + ["... {} more".format(len(files) - 4)]
            print("sample record:")
            print(json.dumps(sample, indent=2, ensure_ascii=False, sort_keys=True))
        return 0

    for record in records:
        if record["_archive"].get("source") != SOURCE:
            store.save(record)
    store.append_events(events)
    state = store.load_state()
    counts = store.rebuild_index()
    for division, archived in counts.items():
        state.setdefault("divisions", {}).setdefault(division, {})["archived"] = archived
    state["totalCharts"] = len(store.known_ids())
    state["localBackup"] = {
        "archived": sum(1 for r in store.iter_records()
                        if (r.get("_archive") or {}).get("source") == SOURCE),
        "lastImport": store.now(),
    }
    state["generated"] = store.now()
    store.save_state(state)
    print("wrote {} record(s): {} from the live API, {} from the backup ({} uploaded)".format(
        len(records), len(records) - local_count, local_count, uploaded))

    if args.commit:
        commit(args.backup_dir, local_count, len(records) - local_count,
               sorted({release_tag(c.id) for c, _, _ in uploads}, key=lambda t: int(t.split("-")[1])))
    return 1 if plan["failed"] else 0


def commit(backup_dir, local, live, tags):
    subprocess.run(("git", "add", "-A", "data"), cwd=store.ROOT, check=True)
    if subprocess.run(("git", "diff", "--cached", "--quiet"), cwd=store.ROOT).returncode == 0:
        print("nothing to commit")
        return
    subject = "backup: +{} new from local backup".format(local)
    if live:
        subject += ", +{} from live API".format(live)
    subject += " (files in GitHub releases, not from Phira API) ({})".format(
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"))
    body = ("Imported from a local Phira client backup ({}).\n"
            "Charts from the backup exist only as local files: metadata comes from the\n"
            "in-game info.yml, and the files are in these releases:\n{}").format(
        os.path.basename(os.path.normpath(backup_dir)),
        "\n".join("  " + tag for tag in tags) or "  (none)")
    subprocess.run(("git", "commit", "-m", subject, "-m", body), cwd=store.ROOT, check=True)
    print("committed - review, then git push")


if __name__ == "__main__":
    sys.exit(main())
