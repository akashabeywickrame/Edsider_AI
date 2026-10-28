// How a question's number is shown on the paper: a whole question by its
// number alone ("05" -> "5", "(01)" -> "1"), a part by its own label only
// ("05(a)" -> "a", "05(a)(i)" -> "i") - its place under its question is
// shown by indenting it. The stored number stays whole: answers and the
// marking scheme are matched on it, and lists that mix questions and parts
// (the duplicate check, the marks list) still need it to tell them apart.
export function shownNumber(q) {
  const raw = (q.question_number || '').trim()
  if (!raw) return String(q.seq ?? '')
  if ((q.depth || 0) > 0 || q.parent_seq != null) {
    const labels = raw.match(/\(([^()]+)\)/g)
    return labels ? labels[labels.length - 1].slice(1, -1).trim() : raw
  }
  const whole = /^\(?\s*0*(\d+)\s*\)?$/.exec(raw)
  return whole ? whole[1] : raw
}
