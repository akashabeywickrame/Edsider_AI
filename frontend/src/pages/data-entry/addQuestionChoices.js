// What "Add a question the extractor missed" may add, worked out from the
// paper itself - so nobody types a question number. A whole question can
// only take a number the paper is missing (a gap between two questions) or
// the next one at the end of a part; a part of a question can only take a
// label missing from its siblings or the next one, in the style the paper
// already uses for them - (a)/(b), (i)/(ii), (1)/(2) or අ/ආ. Each choice
// also says where the question goes (after_seq), so a missed question 7
// lands between 6 and 8 rather than at the end of the paper.

// Sinhala independent vowels in dictionary order - how a Sinhala-medium
// paper letters its parts (the extractor's own "sinhala" kind).
const SINHALA = ['අ', 'ආ', 'ඇ', 'ඈ', 'ඉ', 'ඊ', 'උ', 'ඌ', 'ඍ', 'ඎ', 'ඏ', 'ඐ', 'එ', 'ඒ', 'ඓ', 'ඔ', 'ඕ', 'ඖ']
const ROMAN = ['i', 'ii', 'iii', 'iv', 'v', 'vi', 'vii', 'viii', 'ix', 'x', 'xi', 'xii', 'xiii', 'xiv', 'xv',
  'xvi', 'xvii', 'xviii', 'xix', 'xx']

// The last question in `node`'s own subtree, in the page's tree order - a
// new question placed "after question 6" goes after 6's parts as well.
function lastInSubtree(tree, node) {
  const i = tree.indexOf(node)
  let last = node
  for (let j = i + 1; j < tree.length && (tree[j].depth || 0) > (node.depth || 0); j++) last = tree[j]
  return last
}

// "07" after "06": keep the paper's zero-padding.
function formatNumber(n, like) {
  const s = String(n)
  return like && /^0\d/.test(like) ? s.padStart(like.length, '0') : s
}

const isWhole = (q) => !q.depth

// Whole questions: for each run of the paper's parts (`parts` as the page
// groups them), every number missing between two questions, any missing
// before the first, and the next number at the end.
export function wholeQuestionChoices(tree, parts) {
  if (!tree.length) return [{ label: 'Question 1 - the first question', number: '1', section: null, afterSeq: undefined }]
  const choices = []
  for (const part of parts) {
    const where = part.key || 'the paper'
    const tops = part.questions.filter(isWhole)
    const numbered = tops.filter((q) => /^\d+$/.test(q.question_number || ''))
    if (!numbered.length) {
      // No printed numbers to follow (a paper read without them): the next
      // position is all that can honestly be offered.
      const last = tops.length ? lastInSubtree(tree, tops[tops.length - 1]) : null
      choices.push({
        label: `A new question at the end of ${where}`,
        number: String(tops.length + 1),
        section: part.key,
        afterSeq: last ? last.seq : undefined,
      })
      continue
    }
    const first = numbered[0]
    const firstN = Number(first.question_number)
    const beforeFirst = tree.indexOf(first) > 0 ? tree[tree.indexOf(first) - 1].seq : 0
    for (let n = 1; n < firstN; n++) {
      const number = formatNumber(n, first.question_number)
      choices.push({ label: `Question ${number} - before ${first.question_number} (${where})`, number, section: part.key, afterSeq: beforeFirst })
    }
    numbered.forEach((q, i) => {
      const next = numbered[i + 1]
      const n = Number(q.question_number)
      const afterSeq = lastInSubtree(tree, q).seq
      if (next) {
        for (let m = n + 1; m < Number(next.question_number); m++) {
          const number = formatNumber(m, q.question_number)
          choices.push({
            label: `Question ${number} - between ${q.question_number} and ${next.question_number} (${where})`,
            number,
            section: part.key,
            afterSeq,
          })
        }
      } else {
        const number = formatNumber(n + 1, q.question_number)
        choices.push({ label: `Question ${number} - at the end of ${where}`, number, section: part.key, afterSeq })
      }
    })
  }
  return choices
}

// The label in a part's number, given its question's: "5(a)" under "5" is
// "a"; "5(a)(ii)" under "5(a)" is "ii".
function ownLabel(child, parent) {
  const num = child.question_number || ''
  const base = parent.question_number || ''
  const rest = base && num.startsWith(base) ? num.slice(base.length) : num
  const m = /^\(([^()]+)\)$/.exec(rest) || /\(([^()]+)\)$/.exec(num)
  return m ? m[1] : null
}

function styleOf(labels, parentDepth) {
  if (!labels.length) return parentDepth >= 1 ? 'roman' : 'alpha'
  if (labels.every((l) => /^\d+$/.test(l))) return 'digit'
  if (labels.every((l) => SINHALA.includes(l))) return 'sinhala'
  const lower = labels.map((l) => l.toLowerCase())
  const upper = labels.every((l) => l === l.toUpperCase())
  // "i" alone could be either; only a real roman run settles it, and a
  // lone "(i)" is far likelier the first roman sub-part.
  const roman = lower.every((l) => ROMAN.includes(l)) && (lower.some((l) => l.length > 1) || lower.length === 1)
  if (roman) return upper ? 'ROMAN' : 'roman'
  return upper ? 'ALPHA' : 'alpha'
}

function labelAt(style, n) {
  switch (style) {
    case 'digit':
      return String(n)
    case 'sinhala':
      return SINHALA[n - 1]
    case 'roman':
      return ROMAN[n - 1]
    case 'ROMAN':
      return ROMAN[n - 1]?.toUpperCase()
    case 'ALPHA':
      return String.fromCharCode(64 + n)
    default:
      return String.fromCharCode(96 + n)
  }
}

function indexOf(style, label) {
  switch (style) {
    case 'digit':
      return Number(label)
    case 'sinhala':
      return SINHALA.indexOf(label) + 1
    case 'roman':
    case 'ROMAN':
      return ROMAN.indexOf(label.toLowerCase()) + 1
    default:
      return label.toLowerCase().charCodeAt(0) - 96
  }
}

// Questions (and parts) a new part can go under: anything above the
// deepest level the extractor splits to (two levels below a question).
export function partParents(tree) {
  return tree.filter((q) => (q.depth || 0) < 2)
}

// The labels a new part under `parent` may take.
export function partChoices(tree, parent) {
  const children = tree.filter((q) => q.parent_seq === parent.seq)
  const labels = children.map((c) => ownLabel(c, parent)).filter(Boolean)
  const style = styleOf(labels, parent.depth || 0)
  const at = new Map(children.map((c) => [indexOf(style, ownLabel(c, parent) || ''), c]))
  const known = [...at.keys()].filter((n) => n > 0).sort((a, b) => a - b)
  const highest = known.length ? known[known.length - 1] : 0
  const base = parent.question_number || ''
  const choices = []
  for (let n = 1; n <= highest + 1; n++) {
    if (at.has(n)) continue
    const label = labelAt(style, n)
    if (!label) break
    // Straight after the part before it (and that part's own parts), or
    // straight after the question itself for its first part.
    const before = known.filter((k) => k < n).pop()
    const afterSeq = before ? lastInSubtree(tree, at.get(before)).seq : parent.seq
    const after = before ? `after (${labelAt(style, before)})` : 'the first part'
    choices.push({
      label: n > highest ? `(${label}) - ${highest ? after : 'the first part'}` : `(${label}) - missing, ${after}`,
      number: `${base}(${label})`,
      afterSeq: n > highest ? lastInSubtree(tree, parent).seq : afterSeq,
    })
  }
  return choices
}
