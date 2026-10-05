import { useEffect, useRef, useState } from 'react'
import { api } from '../../lib/api.js'
import './FindDuplicates.css'
import './WithinPaper.css'

const LIMIT = 50

// "G07" -> "Grade 7" - same bank grade codes CheckQuestion.jsx already
// decodes this way (CLAUDE.md: "stores grades as G06-G13").
function gradeLabel(code) {
  const m = /^G(\d{2})$/.exec(code || '')
  return m ? `Grade ${Number(m[1])}` : code
}

function pairKey(p) {
  return `${p.a.id}_${p.b.id}`
}

export default function WithinPaper() {
  const { PageHeader, PairReview, Notice } = window.EdSider

  const [facets, setFacets] = useState({ grades: [], subjects: [] })
  const [assessmentIds, setAssessmentIds] = useState([])

  const [grade, setGrade] = useState('')
  const [subject, setSubject] = useState('')
  const [titleContains, setTitleContains] = useState('')
  const [assessmentId, setAssessmentId] = useState('')
  const [threshold, setThreshold] = useState(0.95)
  const [useGuards, setUseGuards] = useState(true)
  const [showReviewed, setShowReviewed] = useState(false)

  const [sweep, setSweep] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [busyKey, setBusyKey] = useState(null)
  const [skipped, setSkipped] = useState(() => new Set())

  const debounceRef = useRef(null)
  const titleDebounceRef = useRef(null)

  useEffect(() => {
    api.questionFacets().then(setFacets).catch(() => {})
    api.listAssessmentIds().then(setAssessmentIds).catch(() => {})
  }, [])

  function load(overrides) {
    const params = {
      grade: grade || undefined,
      subject: subject || undefined,
      title_contains: titleContains || undefined,
      assessment_id: assessmentId || undefined,
      threshold,
      use_guards: useGuards,
      show_reviewed: showReviewed,
      limit: LIMIT,
      ...overrides,
    }
    setLoading(true)
    setError(null)
    api
      .withinPaperDuplicates(params)
      .then((res) => {
        setSweep(res)
        setSkipped(new Set())
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    load()
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current)
      if (titleDebounceRef.current) clearTimeout(titleDebounceRef.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [grade, subject, assessmentId, useGuards, showReviewed])

  function handleThresholdChange(e) {
    const next = Number(e.target.value)
    setThreshold(next)
    if (debounceRef.current) clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => load({ threshold: next }), 400)
  }

  function handleTitleChange(e) {
    const next = e.target.value
    setTitleContains(next)
    if (titleDebounceRef.current) clearTimeout(titleDebounceRef.current)
    titleDebounceRef.current = setTimeout(() => load({ title_contains: next || undefined }), 400)
  }

  async function handleDecision(pair, verdict) {
    const key = pairKey(pair)
    setBusyKey(key)
    setError(null)
    try {
      await api.createDecision({
        a: pair.a.id,
        b: pair.b.id,
        verdict,
        similarity: pair.score,
        guard_flags: pair.reasons.join('; '),
      })
      load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusyKey(null)
    }
  }

  function handleSkip(pair) {
    setSkipped((prev) => new Set(prev).add(pairKey(pair)))
  }

  function passageNote(pair) {
    if (!pair.shared_passage_with || pair.shared_passage_with.length === 0) return null
    const nums = pair.shared_passage_with.map((n) => `#${n}`).join(', ')
    return `These questions share a long passage with ${nums} on this paper - see whether the passage, not the question, is what matches.`
  }

  const shown = sweep?.pairs || []
  const pending = shown.filter((p) => !skipped.has(pairKey(p)))

  return (
    <>
      <PageHeader
        accent="Duplicates"
        title="within a paper"
        subtitle="The same question should never appear twice on one assessment. This checks each paper only against itself, not the rest of the bank - the same question turning up on a different paper is normal; see 'Also appears on assessment(s)' elsewhere for that."
      />

      <section className="es-card wp-filters" aria-label="Filters">
        <div className="wp-filters__grid">
          <div className="es-field">
            <label className="es-field__label" htmlFor="wp-grade">
              Grade
            </label>
            <select id="wp-grade" className="es-input" value={grade} onChange={(e) => setGrade(e.target.value)}>
              <option value="">All</option>
              {facets.grades.map((g) => (
                <option key={g} value={g}>
                  {gradeLabel(g)}
                </option>
              ))}
            </select>
          </div>

          <div className="es-field">
            <label className="es-field__label" htmlFor="wp-subject">
              Subject
            </label>
            <select id="wp-subject" className="es-input" value={subject} onChange={(e) => setSubject(e.target.value)}>
              <option value="">All</option>
              {facets.subjects.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>

          <div className="es-field">
            <label className="es-field__label" htmlFor="wp-title">
              Title code contains
            </label>
            <input
              id="wp-title"
              className="es-input"
              type="text"
              placeholder="e.g. Q3-2024"
              value={titleContains}
              onChange={handleTitleChange}
            />
          </div>

          <div className="es-field">
            <label className="es-field__label" htmlFor="wp-assessment">
              Assessment #
            </label>
            <select
              id="wp-assessment"
              className="es-input"
              value={assessmentId}
              onChange={(e) => setAssessmentId(e.target.value)}
            >
              <option value="">All</option>
              {assessmentIds.map((id) => (
                <option key={id} value={id}>
                  #{id}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div className="es-row wp-filters__row2">
          <label className="es-toggle">
            <input type="checkbox" checked={useGuards} onChange={(e) => setUseGuards(e.target.checked)} />
            <span aria-hidden="true" />
            Safety checks on
          </label>

          <div className="fd-slider">
            <label className="es-field__label" htmlFor="wp-threshold">
              Similarity threshold - {threshold.toFixed(2)}
            </label>
            <input
              id="wp-threshold"
              type="range"
              min={0.7}
              max={0.99}
              step={0.01}
              value={threshold}
              onChange={handleThresholdChange}
            />
          </div>

          <label className="es-toggle">
            <input type="checkbox" checked={showReviewed} onChange={(e) => setShowReviewed(e.target.checked)} />
            <span aria-hidden="true" />
            Also show reviewed pairs
          </label>
        </div>
      </section>

      {error ? (
        <Notice tone="error" title="Could not run the check">
          {error}
        </Notice>
      ) : null}

      {sweep ? (
        <section className="es-row fd-metrics" aria-label="Check totals">
          <div className="es-stat">
            <span className="es-stat__value">{sweep.papers_checked.toLocaleString()}</span>
            <span className="es-stat__label">papers checked</span>
          </div>
          <div className="es-stat">
            <span className="es-stat__value">{sweep.repeats_found.toLocaleString()}</span>
            <span className="es-stat__label">repeats found</span>
          </div>
        </section>
      ) : null}

      {!loading && sweep && sweep.papers_checked === 0 ? (
        <Notice tone="info">
          No assessment links with more than one comparable question were found for these
          filters.
        </Notice>
      ) : null}

      {loading ? <Notice tone="info">Checking each paper against itself...</Notice> : null}

      {!loading && sweep && sweep.papers_checked > 0 && pending.length === 0 ? (
        <Notice tone="success">
          {sweep.pending === 0
            ? 'No repeats found on these papers at this threshold.'
            : 'Every flagged pair at this threshold has been reviewed.'}
        </Notice>
      ) : null}

      {!loading && sweep && shown.length > 0 && sweep.pending > shown.length ? (
        <p className="es-caption">
          Showing the {shown.length} highest-scoring of {sweep.pending.toLocaleString()} pairs.
          Narrow the filters to see the rest.
        </p>
      ) : null}

      <div className="fd-list">
        {pending.map((pair) => {
          const key = pairKey(pair)
          return (
            <PairReview
              key={key}
              verdict={pair.verdict}
              score={pair.score}
              raw={pair.raw_score}
              reasons={pair.reasons}
              context={`Assessment #${pair.assessment_id}`}
              passageNote={passageNote(pair)}
              a={pair.a}
              b={pair.b}
              decision={
                pair.decision
                  ? {
                      verdict: pair.decision.verdict,
                      by: pair.decision.decided_by,
                      at: pair.decision.decided_at,
                    }
                  : null
              }
              onConfirm={() => handleDecision(pair, 'duplicate')}
              onDistinct={() => handleDecision(pair, 'distinct')}
              onSkip={() => handleSkip(pair)}
              readOnly={busyKey === key}
            />
          )
        })}
      </div>
    </>
  )
}
