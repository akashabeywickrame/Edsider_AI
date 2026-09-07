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

## Run

```
.\venv\Scripts\python.exe -m pytest tests\ -v      106 tests
.\venv\Scripts\python.exe scripts\load.py          load sample questions
.\venv\Scripts\python.exe scripts\embed.py         compute fingerprints
.\venv\Scripts\python.exe scripts\find_similar.py  command-line results

.\venv\Scripts\streamlit.exe run app.py            the web interface
```

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

Measured against the real 15,998-question bank: an exact match resolves
in **4.6ms** (Pass 1 only - the model is never touched), and a reworded
question resolves in **157ms** (a real embedding plus the guard layer).

`GET /health` reports what the service is currently serving against -
which database, how many comparable questions, whether the model has
finished loading. `POST /reload` re-reads OUR OWN database and rebuilds
the in-memory index, without restarting the process - it does not talk
to EdSider. `GET /docs` is a form for trying `/check` by hand in the
browser.

## Pulling directly from EdSider's database

```
cp .env.example .env         # fill in PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD
.\venv\Scripts\python.exe scripts\sync.py
.\venv\Scripts\python.exe scripts\embed.py
```

This replaces the manual CSV export for ongoing use - it queries
EdSider's own PostgreSQL with a read-only account and pulls only
questions newer than the last successful sync (tracked in our own
database, not theirs). It reuses `import_edsider.py`'s row mapping
unchanged, so a Postgres row and a CSV row are handled identically.

The account needs `SELECT` only, and the script opens its own session
read-only as a second safeguard - it never writes to EdSider's database.
`--full` ignores the watermark and pulls everything; `--reload-url`
tells a running `service.py` to pick up what was just synced.

This does not make checking "real-time" by itself - something still has
to run `sync.py` on a schedule (cron, Task Scheduler, or similar). See
`CLAUDE.md` for what's still open with EdSider's team before this can
run against their real database rather than a local test.

This is a local service, not yet a deployed one - see "What 'usable as a
service' still needs" in `CLAUDE.md` for what stands between this and
something EdSider's own save button could call.

## Using real EdSider data

Keep the real bank in its own database, or `tune.py` will score tens of
thousands of real questions against a 30-row answer key:

```
$env:EDSIDER_DB = "d:\EdsiderAI\data\edsider_real.db"

.\venv\Scripts\python.exe scripts\import_edsider.py data\edsider_questions.csv
.\venv\Scripts\python.exe scripts\embed.py
.\venv\Scripts\python.exe scripts\find_duplicates.py
.\venv\Scripts\streamlit.exe run app.py
```

Unset, everything falls back to the sample bank. The export command
itself lives in the `scripts/import_edsider.py` docstring, next to the
code that reads it.

CSV or JSON both work. The importer prints the column names it found and
maps common variations automatically. If rows are skipped, edit
`COLUMN_MAP` — that is the only thing that should need changing.

### What a real export actually looks like

Measured on a 684 MB, 41,139-row export, because almost none of it
matched what the sample set implied:

- **A question is not always a row.** A multi-part question is a *parent*
  row holding the shared stimulus, plus a *child* row per part. "How many
  links of the above food chain" means nothing without the parent that
  holds the food chain, and appears under six different ones. Parents are
  instructions — 43% of them are redundant purely because "Fill in the
  blanks" recurs. Only **standalone** rows are compared.
- **Questions whose content is an image are excluded.** `"Simplify"`
  appears 110 times, each against a different expression living in an
  attachment. Scoring those on text alone is meaningless, so they are
  flagged and set aside rather than compared.
- **`NULL` arrives as a string.** Unquoted `NULL` in the CSV would become
  the text `"NULL"`, making two unset grades compare equal and every fill
  rate read 100%.
- **Cells reach 6.5 MB** (base64 images) against a default CSV field limit
  of 128 KB, and the file is streamed rather than loaded.

The importer reports the shape breakdown and per-field fill rates on
every run, so which fields can be trusted is a number rather than a
guess. Guard 4 needs grade and subject — on the real bank those are 95.5%
and 99.9% populated across the comparable rows.

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

**On the sample data, the guards take flagged pairs from 13 down to 4** —
removing exactly the pairs that are not duplicates. The web interface
has a toggle so you can demonstrate that live.

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

Four *area* questions scored 0.933–0.957 on the raw fingerprint and were
all demoted; the one real match surfaced above them. That is the guard
layer doing its job on a single question rather than on a whole sweep.

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

Four views:

- **Check a new question** — paste a draft, get a verdict before it is
  added. The same three passes as the script, in the browser.
- **Find duplicates** — pairs side by side with scores and the reasons
  any guard fired. A toggle turns the safety checks on and off and
  shows the difference in the counts above. Confirm / reject buttons
  record the verdict.
- **Search by meaning** — type a topic, get the closest questions.
  Finds *"How do green plants make their own food?"* when you search
  for photosynthesis.
- **Question bank** — the full table, filterable by subject and grade.

**Nothing is merged or deleted automatically.** The system suggests; a
person decides. Every decision is written to the `decisions` table,
which becomes the data for tuning thresholds later.

## Layout

```
app.py                     Streamlit interface
src/
  normalize.py             text extraction, normalisation, hashing
  embeddings.py            the model wrapper
  guards.py                the four safety checks
  dedupe.py                the full pipeline
  store.py                 all database access
scripts/
  load.py                  load sample questions
  import_edsider.py        load a real EdSider export
  embed.py                 compute fingerprints
  find_duplicates.py       Stage 1 only
  find_similar.py          Stages 1-2, self-scoring
  check_new.py             one question against the bank
  tune.py                  threshold sweep, with vs without guards
tests/                     106 tests
data/
  questions.json           30 sample questions, duplicates planted
  ground_truth.json        the answer key
```

## Design notes

**`store.py` is the only module that touches the database.** Moving to
PostgreSQL with pgvector means rewriting that one file. Nothing else
knows or cares what is underneath.

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

- Tune the thresholds against real labelled pairs from EdSider
- Learn the contrast list from the decision log instead of maintaining
  it by hand
- Test on Sinhala and Tamil questions
- Wrap `check_one` in a save-time endpoint with a warm model, so the
  check happens where questions are actually added
- Swap the brute-force sweep for a pgvector HNSW index once the bank
  passes a few thousand questions
