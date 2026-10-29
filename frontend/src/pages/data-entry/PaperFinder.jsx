import { useState } from 'react'
import { api } from '../../lib/api.js'
import StatusPill from '../../components/StatusPill.jsx'
import PaperFilters, { PaperCount, Pager, nameOn, usePaperSearch } from '../../components/PaperFilters.jsx'
import { confirmDialog } from '../../components/ConfirmDialog.jsx'

// Finding one paper among hundreds - a dropdown of every paper stops working
// long before 1,000 of them. The filters are EdSider's assessment library's
// (components/PaperFilters.jsx); results come a page at a time.

function gradeLabel(code) {
  const m = /^G(\d{2})$/.exec(code || '')
  return m ? `Grade ${Number(m[1])}` : code
}

export default function PaperFinder({ selectedId, onSelect, refreshKey, onRemoved }) {
  const { Button, Notice } = window.EdSider
  // Nothing is opened for the person: a paper opens only when they pick
  // one, so a new session starts clean.
  const search = usePaperSearch({ refreshKey })
  const { result, lists } = search
  const [removing, setRemoving] = useState(null)
  const [removeError, setRemoveError] = useState(null)

  // An upload made by mistake goes while it is still Extracted - after
  // that it has moved on (the API refuses it too).
  async function remove(d) {
    if (!(await confirmDialog('Delete this paper permanently?', { danger: true }))) return
    setRemoving(d.id)
    setRemoveError(null)
    try {
      await api.deleteIntakeDocument(d.id)
      search.reload()
      if (onRemoved) onRemoved(d)
    } catch (err) {
      setRemoveError(`Not removed: ${err.message}`)
    } finally {
      setRemoving(null)
    }
  }

  const error = removeError || search.error
  return (
    <div className="pi-finder" style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <PaperFilters search={search} />

      {error ? (
        <Notice tone="error" title="Could not load the papers">
          {error}
        </Notice>
      ) : null}

      <PaperCount search={search} />

      <div role="list" aria-label="Papers" style={{ maxHeight: 320, overflowY: 'auto', border: '1px solid var(--border-soft)', borderRadius: 12 }}>
        {(result?.items || []).map((d) => {
          const active = String(d.id) === String(selectedId)
          return (
            <div key={d.id} role="listitem" style={{ display: 'flex', alignItems: 'center',
              borderBottom: '1px solid var(--border-soft)', background: active ? 'var(--surface-sunk)' : 'transparent' }}>
            <button
              type="button"
              aria-current={active ? 'true' : undefined}
              onClick={() => onSelect(String(d.id))}
              style={{
                display: 'flex',
                flex: 1,
                minWidth: 0,
                gap: 12,
                alignItems: 'center',
                justifyContent: 'space-between',
                padding: '10px 14px',
                border: 'none',
                background: 'transparent',
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
                  {[d.paper_subject, gradeLabel(d.paper_grade), nameOn(lists, 'language', d.edsider_labels?.medium_id), d.paper_title ? d.filename : null, d.uploaded_at?.slice(0, 10)]
                    .filter(Boolean)
                    .join('  |  ')}
                </span>
              </span>
              <StatusPill status={d.status} />
            </button>
            {d.status === 'extracted' && !d.archived_at ? (
              <Button variant="ghost" size="sm" onClick={() => remove(d)} disabled={removing !== null}
                aria-label={`Remove paper ${d.id}`} style={{ marginRight: 10 }}>
                {removing === d.id ? 'Removing...' : 'Remove'}
              </Button>
            ) : null}
            </div>
          )
        })}
        {result && !result.items.length ? (
          <p className="es-caption" style={{ padding: 14, margin: 0 }}>
            No papers match these filters.
          </p>
        ) : null}
      </div>

      <Pager search={search} />
    </div>
  )
}
