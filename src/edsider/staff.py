"""Who someone is in EdSider, read live from EdSider's own database - for a
sign-in that came through EdSider (its B2C ID token, see id_tokens.py).

EdSider decides a staff member's job from registration.teacher.role_code -
what its web app keeps as `employeeType` and shows its menus by - not from
staff_user_role, which scripts/sync.py reads: in the dev copy the two
disagreed for about twenty Data Entry Operators (DEOP in role_code, only
ASSR in staff_user_role). Reading the same column EdSider's pages do means
whoever EdSider shows the data-entry pages to is who this API lets in.

Same database papers are sent to (EDSIDER_SEND_DB, send.connect()), so the
ids recorded here are the ones a sent paper names.
"""

import time

from . import send

# Every request resolves its caller, and a round trip to the Azure server
# costs ~0.35 s - so an answer is kept briefly. A minute is also how long a
# role EdSider takes away can still be used here.
CACHE_SECONDS = 60

_cache = {}

QUERY = """
    SELECT su.user_id, su.staff_id, t.role_code,
           NULLIF(trim(concat_ws(' ', trim(t.first_name), trim(t.last_name))), '') AS name
    FROM user_management.staff_user su
    JOIN table_log tl ON tl.table_id = su.table_log_id
         AND tl.table_name = 'registration.teacher'
    JOIN registration.teacher t ON t.user_id = su.staff_id
    WHERE lower(trim(su.user_email)) = %(email)s
      AND t.user_status_id = 1
    ORDER BY su.user_id
    LIMIT 1
"""


def edsider_person(email):
    """{user_id, staff_id, role_code, name} for an active EdSider staff
    member, or None - someone EdSider does not list (or lists as an admin
    or student, who have no role_code here). `email` lower-cased."""
    now = time.monotonic()
    hit = _cache.get(email)
    if hit and hit[0] > now:
        return hit[1]
    conn = send.connect()
    try:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute(QUERY, {"email": email})
            row = cur.fetchone()
    finally:
        conn.close()
    person = None
    if row:
        person = {"user_id": row[0], "staff_id": row[1],
                  "role_code": (row[2] or "").strip() or None, "name": row[3]}
    _cache[email] = (now + CACHE_SECONDS, person)
    return person
