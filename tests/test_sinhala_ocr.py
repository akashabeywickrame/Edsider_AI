"""Reading a scanned Sinhala paper. Every line below is copied from how
Tesseract read the real Grade 12 Business Statistics paper (intake #94) -
read as Sinhala alone it found 17 of its 50 MCQs and no Paper 2; read as
Sinhala+English and repaired, all 50 with their options, and Paper 2's two
parts with every lettered and roman sub-part."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.intake import extract

repair = extract.repair_sinhala_ocr


def test_statement_c_and_option_numbers_are_put_back():
    text = "\n".join([
        "1) පහන ප්‍රකාශ අතුරින්‌ සත්‍ය ප්‍රකාශය හෝ ප්‍රකාශ ඇතුළත්‌ පිළිතුර තෝරන්න.",
        "A, දන්ත රැස්‌ කිරීම, සංවිධානය කිරීම",
        "B. සංඛ්‍යානය, ව්‍යාපාර අරමුණු හා කාර්යයන්‌",
        "ට, අනුමිනික සංඛ්‍යානයේ දී නියැඳි සමීක්ෂණ",
        "3) පහන පිළිනුරු අතරින්‌ සංඛ්‍යානයේ සීමාවක්‌ නොවන්නේ මින්‌ කුමක්ද?",
        "1, සංකීර්ණ දත්ත",
        "2.නිගමන අවිනිශ්චිත",
        "රි.ගුණානත්මක දන්න විශ්ලේෂණයේ",
        "4.සංඛ්‍යානය අවභාචිතා",
    ])
    lines = repair(text).split("\n")
    assert lines[1].startswith("A. ") and lines[3].startswith("C. ")
    assert lines[5].startswith("1. ") and lines[7].startswith("3.ගුණා")


def test_c_is_only_c_after_b():
    # "ට," opening a line with no "B." before it is left as read.
    assert repair("ට, අනුමිනික සංඛ්‍යානයේ") == "ට, අනුමිනික සංඛ්‍යානයේ"


def test_combination_answer_lines():
    read = {
        "1. & පමණි 2, &ැ හා පි පමණි 3. පිහා ටිපමණි 4, & හා ට පමණි 5. A,B aC":
            "1. A පමණි 2. A හා B පමණි 3. B හා C පමණි 4. A හා C පමණි 5. A,B හා C",
        "1. &්‌ පමණි 2. ට පමණි 3. /& හා පි පමණි 4. පිහා ට පමණි 5. & හා ට පමණි":
            "1. A පමණි 2. C පමණි 3. A හා B පමණි 4. B හා C පමණි 5. A හා C පමණි",
        "1. & පමණි 2. AmB පමණි ර. ඨැ හා ට පමණි 4.B හා ටපමණි ට්‌. A, B aC":
            "1. A පමණි 2. A හා B පමණි 3. A හා C පමණි 4.B හා C පමණි 5. A, B හා C",
        "1. ගු පමණි 2. පිපමණි 3. හා පි පමණි 4. පිහා ට පමණි 5.A හා ට පමණි":
            "1. A පමණි 2. B පමණි 3. A හා B පමණි 4. B හා C පමණි 5.A හා C පමණි",
    }
    for ocr, printed in read.items():
        assert repair(ocr) == printed, ocr


def test_options_numbered_on_one_line():
    assert repair("1. 40.67 2. 42.36 3. 39 4, 43.5 5. 41.25") == \
        "1. 40.67 2. 42.36 3. 39 4. 43.5 5. 41.25"
    # Only the number expected next: "7, 21" is an option, and keeps its comma.
    assert repair("1. 13,15 2. 8, 20 3. 6, 22 4, 7, 21 5. 9,19") == \
        "1. 13,15 2. 8, 20 3. 6, 22 4. 7, 21 5. 9,19"
    _stem, options = extract.split_mcq_options(
        "මධ්‍යයනාය වන්නේ,\n" + repair("1. 40.67 2. 42.36 3. 39 4, 43.5 5. 41.25"))
    assert [o["text"] for o in options] == ["40.67", "42.36", "39", "43.5", "41.25"]


def test_roman_sub_parts_and_a_misread_part_letter():
    text = "\n".join([
        "01)",
        "අ)",
        "(i) සංඛ්‍යානය ප්‍රධාන වශයෙන්‌ කොටස්‌ දෙකකට වර්ගීකරණය කරයි. (4)",
        "(1) සංඛ්‍යානයේ කාර්යභාරයන්‌ 4ක්‌ නම්‌ කරන්න. (4)",
        "(iii) සංඛ්‍යානයේ සීමා 4ක්‌ දක්වන්න. (4)",
        "02)",
        "අ) ඕනෑම අධ්‍යනයක පළමු පියවර දත්ත රැස්‌ කිරීමයි.",
        "ආ) පහත එක්‌ එක්‌ අවස්ථා සඳහා",
        "(1) 1990 - 2024 චර්ෂය දක්වා",
        "(1) නව විෂය නිර්දේශයක්‌",
        "(ili) නිෂ්පාදනයක්‌ වෙළඳපොළට",
        "(iv) ශ්‍රී ලංකාවේ දළ දේශීය නිෂ්පාදිතය",
        "2)",
        "(i) අභ්‍යන්තර හා බාහිර දත්ත",
        "08) ආයතනයක සේවකයින්‌ 50 දෙනෙකුගේ වැටුප්‌",
        "(1) ඉහන දත්ත සඳහා සංඛ්‍යාත ව්‍යාප්තියක්‌ ගොඩනගන්න.",
        "(5)",
        "(ii) මධ්‍යන්‍යය, මධ්‍යස්ථය හා මාතය ගණනය කරන්න. (9)",
        "(ili) 6 වන දශමකය හා 77 වන ප්‍රතිශතකය සොයන්න.",
    ])
    marks = [line.split(")")[0] + ")" for line in repair(text).split("\n")]
    assert marks == ["01)", "අ)", "(i)", "(ii)", "(iii)", "02)", "අ)", "ආ)", "(i)", "(ii)",
                     "(iii)", "(iv)", "ඉ)", "(i)", "08)", "(i)", "(5)", "(ii)", "(iii)"]


def test_numbered_options_in_brackets_are_not_roman():
    text = "(1) අවිනිශ්චිත තත්වයන්\n(2) දත්ත රැස්‌ කිරීම\n(3) පුරෝකථනය කිරීම"
    assert repair(text) == text


def test_both_layouts_are_read_and_the_one_keeping_markers_wins(monkeypatch):
    reads = {None: "2) පහත පිළිතුරු\nඅවිනිශ්චිත තත්වයන්\nදත්ත රැස්‌ කිරීම",
             6: "2) පහත පිළිතුරු\n1, අවිනිශ්චිත තත්වයන්\n2. දත්ත රැස්‌ කිරීම"}
    asked = []

    def fake(image, lang="eng", psm=None):
        asked.append((lang, psm))
        return reads[psm]

    monkeypatch.setattr(extract, "ocr_image", fake)
    assert extract.ocr_sinhala_page(object()).split("\n")[1] == "1. අවිනිශ්චිත තත්වයන්"
    assert asked == [("sin+eng", None), ("sin+eng", 6)]


def test_sinhala_paper_and_part_headings():
    paper2 = "12 ශ්‍රේණිය පළමු වාර පරීක්ෂණය ව්‍යාපාර සංඛ්‍යානය = 2 පත්‍රය කාලය = පැය තුනයි"
    assert extract._heading(paper2)["label"] == "Paper 2"
    assert extract._heading("| කොටස")["label"] == "Part I"
    assert extract._heading("1! කොටස")["label"] == "Part II"
    assert extract._heading("II කොටස")["label"] == "Part II"
    # The instructions talk about the parts; they are not one.
    assert extract._heading(
        "මෙම ප්‍රශ්න පත්‍රය I හා II ලෙස කොටස් දෙකකින් සමන්විත වන අතර") is None
    # A part's last marks, alone on a line, are not glued to the heading.
    assert extract.join_lone_markers("(3)\nII කොටස\n05)") == "(3)\nII කොටස\n05)"


def test_choose_the_answer_misread_still_says_mcq():
    assert extract._MCQ_HINT.search("ගැළපෙණ පිළිතුර තොරන්න,")


def test_grade_and_subject_from_a_sinhala_paper():
    meta = extract.detect_paper_meta(
        "12 ශ්‍රේණිය පළමු වාර පරීක්ෂණය ව්‍යාපාර සංඛ්‍යානය - 1 පත්‍රය කාලය - පැය දෙකයි",
        filename="12 ශ්_රේණිය 1 වන වාරය (ප්_රශ්න පත්_රය).pdf")
    assert (meta["grade"], meta["subject"]) == ("G12", "Business Statistics")


def test_an_ellipsis_in_an_option_is_not_a_blank():
    assert not extract._has_blank("6 ශ්‍රේණියේ පංති කාමර 6-A, 6-B.........,6-F ලෙස නම්‌ කිරීම.")
    assert extract._has_blank('Perhaps you ..................."')
    assert extract._has_blank("______ an important tournament")
    _stem, options = extract.split_mcq_options("\n".join([
        "පහත පිළිනුරු අතුරින්‌ නාමික පරිමාණයට අයන්‌ නොචන පිළිතුර තෝරන්න.",
        "1. සංචාරක නිකේනනයක කාමර අංක.",
        "2. ස්ත්‍රී පුරුෂ බව.",
        "3. විවාහක අවිවාහක බව.",
        "4. 6 ශ්‍රේණියේ පංති කාමර 6-A, 6-B.........,6-F ලෙස නම්‌ කිරීම.",
        "5. ශ්‍රී ලංකාවේ පාසල්‌ ආදී වශයෙන්‌ වර්ග නිරීම."]))
    assert len(options) == 5


def test_the_real_scan_reads_as_its_two_papers():
    """The whole paper as Sinhala+English read it (one-block layout, before
    any repair) - a shortened copy would not do: the paper's "1)" question
    numbers only beat its "1."-"5." option numbers by counting up further."""
    text = (ROOT / "tests" / "data" / "business_statistics_g12_ocr.txt").read_text(encoding="utf-8")
    result = extract.structure(repair(text))
    assert [(s["label"], s["kind"], s["questions"]) for s in result["sections"]] == [
        ("Paper 1", "mcq", 50), ("Part I", "structured", 4), ("Part II", "structured", 4)]
    mcqs = [q for q in result["questions"] if q["section"] == "Paper 1"]
    with_five = [q["question_number"] for q in mcqs
                 if len(extract.split_mcq_options(q["text"])[1]) == 5]
    assert len(with_five) == 50
    numbers = [q["question_number"] for q in result["questions"] if q["section"] != "Paper 1"]
    assert numbers[:20] == [
        "01", "01(අ)", "01(අ)(i)", "01(අ)(ii)", "01(අ)(iii)", "01(ආ)", "01(ආ)(i)", "01(ආ)(ii)",
        "02", "02(අ)", "02(අ)(i)", "02(අ)(ii)", "02(ආ)", "02(ආ)(i)", "02(ආ)(ii)", "02(ආ)(iii)",
        "02(ආ)(iv)", "02(ඉ)", "02(ඉ)(i)", "02(ඉ)(ii)"]
    assert numbers[-4:] == ["08", "08(i)", "08(ii)", "08(iii)"]
