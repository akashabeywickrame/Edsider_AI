"""Pasting a paper's text instead of uploading a file - the path for
papers read by the local OCR tool on a data-entry computer (see
extract.extract_text and service.create_document_from_text).

The sample is Surya's vision-OCR output for page 1 of the real Grade 12
Sinhala Business Statistics paper, verbatim after HTML cleaning: a
"combination" question's lettered statements then its five choices
printed together on ONE line, and plain numbered choices one per line.
It stops after question 4 - question 5 runs on to page 2, which is not
in the sample. A cut-down two-question version is NOT a fair test: the
paper's own "1)" numbering only beats the choices' "1. 2. 3. 4. 5." when
there are enough questions for it to count up further (see structure()).
"""

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.intake import extract
import service
from service import PastedPaperIn, create_document_from_text

SINHALA_PAGE = """12 ශ්‍රේණිය පළමු වාර පරීක්ෂණය ව්‍යාපාර සංඛ්‍යානය - 1 පත්‍රය කාලය - පැය දෙකයි
උපදෙස්
සියලුම ප්‍රශ්න වලට පිළිතුරු සපයන්න.
ගණක යන්ත්‍ර භාවිතයට ඉඩ නොලැබේ.
1 සිට 50 තෙක් එක් එක් ප්‍රශ්නයට (1), (2), (3), (4), (5) යන පිළිතුරු වලින් නිවැරදි හෝ ඉතාමත් ගැළපෙණ පිළිතුර තෝරන්න.
1) පහත ප්‍රකාශ අතුරින් සත්‍ය ප්‍රකාශය හෝ ප්‍රකාශ ඇතුළත් පිළිතුර තෝරන්න.
A. දත්ත රැස් කිරීම, සංවිධානය කිරීම හා විශ්ලේෂණය කිරීමේ ශිල්පීය ක්‍රම ඇතුළත් විෂය ක්ෂේත්‍රය අනුමිතික සංඛ්‍යානය වේ.
B. සංඛ්‍යානය, ව්‍යාපාර අරමුණු හා කාර්යයන් සමඟ සම්බන්ධ කළ විට එය ව්‍යාපාර සංඛ්‍යානය වේ.
C. අනුමිතික සංඛ්‍යානයේ දී නියැඳි සමීක්ෂණ ප්‍රතිඵල භාවිතයෙන් තීරණ ගැනීම සිදු වේ.
1. A පමණි      2. A හා B පමණි      3. B හා C පමණි      4. A හා C පමණි      5. A, B හා C
2) පහත පිළිතුරු අතරින් සංඛ්‍යානයේ මූලික කාර්යභාරය ලෙස සැලකෙන්නේ කුමක්ද?
1. අවිනිශ්චිත තත්වයන් යටතේ විද්‍යාත්මක නිගමන වලට එළඹීම
2. දත්ත රැස් කිරීම
3. ව්‍යාපාර පිළිබඳ පුරෝකථනය කිරීම
4. ව්‍යාපාර ක්‍රියාකාරීත්වයට අදාළ තීරණ ගැනීම
5. දත්ත සැසඳීමේ ශිල්පීය ක්‍රම භාවිතය
3) පහත පිළිතුරු අතරින් සංඛ්‍යානයේ සීමාවක් නොවන්නේ මින් කුමක්ද?
1. සංකීර්ණ දත්ත තේරුම් ගත හැකි වන පරිදි සරල ක්‍රම අනුගමනය කිරීම
2. නිගමන අවිනිශ්චිත තාවයකින් යුක්ත වීම
3. ගුණාත්මක දත්ත විශ්ලේෂණයේ දී සීමා ඇති වීම
4. සංඛ්‍යානය අවභාවිතා වීමට ඉඩකඩ ඇති වීම
5. තනි සංඛ්‍යාවක් විශ්ලේෂණය කළ නොහැකි වීම
4) සංඛ්‍යානය යොදා ගැනෙන විෂය ක්ෂේත්‍රයක් නොවන්නේ මින් කුමක්ද?
1. සමාජ විද්‍යාව
2. භූගෝල විද්‍යාව
3. මානව සම්පත් කළමණාකරනය
4. සිංහල භාෂාව හා සාහිත්‍යය
5. මූල්‍ය කළමණාකරනය"""


def _as_surya_html(text):
    """The same text the way Surya returns it before cleaning."""
    out = []
    for line in text.split("\n"):
        out.append(f"<li>• {line}</li>" if line[:2] in ("A.", "B.", "C.") else f"<p><b>{line}</b></p>")
    return "\n".join(out)


def _mcq_shapes(result):
    return [(q["question_number"], len(extract.split_mcq_options(q["text"])[1]))
            for q in result["questions"]]


def test_surya_layout_splits_into_mcqs_with_their_options():
    result = extract.extract_text(SINHALA_PAGE, filename="Sinhala paper")
    assert _mcq_shapes(result) == [("1", 5), ("2", 5), ("3", 5), ("4", 5)]
    q1 = result["questions"][0]
    stem, options = extract.split_mcq_options(q1["text"])
    # The lettered statements stay in the question; the one-line answer
    # key is the options.
    assert "A." in stem and "C." in stem
    assert [o["text"] for o in options][0] == "A පමණි"


def test_surya_html_is_cleaned_to_the_same_result():
    plain = extract.extract_text(SINHALA_PAGE, filename="x")
    html = extract.extract_text(_as_surya_html(SINHALA_PAGE), filename="x")
    assert _mcq_shapes(html) == _mcq_shapes(plain)
    assert "<" not in html["raw_text"] and "•" not in html["raw_text"]


def test_plain_text_is_never_altered_by_the_html_cleaner():
    assert extract.html_to_lines("1. a < b\n2. c > d") == "1. a < b\n2. c > d"


def test_pasted_paper_is_stored_like_an_upload(local_db):
    doc = create_document_from_text(
        PastedPaperIn(text=SINHALA_PAGE, title="Gr12 Business Stats P1"),
        identity="data-entry-1")
    assert doc.filename == "Gr12 Business Stats P1"
    assert doc.extractor == "pasted text"
    qs = service.list_questions(doc.id)
    assert [(q.question_number, q.question_type, len(q.mcq.options)) for q in qs] == [
        ("1", "mcq", 5), ("2", "mcq", 5), ("3", "mcq", 5), ("4", "mcq", 5)]
    # Re-splitting works the same as for an uploaded file - the text is kept.
    preview = service.preview_resplit(doc.id, service.ResplitRequest(style="number"))
    assert preview.question_count == 4


def test_blank_pasted_text_is_refused(local_db):
    with pytest.raises(HTTPException) as exc:
        create_document_from_text(PastedPaperIn(text="   \n  "), identity="x")
    assert exc.value.status_code == 400


# Part II, question 5 of the scanned 2025 A/L Chemistry paper (Sinhala
# medium), as the local reader writes it once formulas and equations are
# text. Marks are written in Sinhala - "(ලකුණු 60 යි)", "60 marks" - once
# per part, on a line of their own after the part's last sub-part, and
# "ලකුණු 150 බැගින්" ("150 marks each") for every question.
CHEMISTRY_Q5 = """B කොටස — රචනා
ප්‍රශ්න දෙකකට පමණක් පිළිතුරු සපයන්න. (එක් එක් ප්‍රශ්නයට ලකුණු 150 බැගින් ලැබේ.)
5. (a) N₂(g) හි 1.0 mol ක් සහ H₂(g) හි 2.0 mol ක් කලින් රේචනය කරන ලද පරිමාව 1.0 dm³ වන දෘඪ-සංවෘත භාජනයක් තුළ 450 °C උෂ්ණත්වයේදී මිශ්‍ර කර පහත සමතුලිතතාවයට එළඹීමට ඉඩ හරින ලදී.
N₂(g) + 3H₂(g) ⇌ 2NH₃(g)
(i) 450 °C දී සමතුලිතතා පද්ධතියෙහි මුළු පීඩනය ගණනය කරන්න.
(ii) 450 °C දී සමතුලිතතා පද්ධතියේ N₂(g), H₂(g) සහ NH₃(g) හි ආංශික පීඩන ගණනය කරන්න.
(iii) 450 °C දී පද්ධතියෙහි සමතුලිතතා නියතය Kₚ ගණනය කරන්න.
(ලකුණු 60 යි)
(b) N₂(g) + 3H₂(g) ⇌ 2NH₃(g) ප්‍රතික්‍රියාවෙහි ΔH° සහ ΔS° උෂ්ණත්වය සමග වෙනස් නොවන බව උපකල්පනය කරන්න.
(i) පද්ධතියේ උෂ්ණත්වය වැඩි කළ විට NH₃(g) හි සමතුලිත සාන්ද්‍රණය මත ඇතිවන බලපෑම පුරෝකථනය කරන්න.
(ii) ඉහත ප්‍රතික්‍රියාව සඳහා ΔH° = -90 kJ mol⁻¹ සහ ΔS° = -200 J K⁻¹ mol⁻¹ වේ. ΔG° අගයන් ගණනය කරන්න.
(ලකුණ 60 යි)
(c) (i) සංශුද්ධ CCl₄(l) හි තාපාංකයේදී පවතින සමතුලිතතාවය ලියන්න.
(ii) CCl₄(l) හි සාමාන්‍ය තාපාංකය ගණනය කරන්න.
(ලකුණු 30 යි)
"""


def test_sinhala_marks_and_part_totals():
    drafts = extract.extract_text(CHEMISTRY_Q5)["questions"]
    by_number = {q["question_number"]: q for q in drafts}
    # "150 marks each" is the question's mark; each "(ලකුණු N යි)" line -
    # including the OCR's "(ලකුණ 60 යි)" with its last vowel sign lost - is
    # the total of the part it closes, not of that part's last sub-part.
    assert by_number["5"]["marks"] == 150
    assert by_number["5(a)"]["marks"] == 60
    assert by_number["5(b)"]["marks"] == 60
    assert by_number["5(c)"]["marks"] == 30
    assert by_number["5(a)(iii)"]["marks"] is None
    assert [n for n in by_number if n.startswith("5(a)(")] == ["5(a)(i)", "5(a)(ii)", "5(a)(iii)"]
