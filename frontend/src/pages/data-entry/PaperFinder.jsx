import { useEffect, useState } from 'react'
import { api } from '../../lib/api.js'

// Finding one paper among hundreds - a dropdown of every paper stops working
// long before 1,000 of them. Filter by where it is in the process, its
// subject and grade, or search its file name or title; results come a page
// at a time from the server (GET /intake/documents/search).

const PAGE = 25

export const STATUS_LABEL = {
  extracted: 'Extracted',
  in_review: 'In review',
  checked: 'Duplicate check done',
  teacher_review: 'Awaiting teacher review',
  refer_back: 'Sent back for changes',
  review_done: 'Review done',
  published: 'In the question bank',
}

function gradeLabel(code) {
  const m = /^G(\d{2})$/.exec(code || '')
  return m ? `Grade ${Number(m[1])}` : code
}

function withCount(label, n) {
  return n != null ? `${label} (${n})` : label
}

export default function PaperFinder({ selectedId, onSelect, refreshKey }) {
  const { Button, Notice, StatusPill } = window.EdSider
  const [status, setStatus] = useState('')
  const [subject, setSubject] = useState('')
  const [grade, setGrade] = useState('')
  const [text, setText] = useState('')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(0)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  // Search as the person types, once they pause.
  useEffect(() => {
    const t = setTimeout(() => {
      setSearch(text)
      setPage(0)
    }, 300)
    return () => clearTimeout(t)
  }, [text])

  useEffect(() => {
    let live = true
    setLoading(true)
    api
      .searchIntakeDocuments({ status, subject, grade, q: search, limit: PAGE, offset: page * PAGE })
      .then((r) => {
        if (!live) return
        // Nothing is opened for the person: a paper opens only when they
        // pick one, so a new session starts clean.
        setResult(r)
        setError(null)
      })
      .catch((err) => live && setError(err.message))
      .finally(() => live && setLoading(false))
    return () => {
      live = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, subject, grade, search, page, refreshKey])

  const facets = result?.facets || { status: {}, subject: {}, grade: {} }
  const total = result?.total ?? 0
  const pages = Math.max(1, Math.ceil(total / PAGE))
  const filtered = Boolean(status || subject || grade || search)

  function change(setter) {
    return (e) => {
      setter(e.target.value)
      setPage(0)
    }
  }

  return (
    <div className="pi-finder" style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div className="pi-row" style={{ alignItems: 'flex-end', flexWrap: 'wrap', gap: 10 }}>
        <div className="es-field" style={{ flex: '2 1 220px' }}>
          <label className="es-field__label" htmlFor="pf-search">
            Find a paper
          </label>
          <input
            id="pf-search"
            className="es-input"
            type="search"
            placeholder="File name or paper title"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </div>
        <div className="es-field" style={{ flex: '1 1 170px' }}>
          <label className="es-field__label" htmlFor="pf-status">
            Status
          </label>
          <select id="pf-status" className="es-input" value={status} onChange={change(setStatus)}>
            <option value="">All statuses</option>
            {Object.entries(STATUS_LABEL).map(([key, label]) => (
              <option key={key} value={key}>
                {withCount(label, facets.status[key] || 0)}
              </option>
            ))}
          </select>
        </div>
        <div className="es-field" style={{ flex: '1 1 170px' }}>
          <label className="es-field__label" htmlFor="pf-subject">
            Subject
          </label>
          <select id="pf-subject" className="es-input" value={subject} onChange={change(setSubject)}>
            <option value="">All subjects</option>
            {Object.entries(facets.subject).map(([name, n]) => (
              <option key={name} value={name}>
                {withCount(name, n)}
              </option>
            ))}
          </select>
        </div>
        <div className="es-field" style={{ flex: '1 1 130px' }}>
          <label className="es-field__label" htmlFor="pf-grade">
            Grade
          </label>
          <select id="pf-grade" className="es-input" value={grade} onChange={change(setGrade)}>
            <option value="">All grades</option>
            {Object.entries(facets.grade).map(([code, n]) => (
              <option key={code} value={code}>
                {withCount(gradeLabel(code), n)}
              </option>
            ))}
          </select>
        </div>
        {filtered ? (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setStatus('')
              setSubject('')
              setGrade('')
              setText('')
              setPage(0)
            }}
          >
            Clear filters
          </Button>
        ) : null}
      </div>

      {error ? (
        <Notice tone="error" title="Could not load the papers">
          {error}
        </Notice>
      ) : null}

      <p className="es-caption" style={{ margin: 0 }}>
        {loading && !result
          ? 'Loading papers...'
          : `${total} paper${total === 1 ? '' : 's'}${filtered ? ' match' : ''}${total > PAGE ? ` - page ${page + 1} of ${pages}` : ''}`}
      </p>

      <div role="list" aria-label="Papers" style={{ maxHeight: 320, overflowY: 'auto', border: '1px solid var(--border-soft)', borderRadius: 12 }}>
        {(result?.items || []).map((d) => {
          const active = String(d.id) === String(selectedId)
          return (
            <button
              key={d.id}
              type="button"
              role="listitem"
              aria-current={active ? 'true' : undefined}
              onClick={() => onSelect(String(d.id))}
              style={{
                display: 'flex',
                width: '100%',
                gap: 12,
                alignItems: 'center',
                justifyContent: 'space-between',
                padding: '10px 14px',
                border: 'none',
                borderBottom: '1px solid var(--border-soft)',
                background: active ? 'var(--surface-sunk)' : 'transparent',
                textAlign: 'left',
                cursor: 'pointer',
                font: 'inherit',
                color: 'inherit',
              }}
            >
              <span style={{ minWidth: 0 }}>
                <span style={{ fontWeight: active ? 700 : 600, display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  #{d.id} - {d.paper_title || d.filename}
                </span>
                <span className="es-caption">
                  {[d.paper_subject, gradeLabel(d.paper_grade), d.paper_title ? d.filename : null, d.uploaded_at?.slice(0, 10)]
                    .filter(Boolean)
                    .join('  |  ')}
                </span>
              </span>
              <StatusPill status={d.status} />
            </button>
          )
        })}
        {result && !result.items.length ? (
          <p className="es-caption" style={{ padding: 14, margin: 0 }}>
            No papers match these filters.
          </p>
        ) : null}
      </div>

      {total > PAGE ? (
        <div className="es-row" style={{ gap: 8 }}>
          <Button variant="secondary" size="sm" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>
            Previous
          </Button>
          <Button variant="secondary" size="sm" disabled={page + 1 >= pages} onClick={() => setPage((p) => p + 1)}>
            Next
          </Button>
        </div>
      ) : null}
    </div>
  )
}
