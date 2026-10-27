"""Write a converted paper into EdSider's own database - step 6 of
"Sending Papers to EdSider". `export.build_package()` decides every row;
this only writes them, in EdSider's order, turning the package's local keys
("part1", "sec1_2", "q512") into the ids EdSider's sequences hand back.

One transaction for the whole paper: a failure leaves nothing behind in
EdSider. The caller commits.

Where it writes is a setting, not a default: EDSIDER_SEND_DB names the
database (on the server PG_HOST names, as PG_USER). Unset, sending is off -
so a deployed app never writes to EdSider until someone decides it should,
and pointing it at EdSider's live database is a deliberate change of that
one setting (it is the pre-prod copy, examonliine_db_prod_copy, for now).
"""

import os

# What the package's rows carry for us, not for EdSider's columns.
_OURS = {"key", "source_question_id", "sub_question_keys", "passage_key"}


class SendNotSetUp(RuntimeError):
    """EDSIDER_SEND_DB is not set - sending is off on this server."""


class TitleTaken(ValueError):
    """EdSider already has a paper with this title."""


def target():
    """The database papers are sent to, or None when sending is off."""
    return (os.environ.get("EDSIDER_SEND_DB") or "").strip() or None


def connect():
    """A writable connection to EdSider's database - see the module note."""
    import psycopg2
    name = target()
    if not name:
        raise SendNotSetUp("Sending to EdSider is not set up on this server (EDSIDER_SEND_DB).")
    return psycopg2.connect(
        host=os.environ["PG_HOST"], port=os.environ.get("PG_PORT", "5432"), dbname=name,
        user=os.environ["PG_USER"], password=os.environ["PG_PASSWORD"],
        sslmode=os.environ.get("PG_SSLMODE", "require"), connect_timeout=15)


def _insert(cur, table, row, returning=None):
    columns = [c for c in row if c not in _OURS]
    sql = (f"INSERT INTO {table} ({', '.join(columns)}) "
           f"VALUES ({', '.join(['%s'] * len(columns))})")
    if returning:
        sql += f" RETURNING {returning}"
    cur.execute(sql, [row[c] for c in columns])
    return cur.fetchone()[0] if returning else None


def write_package(pg, package):
    """Every row of `package` into EdSider, through connection `pg` (not
    committed). Returns {"assessment_id", "questions": {our question id:
    EdSider's}, "links", "passages"}. Refuses a package with errors."""
    errors = [p["message"] for p in package["problems"] if p["level"] == "error"]
    if errors:
        raise ValueError("The paper cannot be sent yet: " + " ".join(errors))
    cur = pg.cursor()

    paper = dict(package["assessment"])
    # EdSider's screens keep paper titles unique; its database does not, so
    # a second paper under a taken title would get in and trip them up.
    cur.execute("SELECT assessment_id FROM assessment.assessment WHERE lower(title) = lower(%s) "
                "LIMIT 1", (paper.get("title"),))
    taken = cur.fetchone()
    if taken:
        raise TitleTaken(f"EdSider already has a paper titled \"{paper.get('title')}\" "
                         f"(#{taken[0]}). Change this paper's title and send it again.")
    if paper.get("terms_content") is None and paper.get("language_id") is not None:
        # EdSider's default terms for the paper's medium, as its own form adds.
        cur.execute("SELECT content FROM assessment.assessment_term_condition "
                    "WHERE language_id = %s ORDER BY term_condition_id LIMIT 1",
                    (paper["language_id"],))
        found = cur.fetchone()
        paper["terms_content"] = found[0] if found else None
    cur.execute("SELECT now()")
    now = cur.fetchone()[0]
    aid = _insert(cur, "assessment.assessment", dict(paper, created_at=now), "assessment_id")

    _insert(cur, "assessment.assessment_schedule",
            dict(package["schedule"], assessment_id=aid, scheduled_at=now))
    _insert(cur, "assessment.assessment_assessor",
            dict(package["assessor"], assessment_id=aid, from_date=now))

    sections = {}
    for part in package["parts"]:
        part_id = _insert(cur, "assessment.assessment_part",
                          dict(part["row"], assessment_id=aid), "assessment_part_id")
        for sec in part["sections"]:
            row = {k: v for k, v in sec["row"].items() if k != "marks"}
            sections[sec["key"]] = _insert(
                cur, "assessment.assessment_part_section",
                dict(row, assessment_part_id=part_id), "part_section_id")

    passages = {}
    for psg in package["passages"]:
        row = dict(psg["row"], created_at=now)
        # Named as EdSider names its own: "ASMNT2141 Q28 - Q31".
        row["title"] = f"ASMNT{aid} {row['title']}"
        row["description"] = f"ASMNT{aid} {row['description']}"
        passages[psg["key"]] = _insert(cur, "question.passage", row, "passage_id")

    # Parts come before their question in the package, so a parent can
    # name its parts' new ids.
    ids, ours = {}, {}
    for q in package["questions"]:
        row = dict(q, created_at=now)
        if q.get("sub_question_keys"):
            row["sub_questions"] = ",".join(str(ids[k]) for k in q["sub_question_keys"])
        if q.get("passage_key"):
            row["passage_id"] = passages[q["passage_key"]]
        new_id = _insert(cur, "question.question", row, "question_id")
        ids[q["key"]] = new_id
        if q.get("source_question_id") is not None:
            ours[q["source_question_id"]] = new_id

    for link in package["links"]:
        _insert(cur, "assessment.assessment_question",
                dict(link["row"], assessment_id=aid, question_id=ids[link["question_key"]],
                     section_id=sections[link["section_key"]]))

    if package.get("operation"):
        _insert(cur, "assessment.assessment_operation",
                dict(package["operation"], asmnt_id=aid, created_at=now, updated_at=now))
    for review in package.get("reviews") or []:
        _insert(cur, "assessment.assessment_review_new",
                dict(review, asmnt_id=aid, created_at=now, updated_at=now))
    cur.close()
    return {"assessment_id": aid, "questions": ours, "links": len(package["links"]),
            "passages": len(passages)}
