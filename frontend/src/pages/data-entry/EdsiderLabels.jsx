import { useEffect, useState } from 'react'
import { api } from '../../lib/api.js'
import { useToast } from '../../components/Toast.jsx'

// The labels EdSider needs on a paper before it can be sent there, chosen
// from EdSider's own lists (copied by the sync) and saved as EdSider's ids.
// Medium, term and year come pre-filled where the paper states them
// (src/edsider/labels.py); they are saved once data entry saves.

const byName = (a, b) => (a.name || '').trim().localeCompare((b.name || '').trim(), undefined, { sensitivity: 'base' })

const FIELDS = [
  { key: 'medium_id', list: 'language', label: 'Medium', required: true },
  { key: 'term_id', list: 'term', label: 'Term' },
  { key: 'year_id', list: 'year', label: 'Year' },
  { key: 'exam_category_id', list: 'exam_category', label: 'Exam category', required: true },
  { key: 'grading_method_id', list: 'grading_method', label: 'Grading method', required: true },
  { key: 'difficulty_id', list: 'difficulty', label: 'Difficulty' },
  { key: 'source_id', list: 'source', label: 'Source' },
  // A dropdown in alphabetical order - EdSider's own order is by when each
  // author was added, which nobody can find a name in.
  { key: 'author_id', list: 'author', label: 'Author', sort: true },
  { key: 'chapter_id', list: 'chapter', label: 'Chapter / module / unit' },
]

let listsPromise = null
export function loadLists() {
  // Once per page load - every paper uses the same lists.
  if (!listsPromise) listsPromise = api.edsiderLists().catch((err) => {
    listsPromise = null
    throw err
  })
  return listsPromise
}

function startingValues(doc) {
  const saved = doc.edsider_labels || {}
  const suggested = doc.suggested_edsider_labels || {}
  const values = {}
  const fromPaper = new Set()
  for (const f of FIELDS) {
    if (saved[f.key] != null) values[f.key] = saved[f.key]
    else if (suggested[f.key] != null) {
      values[f.key] = suggested[f.key]
      fromPaper.add(f.key)
    } else values[f.key] = null
  }
  return { values, fromPaper }
}

export default function EdsiderLabels({ doc, onSaved }) {
  const { Button, Notice } = window.EdSider
  const toast = useToast()
  const [lists, setLists] = useState(null)
  const [listError, setListError] = useState(null)
  const [{ values, fromPaper }, setState] = useState(() => startingValues(doc))
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let live = true
    loadLists()
      .then((r) => live && setLists(r.lists || {}))
      .catch((err) => live && setListError(err.message))
    return () => {
      live = false
    }
  }, [])

  const unsaved = fromPaper.size > 0
  const missing = FIELDS.filter((f) => f.required && values[f.key] == null)

  async function save(next) {
    setBusy(true)
    try {
      const updated = await api.updateIntakeDocument(doc.id, { edsider_labels: next })
      setState({ values: next, fromPaper: new Set() })
      onSaved(updated)
    } catch (err) {
      toast.error(`EdSider labels not saved: ${err.message}`)
    } finally {
      setBusy(false)
    }
  }

  function choose(key, raw) {
    const next = { ...values, [key]: raw === '' || raw == null ? null : Number(raw) }
    save(next)
  }

  if (listError) {
    return (
      <Notice tone="warning" title="EdSider's lists could not be loaded">
        {listError}. The labels below need them - an administrator can refresh them from EdSider.
      </Notice>
    )
  }
  if (!lists) return <p className="es-caption">Loading EdSider's lists...</p>
  if (!Object.keys(lists).length) {
    return (
      <Notice tone="info" title="EdSider's lists have not been copied yet">
        An administrator needs to run the sync from EdSider once before papers can be labelled for it.
      </Notice>
    )
  }

  return (
    <div style={{ display: 'grid', gap: 10 }}>
      <h3 className="scr-h3" style={{ margin: 0 }}>For EdSider</h3>
      <p className="es-caption" style={{ margin: 0 }}>
        What EdSider needs to know about this paper before it can be sent there. Grade, subject and title above are used
        too.
      </p>
      <div className="pi-paper__fields" style={{ flexWrap: 'wrap' }}>
        {FIELDS.map((f) => (
          <div className="es-field" key={f.key} style={{ minWidth: 160 }}>
            <label className="es-field__label" htmlFor={`ed-${f.key}`}>
              {f.label}
              {f.required ? ' *' : ''}
            </label>
            <select
              id={`ed-${f.key}`}
              className="es-input"
              value={values[f.key] ?? ''}
              disabled={busy}
              onChange={(e) => choose(f.key, e.target.value)}
            >
              <option value="">Not set</option>
              {(f.sort ? [...(lists[f.list] || [])].sort(byName) : lists[f.list] || []).map((item) => (
                <option key={item.id} value={item.id}>
                  {(item.name || '').trim()}
                </option>
              ))}
            </select>
            {fromPaper.has(f.key) ? <span className="es-caption">Read from the paper - not saved yet</span> : null}
          </div>
        ))}
      </div>
      <div className="es-row" style={{ gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
        {unsaved ? (
          <Button variant="secondary" size="sm" disabled={busy} onClick={() => save(values)}>
            Save what was read from the paper
          </Button>
        ) : null}
        {missing.length ? (
          <span className="es-caption">
            Still needed before sending to EdSider: {missing.map((f) => f.label).join(', ')}
          </span>
        ) : null}
      </div>
    </div>
  )
}
