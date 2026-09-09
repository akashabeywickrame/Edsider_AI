"""EdSider AI - duplicate detection and semantic search.

Run with:   streamlit run app.py
"""

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src import store, dedupe, guards
from src.embeddings import DEFAULT_MODEL
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
    from src.embeddings import Embedder
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
banks = sorted(p.name for p in (ROOT / "data").glob("*.db"))
if banks and store.DB_PATH is None:
    store.use_database(ROOT / "data" / banks[0])

if not banks:
    st.title("EdSider AI")
    st.error(
        "No database found under `data/*.db`. Build one first:\n\n"
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
    store.use_database(ROOT / "data" / chosen)
    st.rerun()

page = st.sidebar.radio(
    "View",
    ["Check a new question", "Find duplicates", "Duplicates within a paper",
     "Search by meaning", "Question bank"],
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
