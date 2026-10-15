import { useState } from 'react'

// Instructions that cover a range of questions - "Answer questions 20 to 25
// using the chart below" - shown above the first of them. The extractor
// finds many on its own (see src/intake/extract.py, _split_group_instruction);
// data entry adds the rest, or a part's instructions when the extractor did
// not recognise the part's heading ("Part 2", as a title). Stored on the
// paper as one list, so every change here sends the whole list back.

export function sameNumber(a, b) {
  const x = parseInt(a, 10)
  const y = parseInt(b, 10)
  return Number.isNaN(x) || Number.isNaN(y) ? String(a) === String(b) : x === y
}

// The groups that start at this question.
export function groupsStartingAt(groups, section, number) {
  return (groups || []).filter((g) => (g.section ?? null) === (section ?? null) && sameNumber(g.from, number))
}

function GroupForm({ initial, numbersFor, partLabels, onSave, onCancel, busy }) {
  const { Button } = window.EdSider
  const [g, setG] = useState(initial)
  const numbers = numbersFor(g.section)
  const valid = g.from && g.to && parseInt(g.to, 10) >= parseInt(g.from, 10) && (g.text || '').trim()
  return (
    <div className="pi-panel" style={{ display: 'grid', gap: 10 }}>
      {partLabels.length > 0 ? (
        <label className="es-field">
          <span className="es-field__label">Part</span>
          <select className="es-input" value={g.section ?? ''}
            onChange={(e) => setG({ ...g, section: e.target.value || null, from: '', to: '' })}>
            {partLabels.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </label>
      ) : null}
      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
        <label className="es-field">
          <span className="es-field__label">From question</span>
          <select className="es-input" value={g.from} onChange={(e) => setG({ ...g, from: e.target.value })}>
            <option value="">Choose</option>
            {numbers.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
        <label className="es-field">
          <span className="es-field__label">To question</span>
          <select className="es-input" value={g.to} onChange={(e) => setG({ ...g, to: e.target.value })}>
            <option value="">Choose</option>
            {numbers.filter((n) => !g.from || parseInt(n, 10) >= parseInt(g.from, 10)).map((n) => (
              <option key={n} value={n}>{n}</option>
            ))}
          </select>
        </label>
        <label className="es-field" style={{ flex: 1, minWidth: 180 }}>
          <span className="es-field__label">Title (optional)</span>
          <input className="es-input" value={g.title || ''} placeholder='e.g. "Part 2"'
            onChange={(e) => setG({ ...g, title: e.target.value })} />
        </label>
      </div>
      <label className="es-field">
        <span className="es-field__label">Instructions</span>
        <textarea className="es-input" rows={3} value={g.text || ''}
          placeholder="e.g. Choose the correct answer by reading the passage below for questions 20 to 25."
          onChange={(e) => setG({ ...g, text: e.target.value })} />
      </label>
      <div style={{ display: 'flex', gap: 8 }}>
        <Button variant="primary" size="sm" disabled={!valid || busy}
          onClick={() => onSave({ ...g, title: (g.title || '').trim() || null, text: g.text.trim() })}>
          {busy ? 'Saving...' : 'Save'}
        </Button>
        <Button variant="ghost" size="sm" onClick={onCancel} disabled={busy}>Cancel</Button>
      </div>
    </div>
  )
}

// One group's banner, above its first question - read, edit or remove.
export function GroupInstructionBanner({ group, index, groups, numbersFor, partLabels, onSave, busy }) {
  const { Button } = window.EdSider
  const [editing, setEditing] = useState(false)
  const replace = (next) => groups.map((g, i) => (i === index ? next : g))
  if (editing) {
    return (
      <GroupForm initial={group} numbersFor={numbersFor} partLabels={partLabels} busy={busy}
        onCancel={() => setEditing(false)}
        onSave={async (next) => {
          await onSave(replace(next))
          setEditing(false)
        }} />
    )
  }
  return (
    <div className="pi-panel" role="note" aria-label={`Instructions for questions ${group.from} to ${group.to}`}
      style={{ borderLeft: '4px solid var(--primary)', display: 'grid', gap: 6 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <strong>{group.title ? `${group.title} - ` : ''}Questions {group.from} to {group.to}</strong>
        <span style={{ flex: 1 }} />
        <Button variant="ghost" size="sm" onClick={() => setEditing(true)} disabled={busy}>Edit</Button>
        <Button variant="ghost" size="sm" disabled={busy}
          onClick={() => onSave(groups.filter((_, i) => i !== index))}>
          Remove
        </Button>
      </div>
      <p style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{group.text}</p>
    </div>
  )
}

// The "add one" button and its form.
export function AddGroupInstruction({ groups, numbersFor, partLabels, onSave, busy }) {
  const { Button } = window.EdSider
  const [open, setOpen] = useState(false)
  if (!open) {
    return (
      <Button variant="secondary" size="sm" onClick={() => setOpen(true)}>
        Add instructions for a group of questions
      </Button>
    )
  }
  return (
    <GroupForm
      initial={{ section: partLabels[0] ?? null, from: '', to: '', title: '', text: '' }}
      numbersFor={numbersFor} partLabels={partLabels} busy={busy}
      onCancel={() => setOpen(false)}
      onSave={async (g) => {
        await onSave([...(groups || []), g])
        setOpen(false)
      }} />
  )
}
