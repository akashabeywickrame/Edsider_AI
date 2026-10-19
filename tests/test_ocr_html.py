"""Formulas and equations from OCR output (src/intake/ocr_html.py). Every
input here is Surya's real output for the scanned 2025 A/L Chemistry
paper (Sinhala medium), Part I page 4 and Part II page 1, copied verbatim;
the expected text is what the paper prints."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.intake import ocr_html
from src.intake.ocr_html import html_to_lines, latex_to_text


@pytest.mark.parametrize("latex, expected", [
    (r"R = 8.314 \text{ J K}^{-1} \text{ mol}^{-1}", "R = 8.314 J K⁻¹ mol⁻¹"),
    (r"N_A = 6.022 \times 10^{23} \text{ mol}^{-1}", "Nₐ = 6.022 × 10²³ mol⁻¹"),
    (r"N_2(g) + 3H_2(g) \rightleftharpoons 2NH_3(g)", "N₂(g) + 3H₂(g) ⇌ 2NH₃(g)"),
    (r"\text{NH}_3(\text{g})", "NH₃(g)"),
    (r"\Delta H^\circ = -90 \text{ kJ mol}^{-1}", "ΔH° = -90 kJ mol⁻¹"),
    (r"\Delta S^\circ = -200 \text{ J K}^{-1} \text{ mol}^{-1}", "ΔS° = -200 J K⁻¹ mol⁻¹"),
    (r"27^\circ\text{C}", "27°C"),
    (r"2\text{NH}_3(\text{g}) \rightleftharpoons \text{N}_2(\text{g}) + 3\text{H}_2(\text{g})",
     "2NH₃(g) ⇌ N₂(g) + 3H₂(g)"),
    # A fraction keeps its meaning on one line: the denominator, a product
    # of two terms, is bracketed so it is not read as (a/b)c.
    (r"\left( \frac{[\text{NH}_3(\text{g})]^2}{[\text{N}_2(\text{g})][\text{H}_2(\text{g})]^3} \right)",
     "([NH₃(g)]²/([N₂(g)][H₂(g)]³))"),
    # Unicode has no subscript capitals: a subscript that cannot all be
    # shown is bracketed after what it belongs to, never run into it.
    (r"\Delta H^\circ_{\text{CCl}_4(g)} = -95 \text{ kJ mol}^{-1}", "ΔH°(CCl₄(g)) = -95 kJ mol⁻¹"),
])
def test_the_papers_equations_read_as_printed(latex, expected):
    assert latex_to_text(latex) == expected


def test_an_exponent_that_cannot_be_shown_is_never_run_into_its_base():
    # "10^{2/3}" must not become "102/3".
    assert latex_to_text(r"10^{2/3}") == "10^(2/3)"
    assert latex_to_text(r"e^{-E_a/RT}") == "e^(-Eₐ/RT)"


def test_inline_formulas_keep_their_subscripts_and_charges():
    html = ("<p>5. (a) N<sub>2</sub>(g) හි 1.0 mol ක් සහ H<sub>2</sub>(g) හි 2.0 mol ක් "
            "පරිමාව 1.0 dm<sup>3</sup> වන භාජනයක්</p>"
            "<li>(3) Fe<sup>3+</sup> සහ Co<sup>2+</sup> NH<sub>4</sub>Cl/NH<sub>4</sub>OH</li>"
            "<li>I. AgBr(s) + I<sup>-</sup>(aq) → AgI(s) + Br<sup>-</sup>(aq)</li>"
            "<li>II. AgBr(s) හි K<sub>sp</sub> &gt; AgI(s) හි K<sub>sp</sub></li>"
            "<li>(iv) K<sub>C</sub> ගණනය කරන්න.</li>")
    assert html_to_lines(html).split("\n") == [
        "5. (a) N₂(g) හි 1.0 mol ක් සහ H₂(g) හි 2.0 mol ක් පරිමාව 1.0 dm³ වන භාජනයක්",
        "(3) Fe³⁺ සහ Co²⁺ NH₄Cl/NH₄OH",
        "I. AgBr(s) + I⁻(aq) → AgI(s) + Br⁻(aq)",
        "II. AgBr(s) හි Kₛₚ > AgI(s) හි Kₛₚ",
        "(iv) KC ගණනය කරන්න.",
    ]


def test_an_equation_on_its_own_line_stays_on_its_own_line():
    html = ('<p>(b) සලකන්න.</p><p><math display="block">N_2(g) + 3H_2(g) '
            '\\rightleftharpoons 2NH_3(g)</math></p><p>සමතුලිතතාවයේදී</p>')
    assert html_to_lines(html) == "(b) සලකන්න.\nN₂(g) + 3H₂(g) ⇌ 2NH₃(g)\nසමතුලිතතාවයේදී"


def test_a_less_than_in_an_equation_is_not_taken_for_a_tag():
    assert html_to_lines("<p><math>K_{sp} < K_c</math> and x > 2</p>") == "Kₛₚ < Kc and x > 2"


# Question 20's starting compound, as Surya laid out the benzene ring
# drawing: a stack of rows joined by bond lines.
DRAWN = r"""\begin{array}{c}
 \text{CH}_3 \\
 | \\
 \text{C}_6\text{H}_5 \\
 | \\
 \text{CHCH}_2\text{CH}_2\text{OH} \\
 | \\
 \text{OH}
 \end{array}
 \xrightarrow{
 \text{PCC}
 }
 \text{CH}_2\text{Cl}_2
 \end{array}"""


def test_a_drawn_structure_is_left_out_for_a_picture():
    # As text it would be a different molecule - the ring's substitution
    # pattern is lost - so it is left for data entry to add as a picture.
    assert ocr_html.is_drawn_structure(DRAWN)
    html = (f'<p>20. පහත දැක්වෙන ප්‍රතික්‍රියාව සලකන්න.</p>'
            f'<math display="block">{DRAWN}</math>'
            f'<p>මෙම ප්‍රතික්‍රියාවේ ප්‍රධාන ඵලය වනුයේ කුමක් ද?</p>')
    assert html_to_lines(html) == ("20. පහත දැක්වෙන ප්‍රතික්‍රියාව සලකන්න.\n"
                                   "මෙම ප්‍රතික්‍රියාවේ ප්‍රධාන ඵලය වනුයේ කුමක් ද?")


def test_an_aligned_equation_over_rows_is_not_a_drawing():
    latex = r"\begin{aligned} x &= 2 + 3 \\ &= 5 \end{aligned}"
    assert not ocr_html.is_drawn_structure(latex)
    assert latex_to_text(latex) == "x = 2 + 3\n= 5"


def test_a_pictures_made_up_description_is_dropped():
    html = ('<p>Chemistry II</p><img alt="Standard State Seal of the United States '
            'featuring the number \'02\'."/><h2>B කොටස — රචනා</h2>')
    assert html_to_lines(html) == "Chemistry II\nB කොටස — රචනා"


def test_plain_text_is_still_left_exactly_as_it_is():
    for text in ("1. a < b\n2. c > d", "x_2 and y^3 typed by hand", "5. Ksp of AgBr"):
        assert html_to_lines(text) == text
