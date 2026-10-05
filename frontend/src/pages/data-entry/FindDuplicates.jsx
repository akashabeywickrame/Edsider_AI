import { useEffect, useRef, useState } from 'react'
import { api } from '../../lib/api.js'
import './FindDuplicates.css'

const LIMIT = 50

function pairKey(p) {
  return `${p.a.id}_${p.b.id}`
}

export default function FindDuplicates() {
  const { PageHeader, PairReview, Notice } = window.EdSider

  const [threshold, setThreshold] = useState(0.95)
  const [useGuards, setUseGuards] = useState(true)
  const [showReviewed, setShowReviewed] = useState(false)
  const [comparableCount, setComparableCount] = useState(null)

  const [sweep, setSweep] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [busyKey, setBusyKey] = useState(null)
  const [skipped, setSkipped] = useState(() => new Set())

  const debounceRef = useRef(null)

  useEffect(() => {
    api.health().then((h) => setComparableCount(h.comparable_questions)).catch(() => {})
  }, [])

  function load(nextThreshold) {
    const t = nextThreshold != null ? nextThreshold : threshold
    setLoading(true)
    setError(null)
    api
      .findDuplicates({ threshold: t, use_guards: useGuards, show_reviewed: showReviewed, limit: LIMIT })
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
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [useGuards, showReviewed])

  function handleThresholdChange(e) {
    const next = Number(e.target.value)
    setThreshold(next)
    if (debounceRef.current) clearTimeout(debounceRef.current)
    // A whole-bank sweep is 128 million pairs - debounce so dragging the
    // slider doesn't fire a sweep on every intermediate value.
    debounceRef.current = setTimeout(() => load(next), 400)
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

  const shown = sweep?.pairs || []
  const pending = shown.filter((p) => !skipped.has(pairKey(p)))
  const removed = sweep ? sweep.pairs_without_guards - sweep.pairs_with_guards : 0
  const bigBank = comparableCount != null && comparableCount > 1000

  return (
    <>
      <PageHeader
        accent="Find"
        title="duplicate questions"
        subtitle="Compares every comparable question in the bank against every other one. Turn the safety checks off to see what the raw similarity score alone would flag."
      />

      <section className="es-card fd-controls" aria-label="Filters">
        <div className="es-row fd-controls__row">
          <label className="es-toggle">
            <input
              type="checkbox"
              checked={useGuards}
              onChange={(e) => setUseGuards(e.target.checked)}
            />
            <span aria-hidden="true" />
            Safety checks on
          </label>

          <div className="fd-slider">
            <label className="es-field__label" htmlFor="fd-threshold">
              Similarity threshold - {threshold.toFixed(2)}
            </label>
            <input
              id="fd-threshold"
              type="range"
              min={0.7}
              max={0.99}
              step={0.01}
              value={threshold}
              onChange={handleThresholdChange}
            />
          </div>

          <label className="es-toggle">
            <input
              type="checkbox"
              checked={showReviewed}
              onChange={(e) => setShowReviewed(e.target.checked)}
            />
            <span aria-hidden="true" />
            Also show reviewed pairs
          </label>
        </div>
      </section>

      {bigBank && threshold < 0.92 ? (
        <Notice tone="warning">
          At {threshold.toFixed(2)} a bank of {comparableCount.toLocaleString()} questions
          produces tens of thousands of pairs, and below 0.90 well over a million. Raise the
          threshold, or expect this to take a while.
        </Notice>
      ) : null}

      {error ? (
        <Notice tone="error" title="Could not run the sweep">
          {error}
        </Notice>
      ) : null}

      {sweep ? (
        <section className="es-row fd-metrics" aria-label="Sweep totals">
          <div className="es-stat">
            <span className="es-stat__value">{sweep.pairs_with_guards.toLocaleString()}</span>
            <span className="es-stat__label">pairs flagged, safety checks on</span>
          </div>
          <div className="es-stat">
            <span className="es-stat__value">{sweep.pairs_without_guards.toLocaleString()}</span>
            <span className="es-stat__label">pairs flagged, safety checks off</span>
          </div>
          <div className="es-stat">
            <span className="es-stat__value">{removed.toLocaleString()}</span>
            <span className="es-stat__label">removed by safety checks</span>
          </div>
        </section>
      ) : null}

      {sweep && removed > 0 ? (
        <Notice tone="info">
          The safety checks removed {removed.toLocaleString()} pairs that look almost identical
          but test different things. Without them, those would be shown to a reviewer as
          duplicates.
        </Notice>
      ) : null}

      {loading ? <Notice tone="info">Comparing {(comparableCount || 0).toLocaleString()} questions...</Notice> : null}

      {!loading && sweep && pending.length === 0 ? (
        <Notice tone="success">
          {sweep.pending === 0
            ? 'No possible duplicates found at this threshold.'
            : 'Every flagged pair at this threshold has been reviewed.'}
        </Notice>
      ) : null}

      {!loading && sweep && shown.length > 0 && sweep.pending > shown.length ? (
        <p className="es-caption">
          Showing the {shown.length} highest-scoring of {sweep.pending.toLocaleString()} pairs.
          Raise the threshold to narrow this down.
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
