# EdSider AI — Duplicate Detection and Semantic Search

Finds questions the bank already has, even when they are worded
completely differently — and refuses to merge questions that only look
alike.

## Setup

```
cd D:\EdsiderAI
python -m venv venv

.\venv\Scripts\python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cpu
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

The CPU build of torch is ~250 MB instead of ~2 GB, and is plenty fast
at this scale.

## Getting real data in

There is no sample bank any more, and no default database — `EDSIDER_DB`
must be set to a real `.db` file before anything runs, or every script
refuses with a clear error rather than silently guessing:

```
$env:EDSIDER_DB = "d:\EdsiderAI\data\edsider_real.db"
```

Two ways to fill that file, both feeding the same importer logic:

**A CSV export**, produced by hand from EdSider's own database (see
`scripts/import_edsider.py`'s docstring for the export SQL):

```
.\venv\Scripts\python.exe scripts\import_edsider.py path\to\export.csv
```

**Directly from EdSider's live PostgreSQL** — the ongoing way, once a
read-only account exists:

```
cp .env.example .env         # fill in PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD
.\venv\Scripts\python.exe scripts\sync.py --full
```

Either way, then:

```
.\venv\Scripts\python.exe scripts\embed.py --language English
.\venv\Scripts\python.exe scripts\find_duplicates.py     Stage 1 only, no AI
.\venv\Scripts\streamlit.exe run app.py                  the web interface
```

CSV or JSON both work as an import source. The importer prints the
column names it found and maps common variations automatically; if
rows are skipped, edit `COLUMN_MAP` in `import_edsider.py` — that is
the only thing that should need changing.

## Run the tests

```
.\venv\Scripts\python.exe -m pytest tests\ -v
```

148 tests, none of which need the real model or a real server - vectors
are hand-made, Postgres and the embedder are faked. Runs in about two
seconds.

To check a single question against the bank, from a terminal:

```
.\venv\Scripts\python.exe scripts\check_new.py "Your question here"
```

`embed.py` downloads the model on first run, then caches it.

## Running it as a service

`check_new.py` reloads the model on every run - fine once, wrong for
anything that needs an answer repeatedly. `service.py` loads the model
and the bank's fingerprints once, keeps them in memory, and answers
requests over HTTP:

```
$env:EDSIDER_DB = "d:\EdsiderAI\data\edsider_real.db"
.\venv\Scripts\uvicorn.exe service:app --host 0.0.0.0 --port 8000
```

Then, from anywhere:

```
curl -X POST http://localhost:8000/check -H "Content-Type: application/json" -d '{
  "text": "Explain the process of photosynthesis in green plants.",
  "grade": "8", "subject": "Science"
}'
```

```json
{
  "verdict": "LIKELY DUPLICATE",
  "exact_duplicate_of": [],
  "matches": [{"id": "9011", "text": "...", "score": 0.97, "reasons": []}],
  "elapsed_ms": 157.1
}
```

Measured against the real bank: an exact match resolves in **4.6ms**
(Pass 1 only - the model is never touched), and a reworded question
resolves in **157ms** (a real embedding plus the guard layer).

`GET /health` reports what the service is currently serving against -
which database, how many comparable questions, whether the model has
finished loading - and works even before EDSIDER_DB has been set or
startup has finished, rather than crashing. `POST /reload` re-reads OUR
OWN database and rebuilds the in-memory index, without restarting the
process - it does not talk to EdSider. `GET /docs` is a form for trying
`/check` by hand in the browser.

## Keeping it in sync

`scripts/sync.py` queries EdSider's own PostgreSQL with a read-only
account and pulls only what changed since the last successful run
(tracked in OUR OWN database, not theirs) - both new questions and
which assessments (papers) they belong to. It reuses
`import_edsider.py`'s row mapping unchanged, so a Postgres row and a CSV
row are handled identically.

The account needs `SELECT` only, and the script opens its own session
read-only as a second safeguard - it never writes to EdSider's database.
`--full` ignores the watermark and pulls everything; `--reload-url`
tells a running `service.py` to pick up what was just synced.

This does not make checking "real-time" by itself - something still has
to run `sync.py`, then `embed.py`, on a schedule (cron, Task Scheduler,
or similar). See `CLAUDE.md` for what's still open with EdSider's team,
and for what "usable as a production service" still needs beyond this.

## What a real export actually looks like

Measured on the real bank - 103,337 rows, 60% Sinhala or Tamil - because
almost none of it was what a hand-written sample would suggest:

- **A question is not always a row.** A multi-part question is a *parent*
  row holding the shared stimulus, plus a *child* row per part. "How many
  links of the above food chain" means nothing without the parent that
  holds the food chain, and appears under six different ones. Parents are
  instructions — 43% of them are redundant purely because "Fill in the
  blanks" recurs. Only **standalone** rows are compared by default; linked
  sub-questions get their own, separate, cross-passage check (see
  `CLAUDE.md`).
- **Questions whose content is an image are excluded.** `"Simplify"`
  appears 110 times, each against a different expression living in an
  attachment. Scoring those on text alone is meaningless, so they are
  flagged and set aside rather than compared.
- **`NULL` arrives as a string.** Unquoted `NULL` in the CSV would become
  the text `"NULL"`, making two unset grades compare equal and every fill
  rate read 100%.
- **Cells reach 6.5 MB** (base64 images) against a default CSV field limit
  of 128 KB, and the file is streamed rather than loaded.
- **A question can belong to more than one assessment (paper).** Reusing
  the same item across two papers is normal; the same item appearing
  twice on the SAME paper is the actual problem - two different checks,
  in two different app pages, for exactly that reason.

The importer reports the shape breakdown and per-field fill rates on
every run, so which fields can be trusted is a number rather than a
guess.

Rich-text (Draft.js) fields are unwrapped automatically. For MCQs the
content field is a list — the stem followed by each option — so an MCQ
fingerprint covers the stem and its options together.

## The three stages

**Stage 1 — exact match.** Text is normalised (case, whitespace,
punctuation, Unicode form) and fingerprinted with SHA-256. Identical
questions share a fingerprint. Instant, free, perfectly accurate, no AI.

**Stage 2 — meaning fingerprints.** An embedding model turns each
question into numbers representing its meaning, so *"Calculate the area
of a rectangle 12 by 5"* and *"A rectangle has length 12 and width 5,
find its area"* are recognised as the same question despite sharing
almost no words.

**Stage 3 — the guard layer.** This is what makes the system
trustworthy.

    "Calculate the AREA of a rectangle 12 cm by 5 cm"
    "Calculate the PERIMETER of a rectangle 12 cm by 5 cm"

One word apart, same numbers, and the model scores them above 0.9. They
are not duplicates. Four guards lower a pair's score when a warning
sign appears: the numbers differ, opposing concepts are used, different
entities are named, or the grade and subject do not match. Each guard
returns a readable reason, so the reviewer sees *why* a pair was
demoted rather than a bare number.

**Measured, not assumed**: best achievable F1 without the guard layer is
0.667; with it, 1.000. On the real bank, at the threshold the sample set
suggested (0.85), guards cut candidate pairs from 1.46 million to a
number a person could actually review. See `CLAUDE.md` for the full,
repeatedly-measured story - three separate real-bank checks (the
whole-bank sweep, within-one-paper, and cross-passage) each needed a
different real threshold than first assumed, found by measuring, not by
guessing.

## Checking one new question

The three stages above sweep the whole bank against itself, which answers
*"what duplicates already exist"*. That is an audit, not the thing EdSider
does day to day — a teacher writes one question and needs to know whether
the bank already has it.

`scripts/check_new.py` runs the same three passes for a single question:

```
.\venv\Scripts\python.exe scripts\check_new.py "Work out the perimeter of a rectangle whose length is 12 cm and width is 5 cm" --grade 8 --subject Mathematics
```

```
PASS 1 - exact match (hash lookup, no AI)
  No exact match.

PASS 2 + 3 - meaning fingerprint, with safety checks

  1. LIKELY DUPLICATE   0.977   Q003
       Calculate the perimeter of a rectangle with length 12 cm and width 5 cm.

  2. DISTINCT           0.557   Q001
       Calculate the area of a rectangle with length 12 cm and width 5 cm.
       score lowered from 0.957 by the safety checks:
         - opposite concepts (perimeter vs area)

VERDICT: LIKELY DUPLICATE of Q003 (0.977) - review before adding.
```

Two details worth knowing:

- **Pass 1 runs before the model loads.** An exact match is one indexed
  lookup, so the common case never pays for an embedding at all.
- **Near misses are still shown, with the reason.** A reviewer needs to
  see what was ruled out and why, not silence.

`--add --id Q031` will store the question, but only when nothing was
flagged. Nothing is added over a flag without a person.

The scale difference is the point: sweeping 20,000 questions is 200
million comparisons, while checking one against 20,000 is 20,000 — and
usually zero, because the hash settles it.

## Interface

Five views:

- **Check a new question** — paste a draft, get a verdict before it is
  added. The same three passes as the script, in the browser.
- **Find duplicates** — pairs side by side with scores and the reasons
  any guard fired. A toggle turns the safety checks on and off and
  shows the difference in the counts above. Confirm / reject buttons
  record the verdict - and rejecting a flagged pair also captures which
  words distinguish the two questions, as raw material for later guard
  tuning.
- **Duplicates within a paper** — the same question should never appear
  twice on one assessment. Scoped, zero-tolerance, separate from the
  whole-bank sweep.
- **Search by meaning** — type a topic, get the closest questions.
  Finds *"How do green plants make their own food?"* when you search
  for photosynthesis.
- **Question bank** — the full table, filterable by subject and grade.

**Nothing is merged or deleted automatically.** The system suggests; a
person decides. Every decision is written to the `decisions` table.

## Layout

```
app.py                     Streamlit interface
service.py                 FastAPI service - the check, always on
src/
  normalize.py             text extraction, normalisation, hashing
  embeddings.py            the model wrapper
  guards.py                the four safety checks + contrast-term mining
  dedupe.py                the full pipeline (whole-bank, within-paper,
                           cross-passage)
  store.py                 all database access
scripts/
  import_edsider.py        load a real EdSider export (CSV or JSON)
  sync.py                  pull directly from EdSider's own Postgres
  embed.py                 compute fingerprints
  find_duplicates.py       Stage 1 only, no AI
  check_new.py             one question against the bank
tests/                     148 tests
data/                      the real database lives here (gitignored -
                           EDSIDER_DB must point at it explicitly)
```

## Design notes

**`store.py` is the only module that touches the database.** Moving to
PostgreSQL with pgvector means rewriting that one file. Nothing else
knows or cares what is underneath.

**No default database.** `EDSIDER_DB` must be set, or `store.connect()`
refuses to run. There used to be a 30-question sample bank to silently
fall back to; falling back to nothing at all now would mean SQLite
quietly creating a brand new empty database and every check reporting
"no duplicates" for everything, with no warning - worse than an error.

**Unicode normalisation is not decoration.** In Sinhala and Tamil the
same visible character can have more than one byte representation, and
Python's `\w` strips their vowel signs — `ලකුණු` becomes `ලකණ`. The
regex explicitly keeps the Sinhala (0D80–0DFF) and Tamil (0B80–0BFF)
blocks. Tests cover both.

**The E5 prefix is required.** `multilingual-e5-*` models need a
`query:` or `passage:` prefix on every input; omitting it degrades
quality for no reason. Handled in `Embedder._prefix()`.

**Fingerprints are cached by content hash, not question id**, so
identical text is embedded once rather than twice.

**Guards only ever lower a score, never raise it.** That means a pair
far below the threshold can be skipped before the guards run at all —
and in `check_one`, that candidates are ranked on the raw score first so
the guards only run on the handful that could matter.

**The new question is embedded as a `passage:`, not a `query:`.** The
bank was built with passage vectors and a duplicate check compares like
with like. The `query:` prefix belongs to search, where a short query
meets a longer document.

**The entity guard requires a conflict on both sides.** One question
mentioning an extra place is not evidence of difference — a test caught
that, using *"independence from British rule"* against *"independent
from Britain"*.

## Next

See `CLAUDE.md`'s "What 'usable as a service' still needs" for the full,
ordered list. In short: get real reviewer decisions flowing (everything
else below depends on it), automate `sync.py` on a schedule, re-tune
thresholds on real verdicts instead of the real bank's statistics alone,
mine the contrast list from real rejections, then embed and measure
Sinhala and Tamil.
