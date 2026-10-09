import { useEffect, useState } from 'react'
import { api } from '../../lib/api.js'

// An early, informational duplicate check - runs automatically as soon
// as a paper's questions are extracted, well before anyone has spent
// the ~40 minutes to an hour correcting each one by hand. The company
// pays the teacher once a paper is handed over, so whoever is deciding
// whether that review time is worth spending needs this heads-up
// before committing to it, not after.
//
// Deliberately informational only (see service.py's
// precheck_document_duplicates): it never blocks anything below it,
// never requires a question marked reviewed first, and never changes
// the document's status. It runs on raw, uncorrected extraction text,
// so it is read as "worth a closer look", not as a verdict - the
// existing post-review check further down the page is what actually
// gates moving the paper forward.
export default function EarlyDuplicateCheck({ doc, questions }) {
  const { Notice, PairReview, QuestionCard } = window.EdSider
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [open, setOpen] = useState(false)

  useEffect(() => {
    if (!doc?.id) return
    setResult(null)
    setError(null)
    setBusy(true)
    api
      .precheckDocumentDuplicates(doc.id)
      .then(setResult)
      .catch((err) => setError(err.message))
      .finally(() => setBusy(false))
  }, [doc?.id])

  function brief(id) {
    const q = (questions || []).find((qq) => String(qq.id) === String(id))
    if (!q) return { id: String(id), text: '(question no longer on this paper)' }
    return {
      id: String(q.id),
      text: q.text || '',
      grade: doc.paper_grade,
      subject: doc.paper_subject,
      number: q.question_number || q.seq,
    }
  }

  if (busy) {
    return <Notice tone="info">Checking this paper against the question bank before you start reviewing...</Notice>
  }
  if (error) {
    return (
      <Notice tone="error" title="Could not run the early duplicate check">
        {error}
      </Notice>
    )
  }
  if (!result) return null

  const withinCount = (result.within_paper?.exact_groups.length || 0)
    + (result.within_paper?.similar_pairs.length || 0)
  const bankCount = result.against_bank.length
  const nothingFound = withinCount === 0 && bankCount === 0

  return (
    <section className="es-card" aria-label="Early duplicate check" style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <div>
        <h2 className="scr-h2">Before you review this paper</h2>
        <p className="es-caption">
          Checked against the whole question bank and against itself, on the text as extracted -
          before any correction. Worth a look before spending time reviewing each question by
          hand, but it is a heads-up, not a verdict: this is uncorrected text, and the check after
          review (further down) is the one that actually confirms anything.
        </p>
      </div>

      {nothingFound ? (
        <Notice tone="success">
          Nothing in {result.questions_checked} question(s) looked like a repeat, from the bank or
          within this paper itself.
        </Notice>
      ) : (
        <Notice tone="warning" title={`${bankCount} question(s) may already be in the bank, ${withinCount} pair(s) look repeated within this paper`}>
          Click below to see which ones.
        </Notice>
      )}

      {!nothingFound ? (
        <button
          className="pi-exp"
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
        >
          {open ? 'Hide details' : 'Show details'}
          <span className="pi-exp__chev">{open ? 'Hide' : 'Show'}</span>
        </button>
      ) : null}

      {open ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          {bankCount > 0 ? (
            <div>
              <h3 className="scr-h3">Already in the bank</h3>
              {result.against_bank.map((entry) =>
                entry.matches.map((m, i) => (
                  <PairReview
                    key={`bank-${entry.question_id}-${i}`}
                    context="This paper's question vs. an existing one in the bank"
                    verdict={m.verdict}
                    score={m.score}
                    raw={m.raw_score}
                    reasons={m.reasons}
                    a={{
                      id: String(entry.question_id),
                      text: entry.text,
                      number: entry.question_number,
                      grade: doc.paper_grade,
                      subject: doc.paper_subject,
                    }}
                    b={{ id: m.id, text: m.text, grade: m.grade, subject: m.subject }}
                    readOnly
                  />
                )),
              )}
            </div>
          ) : null}

          {withinCount > 0 ? (
            <div>
              <h3 className="scr-h3">Repeated within this paper</h3>
              {result.within_paper.exact_groups.map((ids, i) => (
                <div
                  className="es-card"
                  key={`exact-${i}`}
                  style={{ display: 'flex', flexDirection: 'column', gap: 12 }}
                >
                  <span className="es-pill es-tone--dup">Exact copies</span>
                  <div
                    style={{
                      display: 'grid',
                      gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))',
                      gap: 14,
                    }}
                  >
                    {ids.map((id) => (
                      <QuestionCard key={id} {...brief(id)} />
                    ))}
                  </div>
                </div>
              ))}
              {result.within_paper.similar_pairs.map((p, i) => (
                <PairReview
                  key={`pair-${i}`}
                  verdict={p.verdict}
                  score={p.score}
                  raw={p.raw_score}
                  reasons={p.reasons}
                  a={brief(p.a)}
                  b={brief(p.b)}
                  readOnly
                />
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  )
}
