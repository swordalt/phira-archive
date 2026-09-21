"""Archive Phira chart metadata.

  poll   - hourly: newest uploads per division, plus re-uploads of charts we have
  sweep  - weekly / backfill: walk every page of every division
"""

import argparse
import os
import sys

import phira
import store


class Run:
    """Accumulates one run's work so poll and sweep can share the write path."""

    def __init__(self, client, dry_run=False):
        self.client = client
        self.dry_run = dry_run
        self.known = store.known_ids()
        self.records = {}   # id -> record to write
        self.events = []
        self.new = 0
        self.updated = 0
        self.missing = 0
        self.failed = []

    def list_page(self, division, order, page, page_num=20):
        return self.client.list_charts(division, order=order, page=page, page_num=page_num)

    def take(self, payload, division):
        """Classify one chart payload. Returns True if it is new to the archive."""
        chart_id = payload["id"]
        seen = chart_id in self.known
        old = self.records.get(chart_id) or store.load(chart_id)
        record, changed = store.merge(old, payload, division)
        if not changed:
            return False

        self.records[chart_id] = record
        if not seen:
            self.new += 1
            self.known.add(chart_id)
        else:
            self.updated += 1
        self.events.append({
            "ts": store.now(),
            "event": "new" if not seen else "updated",
            "id": chart_id,
            "division": division,
            "changed": changed,
        })
        return not seen

    def refetch(self, chart_id, division):
        """Replace a record with the canonical /chart/{id} payload."""
        if self.dry_run:
            return
        try:
            payload = self.client.get_chart(chart_id)
        except phira.NotFound:
            self.missing += 1
            self.events.append({
                "ts": store.now(), "event": "missing", "id": chart_id,
                "division": division, "changed": [],
            })
            return
        old = self.records.get(chart_id)
        record, _ = store.merge(old, payload, division)
        if old is not None:
            record["_archive"] = old["_archive"]
        self.records[chart_id] = record

    def commit(self, state):
        if self.dry_run:
            print("dry run: would write {} record(s), {} event(s)".format(
                len(self.records), len(self.events)))
            for event in self.events[:20]:
                print("  {event} {id} ({division}) {changed}".format(**event))
            if len(self.events) > 20:
                print("  ... and {} more".format(len(self.events) - 20))
            return
        for record in self.records.values():
            store.save(record)
        store.append_events(self.events)
        counts = store.rebuild_index()
        for division, archived in counts.items():
            state["divisions"].setdefault(division, {})["archived"] = archived
        state["totalCharts"] = len(store.known_ids())
        store.save_state(state)


def divisions_arg(value):
    if not value:
        return list(phira.DIVISIONS)
    names = [name.strip() for name in value.split(",") if name.strip()]
    unknown = [name for name in names if name not in phira.DIVISIONS]
    if unknown:
        raise argparse.ArgumentTypeError("unknown division(s): " + ", ".join(unknown))
    return names


def poll(run, state, divisions, max_pages):
    """Two cheap queries per division: newest by id, then most recently updated."""
    for division in divisions:
        entry = state["divisions"].setdefault(division, {})
        try:
            fresh = []
            for page in range(1, max_pages + 1):
                count, results = run.list_page(division, "-id", page)
                entry["apiCount"] = count
                if not results:
                    break
                unseen = sum(1 for chart in results if chart["id"] not in run.known)
                if page == 1:
                    entry["maxId"] = max(entry.get("maxId", 0),
                                         max(chart["id"] for chart in results))
                for payload in results:
                    if run.take(payload, division):
                        fresh.append(payload["id"])
                # Only keep paging while the whole page was unseen (burst / downtime).
                if unseen < len(results):
                    break

            _, results = run.list_page(division, None, 1)
            for payload in results:
                if run.take(payload, division):
                    fresh.append(payload["id"])

            for chart_id in sorted(set(fresh)):
                run.refetch(chart_id, division)
        except phira.ApiError as exc:
            print("!! {}: {}".format(division, exc), file=sys.stderr)
            run.failed.append(division)
    state["lastPoll"] = store.now()


def sweep(run, state, divisions, max_pages, detail):
    """Ascending walk of every page - catches charts that became public again."""
    for division in divisions:
        entry = state["divisions"].setdefault(division, {})
        page = 0
        try:
            fresh = []
            for page in range(1, max_pages + 1):
                count, results = run.list_page(division, "id", page, phira.MAX_PAGE_NUM)
                entry["apiCount"] = count
                if not results:
                    break
                for payload in results:
                    if run.take(payload, division):
                        fresh.append(payload["id"])
                    entry["maxId"] = max(entry.get("maxId", 0), payload["id"])
                if len(results) < phira.MAX_PAGE_NUM:
                    break
            if detail:
                for chart_id in sorted(set(fresh)):
                    run.refetch(chart_id, division)
            print("   {}: {} page(s), api count {}".format(division, page, entry.get("apiCount")))
        except phira.ApiError as exc:
            print("!! {}: {}".format(division, exc), file=sys.stderr)
            run.failed.append(division)
    state["lastSweep"] = store.now()


def summary(run, mode):
    lines = ["{}: {} new, {} updated, {} missing, {} request(s)".format(
        mode, run.new, run.updated, run.missing, run.client.requests)]
    if run.failed:
        lines.append("failed divisions: " + ", ".join(run.failed))
    text = "\n".join(lines)
    print(text)

    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("new={}\nupdated={}\nmessage={}: +{} new, {} updated\n".format(
                run.new, run.updated, mode, run.new, run.updated))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("poll", "sweep"))
    parser.add_argument("--division", type=divisions_arg, default=list(phira.DIVISIONS),
                        help="comma-separated subset of: " + ", ".join(phira.DIVISIONS))
    parser.add_argument("--max-pages", type=int, default=None,
                        help="page cap per division (poll: 5, sweep: unlimited)")
    parser.add_argument("--detail", action="store_true",
                        help="sweep: also fetch /chart/{id} for every new chart")
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    parser.add_argument("--delay", type=float, default=0.3, help="seconds between requests")
    args = parser.parse_args(argv)

    client = phira.Client(delay=args.delay)
    run = Run(client, dry_run=args.dry_run)
    state = store.load_state()
    state.setdefault("divisions", {})

    if args.mode == "poll":
        poll(run, state, args.division, args.max_pages or 5)
    else:
        sweep(run, state, args.division, args.max_pages or 10 ** 6, args.detail)

    state["generated"] = store.now()
    run.commit(state)
    summary(run, args.mode)
    return 1 if run.failed else 0


if __name__ == "__main__":
    sys.exit(main())
