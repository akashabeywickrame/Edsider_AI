"""OCR output that came back as HTML -> the plain text a question is stored
as. Shared by EdsiderAI (src/intake/extract.py, "Paste text") and the local
reader on data-entry computers (local_reader/ocr_html.py, an identical copy:
that folder is copied to other computers on its own and cannot import from
this repo - tests/test_local_reader.py fails if the two files differ).
Standard library only, for the same reason.

Surya, the vision OCR the local reader runs, writes a page as HTML: one
<p>/<li> per paragraph, <sub>/<sup> for formulas in a sentence, <math> with
LaTeX for an equation, <table> for a table, <img alt="..."> for a picture.
Measured on a real scanned Sinhala A/L Chemistry paper, the reading itself
was right - "N<sub>2</sub>(g)", "dm<sup>3</sup>", "N_2(g) + 3H_2(g)
\\rightleftharpoons 2NH_3(g)", "\\Delta H^\\circ = -90 \\text{ kJ mol}^{-1}"
- and the damage was all in turning it into text: tags stripped, so "dm3",
"6.022 x 1023", "mol-1", and equations left as raw LaTeX.

So here:
- <sub>/<sup> become the Unicode subscript/superscript characters the
  question editor itself saves (frontend/src/lib/richText.js): N₂, dm³,
  Fe³⁺. Unicode has no subscript for most capitals or for "c", so such a
  run stays plain ("KC") - the same "leave what cannot be shown alone" rule
  the editor follows.
- <math> becomes one line of readable text (latex_to_text).
- A drawn structural formula is left out. Surya "reads" a benzene ring
  drawing as a stack of rows joined by bond lines ("CH3 | C6H5 | CHCH2OH
  | OH"), which as text is a different molecule (the ring's substitution
  pattern and a C=O drawn below its carbon are lost). Like a table, data
  entry adds it to the question as a picture.
- A table's content is left out unless its rows are questions (see
  rows_are_questions), and a picture's made-up description (<img alt>)
  is dropped.
"""

import html as _html
import re

# ------------------------------------------------------------ tables

# Questions laid out in a grid ("26. <statement> | <statement>" - a real
# Political Science paper) are questions, not a table: rows whose first
# cell starts with a numbered item, when the numbers carry on the paper's
# numbering. A data table numbers its own items from 1 (Grade 13 Biology's
# "1. First product of CO2 fixation | C3 | C4"), and a bare "1" alone in
# its cell is never a question (Political Science's page-6 legend, "1 |
# true | true", is a real table of answer choices). Not handled: a paper
# whose very first questions sit in a grid numbered from 1 - none of the
# stored papers does that.
NUMBERED_CELL = re.compile(r"^\s*\(?(\d{1,3})\s*[.)]\s*\S")


def rows_are_questions(first_cells):
    """Whether 2+ rows of a table open with numbered items that carry on
    the paper's question numbering (start after 1)."""
    numbers = [int(m.group(1)) for m in (NUMBERED_CELL.match(c or "") for c in first_cells) if m]
    return len(numbers) >= 2 and numbers[0] > 1


# ------------------------------------------------- sub/superscripts

# The question editor's own maps (richText.js SUPER_MAP / SUB_MAP), plus
# the further lowercase letters Unicode does have - so K_sp is Kₛₚ and K_p
# is Kₚ rather than Ksp / Kp.
_SUPER = dict(zip("0123456789+-=()", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾"))
_SUPER.update({"n": "ⁿ", "i": "ⁱ", "x": "ˣ", "°": "°", "−": "⁻", "–": "⁻", " ": ""})
_SUB = dict(zip("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎"))
_SUB.update(dict(zip("aehijklmnoprstuvx", "ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ")))
_SUB.update({"−": "₋", "–": "₋", " ": ""})


def _script(text, table, lower):
    """`text` in Unicode sub/superscript characters, or None when any of
    it has no such character - a half-converted run ("K꜀p") would be worse
    than a plain one."""
    out = []
    for ch in text:
        mapped = table.get(ch.lower() if lower else ch)
        if mapped is None:
            return None
        out.append(mapped)
    return "".join(out)


def subscript(text):
    """N + "2" -> N₂. Plain when it cannot all be shown: one character is
    appended as it is ("K" + "C" -> "KC"), more is put in brackets so it
    still reads as belonging to what it follows ("ΔH°(CCl₄(g))")."""
    text = text.strip()
    if not text:
        return ""
    mapped = _script(text, _SUB, lower=False) or _script(text, _SUB, lower=True)
    if mapped is not None:
        return mapped
    return text if len(text) == 1 else f"({text})"


def superscript(text):
    """10 + "23" -> 10²³, "-1" -> ⁻¹, "3+" -> ³⁺. Plain "^(...)" when it
    cannot all be shown, so an exponent is never mistaken for a
    coefficient ("10^23" never becomes "1023")."""
    text = text.strip()
    if not text:
        return ""
    # A lone superscript letter "o" is the paper's degree / standard-state
    # circle read as a letter - the Chemistry paper's "ΔH<sup>o</sup>".
    if text in ("°", "∘", "o"):
        return "°"
    mapped = _script(text, _SUPER, lower=False)
    if mapped is not None:
        return mapped
    return f"^{text}" if len(text) == 1 else f"^({text})"


# ------------------------------------------------------------ LaTeX

# Relations and operators: written with a space either side, which is how
# a paper prints them ("N₂(g) + 3H₂(g) ⇌ 2NH₃(g)", "6.022 × 10²³").
_SPACED = {
    "rightleftharpoons": "⇌", "leftrightharpoons": "⇌", "rightleftarrows": "⇄",
    "rightarrow": "→", "longrightarrow": "→", "to": "→", "Rightarrow": "⇒",
    "leftarrow": "←", "longleftarrow": "←", "leftrightarrow": "↔",
    "times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠",
    "approx": "≈", "equiv": "≡", "propto": "∝", "sim": "~", "simeq": "≃",
    "ll": "≪", "gg": "≫", "lt": "<", "gt": ">",
}
_SYMBOL = {
    "Delta": "Δ", "delta": "δ", "alpha": "α", "beta": "β", "gamma": "γ",
    "Gamma": "Γ", "theta": "θ", "lambda": "λ", "mu": "μ", "nu": "ν",
    "pi": "π", "rho": "ρ", "sigma": "σ", "Sigma": "Σ", "tau": "τ",
    "phi": "φ", "Phi": "Φ", "omega": "ω", "Omega": "Ω", "epsilon": "ε",
    "varepsilon": "ε", "eta": "η", "kappa": "κ", "chi": "χ", "psi": "ψ",
    "partial": "∂", "infty": "∞", "circ": "°", "degree": "°", "prime": "′",
    "ldots": "…", "cdots": "⋯", "dots": "…", "angle": "∠", "triangle": "△",
    "%": "%", "{": "{", "}": "}", "$": "$", "&": "&", "#": "#", "_": "_",
    "|": "‖", "vert": "|", "Vert": "‖", "lvert": "|", "rvert": "|",
    "langle": "⟨", "rangle": "⟩", "lbrace": "{", "rbrace": "}",
}
_SPACE = {",", ";", ":", "!", " ", "quad", "qquad", "enspace", "thinspace"}
# Commands whose argument is shown as it is - the formatting is not kept.
_TEXT_ARG = {"text", "textrm", "textnormal", "mathrm", "mathbf", "textbf",
             "mathit", "textit", "mathsf", "mathtt", "operatorname", "mbox",
             "rm", "bf", "boldsymbol", "underline", "overline", "bar", "hat",
             "vec", "dot", "ddot", "tilde", "widehat", "overrightarrow", "ce"}
# Sizing and spacing that mean nothing in plain text.
_IGNORE = {"left", "right", "big", "Big", "bigg", "Bigg", "bigl", "bigr",
           "Bigl", "Bigr", "displaystyle", "textstyle", "limits", "nolimits",
           "middle", "hline", "centering", "nonumber"}

# A row that is nothing but a bond line - how Surya draws the bonds of a
# structure it laid out as a stack ("|", "||", "/", "\\").
_BOND_ROW = re.compile(r"^(?:\||\\vert|\\\||/|\\backslash|\\setminus|\\diagup|\\diagdown|\s)+$")
_ENV = re.compile(r"\\begin\{(array|matrix|pmatrix|bmatrix|aligned|align\*?|gathered|cases|split)\}"
                  r"(\{[^}]*\})?(.*?)\\end\{\1\}", re.DOTALL)


def is_drawn_structure(latex):
    """Whether this LaTeX is a structural formula Surya laid out as rows
    joined by bond lines - not something to show as text."""
    for m in _ENV.finditer(latex or ""):
        rows = re.split(r"\\\\", m.group(3))
        if any(_BOND_ROW.match(r.replace("&", "").strip()) for r in rows if r.strip()):
            return True
    return False


class _Latex:
    """A small reader for the LaTeX Surya writes - groups, commands,
    ^ and _ - into one line of text. Anything it does not know keeps its
    name ("\\log" -> "log"), so nothing silently disappears."""

    def __init__(self, src):
        self.s = src
        self.i = 0

    def parse(self, stop=None):
        out = []
        while self.i < len(self.s):
            c = self.s[self.i]
            if stop and c == stop:
                self.i += 1
                return "".join(out)
            if c == "{":
                self.i += 1
                out.append(self.parse("}"))
            elif c == "}":
                self.i += 1  # unbalanced - skip it rather than fail
            elif c == "^":
                self.i += 1
                out.append(superscript(self.argument()))
            elif c == "_":
                self.i += 1
                out.append(subscript(self.argument()))
            elif c == "\\":
                out.append(self.command())
            elif c == "&":
                self.i += 1
                out.append(" ")
            elif c == "~":
                self.i += 1
                out.append(" ")
            else:
                self.i += 1
                out.append(c)
        return "".join(out)

    def argument(self):
        """What ^ or _ applies to: a {group}, a command, or one character."""
        while self.i < len(self.s) and self.s[self.i] == " ":
            self.i += 1
        if self.i >= len(self.s):
            return ""
        c = self.s[self.i]
        if c == "{":
            self.i += 1
            return self.parse("}")
        if c == "\\":
            return self.command()
        self.i += 1
        return c

    def group(self):
        """A required {argument}, read as text."""
        while self.i < len(self.s) and self.s[self.i] == " ":
            self.i += 1
        if self.i < len(self.s) and self.s[self.i] == "{":
            self.i += 1
            return self.parse("}")
        return self.argument()

    def optional(self):
        if self.i < len(self.s) and self.s[self.i] == "[":
            end = self.s.find("]", self.i)
            if end != -1:
                inner = _Latex(self.s[self.i + 1:end]).parse()
                self.i = end + 1
                return inner
        return None

    def command(self):
        self.i += 1  # the backslash
        if self.i >= len(self.s):
            return ""
        m = re.match(r"[A-Za-z]+\*?", self.s[self.i:])
        if m:
            name = m.group(0).rstrip("*")
            self.i += len(m.group(0))
            # LaTeX ignores spaces after a command word ("\Delta H" is ΔH) -
            # but only skipped before a letter or digit here, so "\circ = "
            # keeps the space a reader expects ("ΔH° = -90").
            j = self.i
            while j < len(self.s) and self.s[j] == " ":
                j += 1
            if name not in _SPACED and j < len(self.s) and self.s[j].isalnum():
                self.i = j
        else:
            name = self.s[self.i]
            self.i += 1
            if name == "\\":
                return "\n"  # a new row
        if name in _SPACED:
            return f" {_SPACED[name]} "
        if name in _SYMBOL:
            return _SYMBOL[name]
        if name in _SPACE:
            return " "
        if name in _IGNORE:
            return ""
        if name in _TEXT_ARG:
            return self.group()
        if name in ("frac", "dfrac", "tfrac", "cfrac"):
            top, bottom = self.group(), self.group()
            return f"{_operand(top)}/{_operand(bottom, denominator=True)}"
        if name == "sqrt":
            index = self.optional()
            body = self.group()
            return (index or "") + "√" + _operand(body)
        if name in ("xrightarrow", "xleftarrow", "xrightleftharpoons"):
            below = self.optional()
            above = self.group()
            arrow = {"xrightarrow": "→", "xleftarrow": "←"}.get(name, "⇌")
            label = ", ".join(x for x in (above.strip(), (below or "").strip()) if x)
            return f" {arrow}({label}) " if label else f" {arrow} "
        if name in ("begin", "end"):
            self.group()  # the environment's name
            if name == "begin":
                self.optional()
                # an array's column spec, "{c}" / "{lcr}"
                if re.match(r"\s*\{[lcr|\s]*\}", self.s[self.i:]):
                    self.group()
            return "\n"
        return name  # unknown: keep its name rather than lose it


def _operand(text, denominator=False):
    """A fraction's top or bottom, bracketed when it is more than one
    term - "(x + 1)/2", "[NH₃]²/([N₂][H₂]³)" - so it cannot be misread."""
    text = text.strip()
    plain = re.fullmatch(r"[^\s+\-−×·/=()\[\]]+", text)
    if plain:
        return text
    one_bracket = re.fullmatch(r"[\[(][^\[\]()]*(?:\([^()]*\))*[^\[\]()]*[\])][^\s+\-−×·/=()\[\]]*", text)
    if one_bracket and not denominator:
        return text
    if one_bracket and denominator and not re.search(r"[\])][\[(]", text):
        return text
    return f"({text})"


def latex_to_text(latex):
    """One line of readable text from an equation's LaTeX:
    "N_2(g) + 3H_2(g) \\rightleftharpoons 2NH_3(g)" -> "N₂(g) + 3H₂(g) ⇌ 2NH₃(g)",
    "\\Delta H^\\circ = -90 \\text{ kJ mol}^{-1}" -> "ΔH° = -90 kJ mol⁻¹".
    A layout over several rows (an aligned derivation) keeps one line per
    row. A drawn structure is the caller's to leave out (is_drawn_structure)."""
    text = _Latex(latex or "").parse()
    lines = []
    for ln in text.split("\n"):
        ln = re.sub(r"[ \t]{2,}", " ", ln)
        ln = re.sub(r"([(\[])\s+", r"\1", re.sub(r"\s+([)\]])", r"\1", ln)).strip()
        if ln:
            lines.append(ln)
    return "\n".join(lines)


# ------------------------------------------------------------- HTML

# Block-level tags - each one starts a new line, which is what keeps "1." /
# "A." / "අ)" at a line start, where segment() and the part/option markers
# look for them.
_BLOCK_TAG = re.compile(r"</?(?:p|li|ul|ol|br|div|h[1-6]|tr|table|thead|tbody)\b[^>]*>", re.IGNORECASE)
_ANY_TAG = re.compile(r"<[^>]+>")
# What says "this is HTML, not plain text" - a closing block tag, or the
# inline tags Surya writes formulas and pictures with. Plain pasted text
# ("a < b", "x > 3") has none of them and comes back untouched.
_LOOKS_LIKE_HTML = re.compile(r"</(?:p|li|td|th|tr|table|h[1-6]|math|sub|sup)\s*>|<img\b", re.IGNORECASE)
_BULLET = re.compile(r"^[•●▪‣⁃]\s*")
_TABLE_HTML = re.compile(r"<table\b.*?</table\s*>", re.IGNORECASE | re.DOTALL)
_CELL_END = re.compile(r"</t[dh]\s*>", re.IGNORECASE)
_HTML_ROW = re.compile(r"<tr\b.*?</tr\s*>", re.IGNORECASE | re.DOTALL)
_HTML_CELL = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]\s*>", re.IGNORECASE | re.DOTALL)
_MATH = re.compile(r"<math\b([^>]*)>(.*?)</math\s*>", re.IGNORECASE | re.DOTALL)
_SUBSUP = re.compile(r"<(sub|sup)\b[^>]*>(.*?)</\1\s*>", re.IGNORECASE | re.DOTALL)
_IMG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)


def _drop_unless_questions(match):
    table = match.group(0)
    first_cells = []
    for row in _HTML_ROW.findall(table):
        cells = [_html.unescape(_ANY_TAG.sub("", c)).strip() for c in _HTML_CELL.findall(row)]
        first_cells.append(next((c for c in cells if c), ""))
    return table if rows_are_questions(first_cells) else "\n"


def _math(match):
    latex = _html.unescape(match.group(2))
    if is_drawn_structure(latex):
        return "\n"
    # "<" / ">" from an equation ("K_{sp} < ...") are escaped back, so the
    # tag-stripping after this cannot take "< ... >" for a tag.
    text = _html.escape(latex_to_text(latex), quote=False)
    if "display" in match.group(1).lower() and "block" in match.group(1).lower():
        return f"\n{text}\n"
    return text.replace("\n", " ")


def _subsup(match):
    inner = _html.unescape(_ANY_TAG.sub("", match.group(2)))
    out = subscript(inner) if match.group(1).lower() == "sub" else superscript(inner)
    return _html.escape(out, quote=False)


def html_to_lines(text, keep_tables=False):
    """Plain lines from OCR output that came back as HTML - one line per
    paragraph or list item, formulas and equations as readable text (see
    the module's notes), entities decoded, and bullet glyphs ("•")
    stripped so the marker after one is the first thing on its line. Text
    that is not HTML is returned unchanged, so pasting plain text never
    loses anything.

    A table's content is left out unless its rows are questions, or
    `keep_tables` - the local reader keeps a paper's header table that
    way. A kept table is one line per row, cells separated by spaces."""
    if not _LOOKS_LIKE_HTML.search(text or ""):
        return text
    # Equations first: their LaTeX can hold "<" and table-like "&".
    text = _MATH.sub(_math, text)
    if not keep_tables:
        text = _TABLE_HTML.sub(_drop_unless_questions, text)
    text = _IMG.sub("\n", text)
    text = _SUBSUP.sub(_subsup, text)
    text = _CELL_END.sub(" ", text)
    lines = []
    for chunk in _BLOCK_TAG.split(text):
        for piece in _html.unescape(_ANY_TAG.sub("", chunk)).split("\n"):
            line = _BULLET.sub("", re.sub(r"[ \t]{2,}", " ", piece).strip())
            if line:
                lines.append(line)
    return "\n".join(lines)
