// Quill's document model (a "Delta") -> the plain string intake_questions.text
// actually stores. Every format here is preserved by converting the
// formatted run into real Unicode characters that look the same in any
// plain-text reader - never by keeping HTML or discarding the format,
// since the column is plain text and a reviewer asked for what they see
// in the editor to be what is actually saved.
//
// Superscript/subscript use the dedicated superscript/subscript Unicode
// blocks (digits and a handful of letters only - CLAUDE.md's own example
// is exactly this: "a flattened exponent", x² rather than "x2"). Bold and
// italic use the Mathematical Alphanumeric Symbols block, which only
// covers the Latin alphabet and digits - a Sinhala or Tamil word, or any
// other script, passes through unstyled rather than silently losing
// characters, the same "leave what it doesn't understand alone" rule
// every map here follows. Underline and strikethrough instead stack a
// combining mark after each character (U+0332, U+0336), which works for
// any script at all since a combining mark attaches to whatever base
// character precedes it.

function buildLetterMap(upperStart, lowerStart, digitStart, overrides) {
  const map = {}
  for (let i = 0; i < 26; i++) {
    map[String.fromCharCode(65 + i)] = String.fromCodePoint(upperStart + i)
    map[String.fromCharCode(97 + i)] = String.fromCodePoint(lowerStart + i)
  }
  if (digitStart != null) {
    for (let i = 0; i < 10; i++) {
      map[String.fromCharCode(48 + i)] = String.fromCodePoint(digitStart + i)
    }
  }
  return Object.assign(map, overrides)
}

export const SUPER_MAP = {
  '0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴',
  '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹',
  '+': '⁺', '-': '⁻', '=': '⁼', '(': '⁽', ')': '⁾',
  n: 'ⁿ', i: 'ⁱ', x: 'ˣ',
}
export const SUB_MAP = {
  '0': '₀', '1': '₁', '2': '₂', '3': '₃', '4': '₄',
  '5': '₅', '6': '₆', '7': '₇', '8': '₈', '9': '₉',
  '+': '₊', '-': '₋', '=': '₌', '(': '₍', ')': '₎',
  a: 'ₐ', e: 'ₑ', x: 'ₓ',
}
// Unicode's italic block has no lowercase 'h' (the slot is unassigned -
// it reuses the pre-existing Planck-constant symbol instead); bold and
// bold-italic have no such gap.
const BOLD_MAP = buildLetterMap(0x1d400, 0x1d41a, 0x1d7ce)
const ITALIC_MAP = buildLetterMap(0x1d434, 0x1d44e, null, { h: 'ℎ' })
const BOLD_ITALIC_MAP = buildLetterMap(0x1d468, 0x1d482, null)

function mapChars(str, table) {
  return Array.from(str)
    .map((ch) => table[ch.toLowerCase()] || ch)
    .join('')
}

// Case-sensitive, unlike mapChars above - bold capital and lowercase
// letters are different Unicode characters, not a styling of one lookup.
function styleChars(str, table) {
  return Array.from(str)
    .map((ch) => table[ch] || ch)
    .join('')
}

function addCombining(str, mark) {
  // A combining mark on a newline does nothing useful and some renderers
  // show it as a stray dot sitting on its own line.
  return Array.from(str)
    .map((ch) => (ch === '\n' ? ch : ch + mark))
    .join('')
}

// A Quill Delta's ops in order -> one plain string. Embeds (images) have a
// non-string `insert` - none are used in this editor (images are their own
// attachment, not inline content), but a defensive empty string keeps a
// stray one from ever reaching the database as "[object Object]".
export function deltaToPlainText(delta) {
  const text = (delta.ops || [])
    .map((op) => {
      if (typeof op.insert !== 'string') return ''
      const attrs = op.attributes || {}
      let out = op.insert
      if (attrs.script === 'super') out = mapChars(out, SUPER_MAP)
      else if (attrs.script === 'sub') out = mapChars(out, SUB_MAP)
      else if (attrs.bold && attrs.italic) out = styleChars(out, BOLD_ITALIC_MAP)
      else if (attrs.bold) out = styleChars(out, BOLD_MAP)
      else if (attrs.italic) out = styleChars(out, ITALIC_MAP)
      if (attrs.underline) out = addCombining(out, '̲')
      if (attrs.strike) out = addCombining(out, '̶')
      return out
    })
    .join('')
  // Quill's document model always ends in a newline; that is its own
  // bookkeeping, not something the person typed.
  return text.replace(/\n$/, '')
}
