"""Paper assignment - a separate, small app for the assigning administrator.

Run with:   streamlit run masd_app.py

Once data-entry's own duplicate check is done (a paper's status is
'checked'), someone has to decide which teacher reviews it. That person
is not data-entry and not a teacher - a third, separate role - so this
is a third, separate app: a list of checked papers, a list of teachers
who have actually opened their own review page at least once (see
teacher_app.py's upsert_teacher() call), and a button to hand one to
the other. Nothing here edits a question - that is what the other two
apps are for.
"""

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src import store

st.set_page_config(page_title="Assign papers", layout="wide")


@st.cache_resource(ttl=60, show_spinner="Connecting to the database...")
def database_problem(db_name):
    """None when the database answers, else why not. A bare
    store.connect() with no `with` would leak the connection it checks
    out - see app.py's own copy of this same check."""
    try:
        with store.connect():
            return None
    except Exception as unreachable:  # noqa: BLE001 - shown to the reviewer
        return str(unreachable)


problem = database_problem(store.display_name())
if problem:
    database_problem.clear()
    st.title("Assign papers to teachers")
    st.error(f"Cannot reach the database named by `EDSIDER_DB`:\n\n```\n{problem}\n```")
    st.stop()

st.title("Assign papers to teachers")
st.caption(
    "Papers data-entry has already checked for internal repeats, waiting "
    "to be handed to a teacher for subject review."
)

with store.connect() as conn:
    docs = [dict(d) for d in store.intake_documents(conn)]
    teachers = [dict(t) for t in store.assignable_teachers(conn)]

teacher_options = {t["identity"]: (t["display_name"] or t["identity"])
                   for t in teachers}

to_assign = [d for d in docs if d["status"] == "checked"]
already_assigned = [d for d in docs
                    if d["status"] in ("teacher_review", "refer_back",
                                       "review_done", "published")]

st.subheader(f"Ready to assign ({len(to_assign)})")
if not to_assign:
    st.info("No checked papers are waiting for assignment right now.")
elif not teacher_options:
    st.warning(
        "No teachers yet, so there is nothing to assign to. Run "
        "`python scripts/sync.py` to copy EdSider's teachers here, or ask "
        "a teacher to open the Teacher review app once."
    )
else:
    for d in to_assign:
        with st.container(border=True):
            st.markdown(f"**#{d['id']} - {d['filename']}**")
            st.caption(
                f"{d['paper_grade'] or 'grade not set'}  -  "
                f"{d['paper_subject'] or 'subject not set'}  -  "
                f"uploaded {d['uploaded_at']}"
            )
            teacher = st.selectbox(
                "Assign to", list(teacher_options),
                format_func=lambda i: teacher_options[i],
                key=f"assign_{d['id']}",
            )
            if st.button("Assign", key=f"assign_btn_{d['id']}", type="primary"):
                with store.connect() as conn:
                    store.assign_document(conn, d["id"], teacher)
                st.success(f"Assigned to {teacher_options[teacher]}.")
                st.rerun()

st.divider()
st.subheader(f"Already assigned ({len(already_assigned)})")
if not already_assigned:
    st.caption("Nothing assigned yet.")
else:
    status_labels = {
        "teacher_review": "Awaiting the teacher's review",
        "refer_back": "Sent back for changes",
        "review_done": "Review done",
        "published": "Published",
    }
    for d in already_assigned:
        who = teacher_options.get(d["assigned_to"], d["assigned_to"])
        st.write(
            f"#{d['id']} - {d['filename']}  -  {who}  -  "
            f"**{status_labels.get(d['status'], d['status'])}**"
        )
