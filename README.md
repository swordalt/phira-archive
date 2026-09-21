# phira-archive

An unattended archive of chart (level) metadata from [Phira](https://phira.moe)'s API.

A GitHub Actions job runs **hourly**, asks the API what is new across all four divisions, and
commits the metadata for anything it has not seen before. A **weekly** job walks every page of
every division, which catches charts that were private or delisted and later became public again.

Chart *files* are not mirrored. The API's `file`, `illustration` and `preview` URLs keep working
even for charts that have been delisted, so preserving the metadata record — which contains those
URLs — is enough to recover the assets later.

## Layout

| Path | What it is |
|---|---|
| `data/charts/<id/1000>/<id>.json` | One chart, exactly as the API returned it, plus an `_archive` key |
| `data/index.jsonl` | One compact line per chart, id-ascending — the file to grep or load |
| `data/log/YYYY-MM.jsonl` | Append-only event log: `new`, `updated`, `missing` |
| `data/state.json` | Per-division counts, high-water ids, last run times |

### Chart record

Every field the API returned, unmodified, plus:

```json
"_archive": {
  "firstSeen": "2026-09-21T01:59:46Z",
  "lastChanged": "2026-09-21T01:59:46Z",
  "divisions": ["visual"],
  "revisions": 1
}
```

A record is only rewritten when something meaningful changed — `rating` and `ratingCount` drift is
ignored, so a commit touching a chart means the chart itself changed (re-upload, rename, new
difficulty, ranked/stable status, tags). Previous versions are in git:

```bash
git log -p --follow data/charts/78/78514.json
```

### Getting the files for a chart

```bash
jq -r '.file, .illustration, .preview' data/charts/78/78514.json | xargs -n1 curl -O
```

The `file` URL is a zip containing the chart, its audio and its illustration.

## Running it by hand

Requires Python 3.9+ and nothing else — no dependencies.

```bash
python scripts/archive.py poll --dry-run          # report, write nothing
python scripts/archive.py poll                    # hourly job
python scripts/archive.py sweep                   # full walk of every division (~460 requests)
python scripts/archive.py sweep --division visual --max-pages 2
```

`--division` takes a comma-separated subset of `regular,troll,plain,visual`; `--delay` sets the
pacing between requests (default 0.3s).

## API notes

Learned by probing the live API, since it is undocumented:

- `GET /chart?division=<d>` returns `{"count": N, "results": [...]}` and each result is the **full**
  chart object — the same schema as `GET /chart/{id}`.
- `pageNum` (page size) defaults to 30 and is capped at 30; `page` is 1-based; deep pages work.
- Default sort is `-updated`. `order=-id` gives newest uploads, `order=id` ascending — the sweep
  uses ascending so pagination stays stable while charts are being uploaded mid-run.
- Errors are JSON: `404 NOT_FOUND`, `400 INVALID_INPUT`. No authentication required.

`poll` uses exactly two list queries per division: `order=-id` to find new uploads, and the default
`-updated` order to notice re-uploads of charts already archived. New charts then get one
`GET /chart/{id}` so the stored record is the canonical one.
