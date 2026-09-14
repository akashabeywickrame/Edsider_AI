"""EdSider AI - duplicate detection and semantic search.

Run with:   streamlit run app.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src import store, dedupe
from src.dedupe import guards
from src.dedupe.embeddings import DEFAULT_MODEL
from src.intake import extract as intake_extract
from src.intake import review as intake_review
from src.normalize import content_hash

st.set_page_config(page_title="EdSider AI", layout="wide")


# ---------------------------------------------------------------- data

# Keyed on the database name so switching banks invalidates the cache
# rather than showing the previous one's questions.
@st.cache_data(ttl=5)
def load_rows(db_name):
    with store.connect() as conn:
        rows = store.comparable_questions(conn, DEFAULT_MODEL)
        total = store.count(conn)
    return [dict(r) for r in rows], total


@st.cache_resource
def get_embedder():
    from src.dedupe.embeddings import Embedder
    return Embedder()


@st.cache_data(ttl=300)
def sweep(db_name, threshold, use_guards):
    """A full sweep is expensive on a real bank - cache it per setting."""
    rows, _ = load_rows(db_name)
    return dedupe.find_pairs(rows, use_guards=use_guards, threshold=threshold)


@st.cache_data(ttl=15)
def load_assessment_groups(db_name):
    """Comparable, fingerprinted questions grouped by the paper they're
    on. Only groups of more than one question can possibly contain a
    within-paper repeat, so smaller ones are dropped here rather than
    compared for nothing."""
    with store.connect() as conn:
        ids = store.assessment_ids(conn)
        groups = {
            aid: [dict(r) for r in
                 store.questions_in_assessment(conn, aid, DEFAULT_MODEL)]
            for aid in ids
        }
    return {aid: rs for aid, rs in groups.items() if len(rs) > 1}


@st.cache_data(ttl=15)
def other_assessments(db_name, qid):
    """Which papers a question appears on - informational context, not a
    flag. The same question on two different papers is normal; it is
    only a problem when it repeats on the SAME paper (see the
    'Duplicates within a paper' page)."""
    with store.connect() as conn:
        return [dict(r) for r in store.assessments_for_question(conn, qid)]


def assessment_caption(qid):
    links = other_assessments(store.DB_PATH.name, qid)
    if not links:
        return
    where = ", ".join(f"#{l['assessment_id']}" for l in links)
    st.caption(f"Also appears on assessment(s): {where}")


def title_caption(q):
    """The per-question code EdSider stores against this question - a
    label for a reviewer to cross-reference against the paper, never
    fingerprinted and never part of the comparison itself."""
    title = q.get("title")
    if title:
        st.caption(f"Title: {title}")


# ------------------------------------------------------------- sidebar

st.sidebar.title("EdSider AI")

# Which bank is open has to be visible and switchable - launching
# without EDSIDER_DB set should not be a crash just because store.py
# refuses to guess a database on its own (see store.py's DB_PATH
# comment). If nothing has been configured yet but a bank exists on
# disk, open the first one found rather than leave DB_PATH as None.
#
# The bank EDSIDER_DB names comes first, wherever it is. On a server it is
# usually not under data/ at all - startup.sh serves a copy from local
# disk - and looking only in data/ reported "no database found" right
# next to a perfectly good one.
bank_paths = {}
if os.environ.get("EDSIDER_DB") and Path(os.environ["EDSIDER_DB"]).is_file():
    configured = Path(os.environ["EDSIDER_DB"])
    bank_paths[configured.name] = configured
for p in sorted((ROOT / "data").glob("*.db")):
    bank_paths.setdefault(p.name, p)
banks = list(bank_paths)

if banks and store.DB_PATH is None:
    store.use_database(bank_paths[banks[0]])

if not banks:
    st.title("EdSider AI")
    st.error(
        "No database found - neither `EDSIDER_DB` nor `data/*.db` points "
        "at one. Build one first:\n\n"
        "```\n$env:EDSIDER_DB = \"data/edsider_real.db\"\n"
        "python scripts/sync.py --full\n"
        "python scripts/embed.py --language English\n```"
    )
    st.stop()

current = store.DB_PATH.name
chosen = st.sidebar.selectbox(
    "Question bank", banks,
    index=banks.index(current) if current in banks else 0,
)
if chosen != store.DB_PATH.name:
    store.use_database(bank_paths[chosen])
    st.rerun()

page = st.sidebar.radio(
    "View",
    ["Check a new question", "Find duplicates", "Duplicates within a paper",
     "Search by meaning", "Question bank", "Paper intake"],
)

rows, total_questions = load_rows(store.DB_PATH.name)

st.sidebar.divider()
st.sidebar.caption(f"Database: {store.DB_PATH.name}")
st.sidebar.caption(f"{total_questions:,} questions loaded")
st.sidebar.caption(f"{len(rows):,} comparable, with fingerprints")
st.sidebar.caption(f"Model: {DEFAULT_MODEL.split('/')[-1]}")

if not rows:
    st.title("EdSider AI")
    st.error(
        f"**{store.DB_PATH.name}** holds {total_questions:,} questions but "
        "none of them have fingerprints yet.\n\n"
        "Either pick another bank in the sidebar, or build them:\n\n"
        "```\npython scripts/embed.py\n```"
    )
    st.stop()


# ------------------------------------------------ check a new question

if page == "Check a new question":
    st.title("Check a new question")
    st.caption(
        "Before adding a question, check whether the bank already has "
        "it. This compares one question against the bank, which is what "
        "happens at save time - not a full sweep."
    )

    draft = st.text_area(
        "The question you are about to add",
        height=110,
        placeholder="Type or paste a question...",
    )

    c1, c2, c3 = st.columns(3)
    grade = c1.selectbox(
        "Grade", [""] + sorted({r["grade"] for r in rows if r["grade"]})
    )
    subject = c2.selectbox(
        "Subject", [""] + sorted({r["subject"] for r in rows if r["subject"]})
    )
    check_guards = c3.toggle("Safety checks ON", value=True, key="check_guards")

    if draft.strip():
        new_hash = content_hash(draft)

        # Pass 1 first. An exact match costs one indexed lookup and
        # makes the model unnecessary, so never load it before trying.
        with store.connect() as conn:
            exact = [dict(r) for r in store.find_by_hash(conn, new_hash)]

        st.divider()

        if exact:
            st.error(
                f"**Exact duplicate** of "
                f"{', '.join(r['id'] for r in exact)}. This question is "
                "already in the bank, word for word."
            )
            for r in exact:
                st.markdown(f"**{r['id']}**")
                title_caption(r)
                st.write(r["text"])
                st.caption(
                    f"Grade {r['grade']} - {r['subject']} - "
                    f"{r['topic'] or 'untagged'}"
                )
                assessment_caption(r["id"])
            st.info(
                "Matched by hash alone. No embedding was computed - the "
                "cheapest pass settled it."
            )
        else:
            with st.spinner("Comparing against the bank..."):
                embedder = get_embedder()
                # passage, not query: the bank was embedded as passages,
                # and a duplicate check compares like with like.
                qvec = embedder.encode(draft, is_query=False)[0]
                candidate = {
                    "text": draft,
                    "vector": qvec,
                    "grade": grade or None,
                    "subject": subject or None,
                }
                matches = dedupe.check_one(
                    candidate, rows, use_guards=check_guards
                )

            flagged = [m for m in matches
                       if m["score"] >= dedupe.REVIEW_THRESHOLD]

            if not flagged:
                st.success(
                    "**No duplicate found.** Nothing in the bank is close "
                    "enough to worry about."
                )
                if matches:
                    st.info(
                        f"{len(matches)} question(s) scored high before the "
                        "safety checks and were ruled out. They are shown "
                        "below with the reason."
                    )
            else:
                top = flagged[0]
                st.error(
                    f"**{top['verdict']}** of {top['match']['id']} at "
                    f"{top['score']:.0%}. Review this before adding."
                )

            if not matches:
                st.caption("No question in the bank scored above the threshold.")

            for m in matches:
                q = m["match"]
                colour = {
                    "LIKELY DUPLICATE": "red",
                    "NEEDS REVIEW": "orange",
                    "DISTINCT": "green",
                }[m["verdict"]]

                st.markdown(
                    f"### :{colour}[{m['verdict']}] &nbsp; "
                    f"{m['score']:.0%} similar &nbsp; - &nbsp; {q['id']}"
                )
                title_caption(q)
                st.write(q["text"])
                st.caption(
                    f"Grade {q['grade']} - {q['subject']} - "
                    f"{q['topic'] or 'untagged'}"
                )
                assessment_caption(q["id"])
                if m["reasons"]:
                    st.warning(
                        "**Safety checks fired** (score lowered from "
                        f"{m['raw_score']:.0%} to {m['score']:.0%}):\n\n"
                        + "\n".join(f"- {r}" for r in m["reasons"])
                    )
                st.divider()


# ----------------------------------------------------- find duplicates

elif page == "Find duplicates":
    st.title("Find duplicate questions")

    # 0.85 was tuned on 30 sample questions. On the real bank it yields
    # about 1.5 million candidate pairs, which is neither reviewable nor
    # renderable, so a large bank starts high.
    big = len(rows) > 1000
    default_threshold = 0.95 if big else 0.85

    col1, col2 = st.columns([3, 2])
    with col1:
        use_guards = st.toggle(
            "Safety checks ON",
            value=True,
            help="Turn this off to see what happens without the guard layer.",
        )
    with col2:
        threshold = st.slider(
            "Similarity threshold", 0.70, 0.99, default_threshold, 0.01
        )

    if big and threshold < 0.92:
        st.warning(
            f"At {threshold:.2f} a bank of {len(rows):,} questions produces "
            "tens of thousands of pairs, and below 0.90 well over a "
            "million. Raise the threshold, or expect this to take a while."
        )

    with st.spinner(f"Comparing {len(rows):,} questions..."):
        with_guards = sweep(store.DB_PATH.name, threshold, True)
        without_guards = sweep(store.DB_PATH.name, threshold, False)

    # -- the comparison that matters --
    a, b, c = st.columns(3)
    a.metric("Pairs flagged, safety checks ON", f"{len(with_guards):,}")
    b.metric("Pairs flagged, safety checks OFF", f"{len(without_guards):,}")
    removed = len(without_guards) - len(with_guards)
    c.metric("Removed by safety checks", f"{removed:,}")

    if removed > 0:
        st.info(
            f"The safety checks removed **{removed:,}** pairs that look "
            "almost identical but test different things. Without them "
            "those would be shown to a teacher as duplicates."
        )

    st.divider()

    results = with_guards if use_guards else without_guards
    by_id = {r["id"]: r for r in rows}

    if not results:
        st.success("No possible duplicates found at this threshold.")

    # Rendering thousands of pairs makes the page unusable. Highest
    # scores first, so the top of the list is where a reviewer starts.
    SHOW = 50
    if len(results) > SHOW:
        st.caption(
            f"Showing the {SHOW} highest-scoring of {len(results):,} pairs. "
            "Raise the threshold to narrow this down."
        )

    for pair in results[:SHOW]:
        qa, qb = by_id[pair["a"]], by_id[pair["b"]]

        colour = {
            "LIKELY DUPLICATE": "red",
            "NEEDS REVIEW": "orange",
            "DISTINCT": "green",
        }[pair["verdict"]]

        st.markdown(
            f"### :{colour}[{pair['verdict']}] &nbsp; "
            f"{pair['score']:.0%} similar"
        )

        left, right = st.columns(2)
        for col, q in ((left, qa), (right, qb)):
            with col:
                st.markdown(f"**{q['id']}**")
                title_caption(q)
                st.write(q["text"])
                st.caption(
                    f"Grade {q['grade']} - {q['subject']} - "
                    f"{q['topic'] or 'untagged'}"
                )
                st.caption(f"by {q['contributor'] or 'unknown'}")
                assessment_caption(q["id"])

        if pair["reasons"]:
            st.warning(
                "**Safety checks fired** (score lowered from "
                f"{pair['raw_score']:.0%} to {pair['score']:.0%}):\n\n"
                + "\n".join(f"- {r}" for r in pair["reasons"])
            )

        d1, d2, d3, _ = st.columns([1, 1, 1, 3])
        key = f"{pair['a']}_{pair['b']}"
        if d1.button("Confirm duplicate", key=f"dup_{key}"):
            with store.connect() as conn:
                store.record_decision(
                    conn, pair["a"], pair["b"], "duplicate", "reviewer",
                    pair["score"], "; ".join(pair["reasons"]),
                )
            st.success("Recorded as duplicate.")
        if d2.button("Not a duplicate", key=f"not_{key}"):
            # The system flagged these as similar - whatever vocabulary
            # differs between them is a candidate reason why they are
            # not the same question, worth capturing for later guard
            # tuning even though nothing acts on it automatically.
            only_a, only_b = guards.candidate_contrast_terms(qa["text"], qb["text"])
            with store.connect() as conn:
                store.record_decision(
                    conn, pair["a"], pair["b"], "distinct", "reviewer",
                    pair["score"], "; ".join(pair["reasons"]),
                    candidate_contrast_terms=(only_a, only_b),
                )
            st.success("Recorded as distinct.")
            if only_a or only_b:
                st.caption(
                    f"Captured as a candidate distinguishing pair: "
                    f"{', '.join(only_a[:5]) or '(none)'} vs "
                    f"{', '.join(only_b[:5]) or '(none)'}"
                )
        d3.button("Skip", key=f"skip_{key}")

        st.divider()


# ------------------------------------------------ duplicates within a paper

elif page == "Duplicates within a paper":
    st.title("Duplicates within a paper")
    st.caption(
        "The same question should never appear twice on one assessment - "
        "MCQ, structured or essay. This checks each paper only against "
        "itself, not the rest of the bank, and does not use the safety "
        "checks: a paper is small enough that a false alarm costs nothing "
        "to dismiss, and a repeat here is a real problem even with "
        "different numbers plugged in - which is exactly what a safety "
        "check would otherwise excuse. The same question turning up on a "
        "DIFFERENT paper is normal and is not what this checks; see the "
        "'Also appears on assessment(s)' note on the other pages for that."
    )

    groups_all = load_assessment_groups(store.DB_PATH.name)

    if not groups_all:
        st.info(
            "No assessment links with more than one comparable question "
            "were found. Either scripts/sync.py has not pulled "
            "assessment.assessment_question yet, or every synced paper "
            "has zero or one comparable question on it."
        )
    else:
        # Filters narrow WHICH questions are compared, not just which
        # results are shown - a row that fails a filter is dropped from
        # its paper's group before find_pairs ever sees it, so a group
        # left with only one question after filtering is correctly
        # skipped rather than reported as having "0 repeats".
        all_rows = [r for rs in groups_all.values() for r in rs]

        f1, f2, f3, f4 = st.columns(4)
        grade_f = f1.selectbox(
            "Grade",
            ["All"] + sorted({r["grade"] for r in all_rows if r["grade"]}),
        )
        subject_f = f2.selectbox(
            "Subject",
            ["All"] + sorted({r["subject"] for r in all_rows if r["subject"]}),
        )
        title_f = f3.text_input(
            "Title code contains", placeholder="e.g. Q3-2024"
        )
        assessment_options = sorted(
            groups_all.keys(),
            key=lambda x: (0, int(x)) if str(x).isdigit() else (1, str(x)),
        )
        assessment_f = f4.selectbox("Assessment #", ["All"] + assessment_options)

        def _matches(r):
            if grade_f != "All" and r["grade"] != grade_f:
                return False
            if subject_f != "All" and r["subject"] != subject_f:
                return False
            if title_f and title_f.lower() not in (r.get("title") or "").lower():
                return False
            return True

        groups = {}
        for aid, rs in groups_all.items():
            if assessment_f != "All" and aid != assessment_f:
                continue
            kept = [r for r in rs if _matches(r)]
            if len(kept) > 1:
                groups[aid] = kept

        st.divider()

        if not groups:
            st.warning(
                "No paper has more than one comparable question matching "
                "these filters."
            )
        else:
            with st.spinner(f"Checking {len(groups):,} papers..."):
                results = dedupe.find_within_assessment_duplicates(groups)
                # Informational only - does not change results or scores.
                # See dedupe.find_shared_passage_groups() docstring for why
                # this is content-based (a real shared block) rather than
                # "consecutive question numbers": numbering has gaps (the
                # #1127 pair below is Q31/Q33, not Q31/Q32), and two
                # genuinely copy-pasted consecutive questions would be
                # exactly the mistake this page exists to catch, not
                # something to wave through on position alone.
                passage_groups = {
                    aid: dedupe.find_shared_passage_groups(rs)
                    for aid, rs in groups.items()
                }

            def shared_passage_group(aid, qid):
                for g in passage_groups.get(aid, []):
                    if qid in g["question_ids"]:
                        return g
                return None

            a, b = st.columns(2)
            a.metric("Papers checked", f"{len(groups):,}")
            b.metric("Repeats found", f"{len(results):,}")

            st.divider()

            if not results:
                st.success("No paper repeats a question.")

            by_assessment = {aid: {r["id"]: r for r in rs}
                             for aid, rs in groups.items()}

            for pair in results:
                aid = pair["assessment_id"]
                by_id = by_assessment[aid]
                qa, qb = by_id[pair["a"]], by_id[pair["b"]]

                st.markdown(
                    f"### :red[REPEAT ON ASSESSMENT #{aid}] &nbsp; "
                    f"{pair['score']:.0%} similar"
                )

                cluster = shared_passage_group(aid, pair["a"])
                if cluster:
                    nums = ", ".join(str(n) for n in cluster["question_numbers"])
                    st.info(
                        "Shares a long passage with question(s) "
                        f"{nums or 'unnumbered'} on this paper - looks like "
                        "a shared-passage question family (the same "
                        "passage or statement set, a different question "
                        "asked each time), not necessarily a real "
                        "duplicate. Check the actual questions below "
                        "before confirming."
                    )

                left, right = st.columns(2)
                for col, q in ((left, qa), (right, qb)):
                    with col:
                        # EdSider stores a sentinel (int32 max) on some
                        # rows rather than leaving question_number null -
                        # not a real position, so it is not worth showing.
                        num = q.get("question_number")
                        if num is not None and num > 100_000:
                            num = None
                        st.markdown(
                            f"**{q['id']}**"
                            + (f" (question {num} on this paper)" if num else "")
                        )
                        title_caption(q)
                        st.write(q["text"])
                        st.caption(
                            f"Grade {q['grade']} - {q['subject']} - "
                            f"{q['topic'] or 'untagged'}"
                        )

                d1, d2, d3, _ = st.columns([1, 1, 1, 3])
                key = f"paper_{aid}_{pair['a']}_{pair['b']}"
                if d1.button("Confirm duplicate", key=f"dup_{key}"):
                    with store.connect() as conn:
                        store.record_decision(
                            conn, pair["a"], pair["b"], "duplicate", "reviewer",
                            pair["score"], "within-assessment repeat",
                        )
                    st.success("Recorded as duplicate.")
                if d2.button("Not a duplicate", key=f"not_{key}"):
                    only_a, only_b = guards.candidate_contrast_terms(
                        qa["text"], qb["text"])
                    with store.connect() as conn:
                        store.record_decision(
                            conn, pair["a"], pair["b"], "distinct", "reviewer",
                            pair["score"], "within-assessment repeat",
                            candidate_contrast_terms=(only_a, only_b),
                        )
                    st.success("Recorded as distinct.")
                    if only_a or only_b:
                        st.caption(
                            f"Captured as a candidate distinguishing pair: "
                            f"{', '.join(only_a[:5]) or '(none)'} vs "
                            f"{', '.join(only_b[:5]) or '(none)'}"
                        )
                d3.button("Skip", key=f"skip_{key}")

                st.divider()

# ---------------------------------------------------- search by meaning

elif page == "Search by meaning":
    st.title("Search by meaning")
    st.caption(
        "Finds questions by what they ask about, not by matching words."
    )

    examples = [
        "how do plants make their own food",
        "area of a rectangle",
        "when did Sri Lanka become independent",
    ]
    cols = st.columns(len(examples))
    for col, ex in zip(cols, examples):
        if col.button(ex, use_container_width=True):
            st.session_state["query"] = ex

    query = st.text_input(
        "Search", value=st.session_state.get("query", ""),
        placeholder="Type a question or a topic...",
    )

    if query:
        with st.spinner("Searching..."):
            embedder = get_embedder()
            qvec = embedder.encode(query, is_query=True)[0]
            hits = dedupe.search(qvec, rows, top_k=5)

        st.divider()
        for hit in hits:
            q = hit["question"]
            st.markdown(f"**{hit['score']:.0%} match** - {q['id']}")
            title_caption(q)
            st.write(q["text"])
            st.caption(
                f"Grade {q['grade']} - {q['subject']} - "
                f"{q['topic'] or 'untagged'}"
            )
            st.divider()


# --------------------------------------------------------------- paper intake

elif page == "Paper intake":
    st.title("Paper intake")
    st.caption(
        "Upload a question paper - PDF, Word, or an image of a handwritten "
        "one. Its questions are pulled out, you correct them, then the batch "
        "is checked for repeats against itself before any of it joins the "
        "bank. Extraction is local and rough, handwriting especially, so "
        "every question needs a look - nothing here is trusted until you "
        "have confirmed it."
    )

    up = st.file_uploader(
        "Question paper",
        type=["pdf", "docx", "doc", "png", "jpg", "jpeg", "tif", "tiff", "bmp"],
    )
    up_lang = st.radio(
        "Language of the paper",
        list(intake_extract.OCR_LANGS),
        format_func=lambda k: intake_extract.OCR_LANGS[k],
        horizontal=True,
        help="Used to read scanned pages and questions pasted in as "
             "pictures. Typed text is read directly whatever this says.",
    )
    style_keys = list(intake_extract.SPLIT_STYLES)
    up_style = st.radio(
        "Where does a new question start in this paper?",
        style_keys,
        format_func=lambda k: intake_extract.SPLIT_STYLES[k],
        horizontal=True,
        help="Most papers number their questions. Pick 'Question' for a "
             "worked-solutions or marking-scheme document, where the "
             "numbered lists are answer steps rather than questions.",
    )
    if up is not None and st.button("Extract questions"):
        suffix = Path(up.name).suffix
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(up.getbuffer())
            tmp_path = tmp.name
        try:
            with st.spinner("Reading the paper - scanned pages and pictures "
                            "are OCR'd, which can take a minute..."):
                # The real name, not the temp file's - the paper's grade,
                # subject and title fall back to it ("GIT_Gr12_FirstTT").
                result = intake_extract.extract(tmp_path, style=up_style,
                                                lang=up_lang,
                                                filename=up.name)
        finally:
            os.unlink(tmp_path)

        with store.connect() as conn:
            doc_id = store.create_intake_document(
                conn, up.name, file_type=result["file_type"],
                extractor=result["extractor"],
                paper_grade=result["paper"]["grade"],
                paper_subject=result["paper"]["subject"],
                paper_title=result["paper"]["title"],
                sections=result["sections"],
                raw_text=result["raw_text"], split_style=up_style,
            )
            store.add_extracted_questions(
                conn, doc_id, result["questions"],
                fallback_grade=result["paper"]["grade"],
                fallback_subject=result["paper"]["subject"],
            )
        parts = ", ".join(f"{s['label']} ({s['questions']})"
                          for s in result["sections"] if s["label"])
        st.success(
            f"Extracted {len(result['questions'])} question(s) from "
            f"{up.name} via {result['extractor']}"
            + (f" - {parts}" if parts else "")
            + ". Select it below to review."
        )
        st.rerun()

    with store.connect() as conn:
        docs = [dict(d) for d in store.intake_documents(conn)]

    if not docs:
        st.info("No papers uploaded yet.")
        st.stop()

    labels = {d["id"]: f"#{d['id']}  -  {d['filename']}  ({d['status']})"
              for d in docs}
    doc_id = st.selectbox(
        "Uploaded papers", [d["id"] for d in docs],
        format_func=lambda i: labels[i],
    )
    doc = next(d for d in docs if d["id"] == doc_id)
    st.caption(f"Extractor: {doc['extractor']}  -  uploaded {doc['uploaded_at']}")
    if intake_extract.was_incomplete(doc["extractor"] or ""):
        st.warning(
            "Part of this paper could not be read, so questions are "
            "probably missing below - see the extractor note above. Fix "
            "the cause and upload it again, or add the missing questions "
            "by hand."
        )

    with store.connect() as conn:
        drafts = [dict(q) for q in store.intake_questions(conn, doc_id)]

    # -- re-split, without needing the original file again --
    if doc.get("raw_text"):
        with st.expander("Split it differently, or see the raw text read"):
            st.caption(
                "The paper's text is kept, so it can be re-split without "
                "uploading it again. Re-splitting REPLACES the questions "
                "below, including any edits already made to them."
            )
            cur_style = doc.get("split_style") or \
                intake_extract.DEFAULT_SPLIT_STYLE
            keys = list(intake_extract.SPLIT_STYLES)
            re_style = st.radio(
                "Where does a new question start?", keys,
                index=keys.index(cur_style) if cur_style in keys else 0,
                format_func=lambda k: intake_extract.SPLIT_STYLES[k],
                key="resplit_style",
            )
            parsed = intake_extract.structure(doc["raw_text"], style=re_style)
            preview = parsed["questions"]
            parts = ", ".join(f"{s['label']}: {s['questions']}"
                              for s in parsed["sections"] if s["label"])
            st.write(
                f"That style finds **{len(preview)}** question(s)"
                + (f" ({parts})" if parts else "")
                + f". Currently stored: **{len(drafts)}**."
            )
            if st.button("Re-split this paper", type="primary"):
                meta = intake_extract.detect_paper_meta(
                    doc["raw_text"], filename=doc["filename"])
                # Papers read before grades and subjects came back in the
                # bank's own names hold "13" or "Combined Mathematics",
                # which the lists below cannot show - take the fresh
                # reading for those, keep a reviewer's own choice otherwise.
                grade = (doc["paper_grade"]
                         if intake_extract.is_grade_code(doc["paper_grade"])
                         else meta["grade"] or doc["paper_grade"])
                subject = (doc["paper_subject"]
                           if doc["paper_subject"] in intake_extract.KNOWN_SUBJECTS
                           else meta["subject"] or doc["paper_subject"])
                with store.connect() as conn:
                    store.replace_intake_questions(
                        conn, doc_id, preview,
                        fallback_grade=grade, fallback_subject=subject,
                    )
                    store.update_intake_document(
                        conn, doc_id, split_style=re_style, status="extracted",
                        sections=parsed["sections"], paper_grade=grade,
                        paper_subject=subject,
                        paper_title=doc.get("paper_title") or meta["title"],
                    )
                st.rerun()

            st.text_area(
                "Raw text the extractor read", value=doc["raw_text"],
                height=220, disabled=True, key=f"rawtext_{doc_id}",
            )
    else:
        st.info(
            "This paper was uploaded before the raw text was kept, so it "
            "cannot be re-split here - upload it again to pick up the "
            "current extractor."
        )

    # -- paper-level labels: one set for the whole paper. Grade, subject
    # and title are the same for every question on a paper; only the
    # marks differ, and those are set per question below. --
    sections = store.intake_sections(doc)
    title_bits = [doc.get("paper_subject"),
                  intake_extract.grade_label(doc.get("paper_grade")),
                  doc.get("paper_title")]
    st.header(" - ".join(b for b in title_bits if b) or doc["filename"])

    st.subheader("Paper details")
    # The bank's own grades and subjects, plus whatever this paper holds -
    # a value missing from its list would otherwise show as blank.
    grade_opts = sorted({r["grade"] for r in rows if r["grade"]}
                        | ({doc["paper_grade"]} if doc["paper_grade"] else set()))
    subject_opts = sorted({r["subject"] for r in rows if r["subject"]}
                          | ({doc["paper_subject"]} if doc["paper_subject"]
                             else set()))

    pc1, pc2, pc3, pc4 = st.columns([2, 2, 3, 1])
    pg = pc1.selectbox(
        "Grade", [""] + grade_opts,
        index=([""] + grade_opts).index(doc["paper_grade"] or ""),
        format_func=lambda g: intake_extract.grade_label(g) or "-",
    )
    ps = pc2.selectbox(
        "Subject", [""] + subject_opts,
        index=([""] + subject_opts).index(doc["paper_subject"] or ""),
        format_func=lambda s: s or "-",
    )
    ptitle = pc3.text_input("Title", value=doc.get("paper_title") or "")
    pc4.markdown("<div style='height:1.7rem'></div>", unsafe_allow_html=True)
    if pc4.button("Apply to all", use_container_width=True):
        with store.connect() as conn:
            store.update_intake_document(
                conn, doc_id, paper_grade=pg or None, paper_subject=ps or None,
                paper_title=ptitle.strip() or None,
            )
            for d in drafts:
                store.update_intake_question(
                    conn, d["id"], grade=pg or None, subject=ps or None,
                )
        st.rerun()

    st.divider()
    st.subheader(f"Questions ({len(drafts)})")

    low = [d for d in drafts if d["extraction_confidence"] == "low"]
    if low:
        st.warning(
            f"{len(low)} of {len(drafts)} came out low-confidence - almost no "
            "text, or no marks found. They are marked below."
        )
    if len(drafts) == 1 and drafts[0]["question_number"] is None:
        st.error(
            "This paper did not split into separate questions - it came back "
            "as one block. Correct the text below and use 'Add a question' to "
            "enter each one, or re-upload a clearer copy."
        )

    st.caption(
        "Grade and subject come from Paper details above. Set the marks per "
        "question, fix the text, and tick Reviewed once a question is correct."
    )

    # Parts in paper order, questions in order within each part - so a
    # question added by hand to Part I shows under Part I, not at the end.
    part_order = {s["label"]: n for n, s in enumerate(sections)}
    drafts.sort(key=lambda d: (part_order.get(d.get("section"), len(part_order)),
                               d["seq"]))
    by_label = {s["label"]: s for s in sections}
    shown = None
    for i, d in enumerate(drafts):
        if d.get("section") and d["section"] != shown:
            shown = d["section"]
            part = by_label.get(shown, {})
            st.header(shown)
            about = [
                part.get("heading") if part.get("heading") != shown else None,
                intake_extract.KIND_LABELS.get(part.get("kind")),
                f"{sum(1 for x in drafts if x.get('section') == shown)} "
                "question(s)",
                f"{part['marks_each']} marks each"
                if part.get("marks_each") else None,
            ]
            st.caption("  |  ".join(a for a in about if a))
            if part.get("instructions"):
                with st.expander("Instructions for this part"):
                    st.text(part["instructions"])

        with st.container(border=True):
            head, rm = st.columns([5, 1])
            label = (f"Question {d['question_number']}" if d["question_number"]
                     else f"Question {i + 1} (no number found)")
            if d["extraction_confidence"] == "low":
                label += "  -  low confidence"
            head.markdown(f"**{label}**")
            if rm.button("Remove", key=f"iq_{d['id']}_rm",
                         use_container_width=True):
                with store.connect() as conn:
                    store.delete_intake_question(conn, d["id"])
                st.rerun()

            st.text_area(
                "Question text", value=d["text"] or "",
                key=f"iq_{d['id']}_text", height=140,
                label_visibility="collapsed",
            )
            m1, m2, _ = st.columns([1, 1, 3])
            m1.number_input(
                "Marks", min_value=0, max_value=100, step=1,
                value=int(d["marks"]) if d["marks"] else 0,
                key=f"iq_{d['id']}_marks",
            )
            m2.checkbox(
                "Reviewed", value=bool(d["reviewed"]),
                key=f"iq_{d['id']}_reviewed",
            )
            with st.expander("Answer" if d.get("answer")
                             else "Answer (none found in the paper)"):
                st.text_area(
                    "Answer", value=d.get("answer") or "",
                    key=f"iq_{d['id']}_answer", height=120,
                    label_visibility="collapsed",
                )

    if st.button("Save all questions", type="primary"):
        with store.connect() as conn:
            for d in drafts:
                m = st.session_state[f"iq_{d['id']}_marks"]
                store.update_intake_question(
                    conn, d["id"],
                    text=st.session_state[f"iq_{d['id']}_text"],
                    marks=int(m) if m else None,
                    answer=st.session_state[f"iq_{d['id']}_answer"].strip()
                    or None,
                    reviewed=1 if st.session_state[f"iq_{d['id']}_reviewed"]
                    else 0,
                )
        st.success("Saved.")
        st.rerun()

    with st.expander("Add a question the extractor missed"):
        part_labels = [s["label"] for s in sections if s["label"]]
        if part_labels:
            st.selectbox("Part", part_labels, key="iq_new_section")
        st.text_input("Number (optional)", key="iq_new_number")
        st.text_area("Question text", key="iq_new_text", height=120)
        st.number_input("Marks", min_value=0, max_value=100, step=1, value=0,
                        key="iq_new_marks")
        if st.button("Add question"):
            new_text = st.session_state["iq_new_text"].strip()
            if not new_text:
                st.warning("Enter the question text first.")
            else:
                with store.connect() as conn:
                    seq = max((d["seq"] for d in drafts), default=0) + 1
                    m = st.session_state["iq_new_marks"]
                    store.add_intake_question(
                        conn, doc_id, seq, new_text,
                        question_number=st.session_state["iq_new_number"] or None,
                        grade=doc["paper_grade"], subject=doc["paper_subject"],
                        marks=int(m) if m else None,
                        section=(st.session_state["iq_new_section"]
                                 if part_labels else None),
                    )
                st.rerun()

    st.divider()
    st.subheader("Duplicate check within this paper")
    st.caption(
        "Same rule as 'Duplicates within a paper' for the existing bank - the "
        "same question must not appear twice on one paper. Runs only once "
        "every question is marked reviewed, because it is meaningless on text "
        "that has not been corrected yet."
    )

    all_reviewed = bool(drafts) and all(d["reviewed"] for d in drafts)
    if not all_reviewed:
        pending = sum(1 for d in drafts if not d["reviewed"])
        st.info(f"{pending} question(s) still not marked reviewed.")

    if st.button("Check for duplicates", disabled=not all_reviewed):
        with store.connect() as conn:
            fresh = [dict(q) for q in store.intake_questions(conn, doc_id)]
        with st.spinner("Embedding and comparing..."):
            out = intake_review.check_batch(fresh, get_embedder())
        with store.connect() as conn:
            store.update_intake_document(conn, doc_id, status="checked")

        by_id = {d["id"]: d for d in fresh}
        if not out["exact_groups"] and not out["similar_pairs"]:
            st.success("No repeats found in this paper.")

        def qname(q):
            # Part I and Part II both have a question 1.
            num = f"Q{q['question_number'] or q['id']}"
            return f"{q['section']} {num}" if q["section"] else num

        for grp in out["exact_groups"]:
            nums = ", ".join(qname(by_id[i]) for i in grp)
            st.error(f"Exact duplicate - questions {nums}")
            for i in grp:
                st.write(f"- {by_id[i]['text']}")
            st.divider()

        for pair in out["similar_pairs"]:
            qa, qb = by_id[pair["a"]], by_id[pair["b"]]
            colour = {
                "LIKELY DUPLICATE": "red",
                "NEEDS REVIEW": "orange",
                "DISTINCT": "green",
            }[pair["verdict"]]
            st.markdown(
                f"### :{colour}[{pair['verdict']}] &nbsp; "
                f"{pair['score']:.0%} similar"
            )
            left, right = st.columns(2)
            left.markdown(f"**{qname(qa)}**\n\n{qa['text']}")
            right.markdown(f"**{qname(qb)}**\n\n{qb['text']}")
            if pair["reasons"]:
                st.warning(
                    "**Safety checks fired** (score lowered from "
                    f"{pair['raw_score']:.0%} to {pair['score']:.0%}):\n\n"
                    + "\n".join(f"- {r}" for r in pair["reasons"])
                )
            st.divider()

    st.caption(
        "Promoting the reviewed questions into the bank, and back to "
        "EdSider, is not wired yet - this page stops at the check."
    )


# --------------------------------------------------------- question bank

else:
    st.title("Question bank")

    df = pd.DataFrame([
        {
            "ID": r["id"],
            "Title": r["title"],
            "Question": r["text"],
            "Grade": r["grade"],
            "Subject": r["subject"],
            "Topic": r["topic"],
            "Marks": r["marks"],
            "Contributor": r["contributor"],
            "Status": r["status"],
        }
        for r in rows
    ])

    c1, c2 = st.columns(2)
    subjects = c1.multiselect(
        "Subject", sorted(df["Subject"].dropna().unique())
    )
    grades = c2.multiselect(
        "Grade", sorted(df["Grade"].dropna().unique())
    )

    view = df
    if subjects:
        view = view[view["Subject"].isin(subjects)]
    if grades:
        view = view[view["Grade"].isin(grades)]

    st.caption(f"Showing {len(view)} of {len(df)} questions")
    st.dataframe(view, use_container_width=True, hide_index=True)
