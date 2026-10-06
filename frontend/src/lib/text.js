// Shared text helpers for the paper views.

// The extractor keeps the paper's own line wraps, so a sentence that was
// wrapped at a page margin arrives as several lines. Those are joined back
// into one line of running text. A line that starts a list item ("(i) ...",
// "a) ...", "1. ...") or a blank line still breaks, as the paper does.
const LIST_START = /^\s*(\(?[ivxIVX]{1,4}\)|\(?[a-zA-Z]\)|\(?\d{1,2}[.)]|[-•*])\s/
export function joinSoftBreaks(text) {
  if (!text) return text
  const out = []
  for (const line of text.split('\n')) {
    if (out.length && line.trim() && !LIST_START.test(line) && !/ \| /.test(line)
        && out[out.length - 1].trim() && !/ \| /.test(out[out.length - 1])) {
      out[out.length - 1] = `${out[out.length - 1].trimEnd()} ${line.trimStart()}`
    } else {
      out.push(line)
    }
  }
  return out.join('\n')
}
