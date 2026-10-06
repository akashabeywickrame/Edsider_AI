import { useState } from 'react'
import { api } from '../../lib/api.js'
import { useToast } from '../../components/Toast.jsx'

// The assessment screen's own fields, in EdSider's order. Only the ones the
// paper states start filled in (see the suggestions below); the rest are the
// data-entry person's to set.
const NOTATIONS = [
  { key: 'alpha', label: 'Alpha (A, B, C)' },
  { key: 'roman', label: 'Roman (I, II, III)' },
  { key: 'numeric', label: 'Numeric (1, 2, 3)' },
]

function fromSaved(saved, suggested) {
  const has = (k) => Object.prototype.hasOwnProperty.call(saved, k)
  const hasDuration = suggested.duration_minutes != null
  return {
    schedule_at: has('schedule_at') ? saved.schedule_at : '',
    time_limit: has('time_limit') ? saved.time_limit : hasDuration,
    duration_value: has('duration_value')
      ? saved.duration_value
      : hasDuration ? suggested.duration_minutes : '',
    duration_unit: has('duration_unit') ? saved.duration_unit : 'minutes',
    access_buffer_value: has('access_buffer_value') ? saved.access_buffer_value : '',
    access_buffer_unit: has('access_buffer_unit') ? saved.access_buffer_unit : 'minutes',
    pause_option: has('pause_option') ? saved.pause_option : false,
    restart_option: has('restart_option') ? saved.restart_option : false,
    part_notation: has('part_notation') ? saved.part_notation : 'alpha',
    multiple_papers: has('multiple_papers') ? saved.multiple_papers : false,
    master_paper: has('master_paper') ? saved.master_paper : false,
  }
}

function YesNo({ name, value, onChange, disabled }) {
  return (
    <div className="es-row">
      {[true, false].map((v) => (
        <label className="scr-radio" key={String(v)}>
          <input
            type="radio"
            name={name}
            checked={value === v}
            disabled={disabled}
            onChange={() => onChange(v)}
          />
          {v ? 'Yes' : 'No'}
        </label>
      ))}
    </div>
  )
}

export default function AssessmentSettings({ doc, onSaved }) {
  const { Button, Notice } = window.EdSider
  const toast = useToast()
  const suggested = doc.suggested_settings || {}
  const [form, setForm] = useState(() => fromSaved(doc.assessment_settings || {}, suggested))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [saved, setSaved] = useState(false)

  const set = (key, value) => {
    setSaved(false)
    setForm((f) => ({ ...f, [key]: value }))
  }

  async function save() {
    setBusy(true)
    setError(null)
    setSaved(false)
    try {
      // Merged onto what is saved, so the marks section's own data (under
      // `marking`) is never dropped by saving these settings.
      const payload = {
        ...(doc.assessment_settings || {}),
        ...form,
        duration_value: form.duration_value === '' ? null : Number(form.duration_value),
        access_buffer_value:
          form.access_buffer_value === '' ? null : Number(form.access_buffer_value),
        // Only saved as "parts" when the paper stated them - the count is
        // read from the paper, never typed by hand.
        parts: suggested.parts ?? null,
      }
      const updated = await api.updateIntakeDocument(doc.id, { assessment_settings: payload })
      setSaved(true)
      onSaved(updated)
      toast.success('Assessment settings saved.')
    } catch (err) {
      setError(err.message)
      toast.error(`Settings not saved: ${err.message}`)
    } finally {
      setBusy(false)
    }
  }

  const fromPaper = (value, suggestedValue) =>
    suggestedValue != null && Number(value) === Number(suggestedValue) ? (
      <span className="es-caption"> from the paper</span>
    ) : null

  return (
    <section className="es-card" aria-label="Assessment settings" style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
      <h2 className="scr-h2">Assessment settings</h2>
      <p className="es-caption" style={{ margin: 0 }}>
        Filled in from the paper where it states them. Everything else is set here.
      </p>

      <div className="es-field">
        <label className="es-field__label" htmlFor="as-schedule">
          Assessment date and time
        </label>
        <input
          id="as-schedule"
          className="es-input"
          type="datetime-local"
          value={form.schedule_at}
          onChange={(e) => set('schedule_at', e.target.value)}
        />
      </div>

      <div className="es-field">
        <span className="es-field__label">Do you want to set a time limit?</span>
        <YesNo name="as-time-limit" value={form.time_limit} onChange={(v) => set('time_limit', v)} />
      </div>

      {form.time_limit ? (
        <div className="pi-row">
          <div className="es-field" style={{ minWidth: 220 }}>
            <label className="es-field__label" htmlFor="as-duration">
              Time duration{fromPaper(form.duration_value, suggested.duration_minutes)}
            </label>
            <input
              id="as-duration"
              className="es-input"
              type="number"
              min="1"
              value={form.duration_value}
              onChange={(e) => set('duration_value', e.target.value)}
            />
          </div>
          <div className="es-field" style={{ minWidth: 160 }}>
            <label className="es-field__label" htmlFor="as-duration-unit">
              Unit
            </label>
            <select
              id="as-duration-unit"
              className="es-input"
              value={form.duration_unit}
              onChange={(e) => set('duration_unit', e.target.value)}
            >
              <option value="minutes">minutes</option>
              <option value="hours">hours</option>
            </select>
          </div>
        </div>
      ) : null}

      <div className="pi-row">
        <div className="es-field" style={{ minWidth: 220 }}>
          <label className="es-field__label" htmlFor="as-buffer">
            Access buffer duration
          </label>
          <input
            id="as-buffer"
            className="es-input"
            type="number"
            min="0"
            value={form.access_buffer_value}
            onChange={(e) => set('access_buffer_value', e.target.value)}
          />
        </div>
        <div className="es-field" style={{ minWidth: 160 }}>
          <label className="es-field__label" htmlFor="as-buffer-unit">
            Unit
          </label>
          <select
            id="as-buffer-unit"
            className="es-input"
            value={form.access_buffer_unit}
            onChange={(e) => set('access_buffer_unit', e.target.value)}
          >
            <option value="minutes">minutes</option>
            <option value="hours">hours</option>
          </select>
        </div>
      </div>

      <div className="es-field">
        <span className="es-field__label">Pause option</span>
        <YesNo name="as-pause" value={form.pause_option} onChange={(v) => set('pause_option', v)} />
      </div>
      <div className="es-field">
        <span className="es-field__label">Restart option</span>
        <YesNo name="as-restart" value={form.restart_option} onChange={(v) => set('restart_option', v)} />
      </div>

      <div className="es-field">
        <span className="es-field__label">
          Total marks{suggested.total_marks != null ? <span className="es-caption"> from the paper</span> : null}
        </span>
        <input
          className="es-input"
          value={suggested.total_marks ?? 'Not stated on the paper'}
          readOnly
          style={{ maxWidth: 280 }}
        />
        {(suggested.parts_marks || []).map((p) => (
          <p key={p.label ?? 'paper'} className="es-caption" style={{ margin: '4px 0 0' }}>
            {p.label || 'Paper'}: {p.answer_count ?? '?'} question(s) to answer
            {p.marks_each != null ? ` x ${p.marks_each} mark(s) each` : ''}
            {p.total != null ? ` = ${p.total}` : ' - marks not stated'}
            {p.assumed_marks ? ' (one mark each assumed - not stated on the paper)' : ''}
          </p>
        ))}
      </div>

      <div className="es-field">
        <span className="es-field__label">
          No of parts{suggested.parts != null ? <span className="es-caption"> from the paper</span> : null}
        </span>
        <input
          className="es-input"
          value={suggested.parts ?? 'Not found on the paper'}
          readOnly
          style={{ maxWidth: 280 }}
        />
      </div>

      <div className="es-field">
        <label className="es-field__label" htmlFor="as-notation">
          Part notation
        </label>
        <select
          id="as-notation"
          className="es-input"
          value={form.part_notation}
          onChange={(e) => set('part_notation', e.target.value)}
          style={{ maxWidth: 280 }}
        >
          {NOTATIONS.map((n) => (
            <option key={n.key} value={n.key}>
              {n.label}
            </option>
          ))}
        </select>
      </div>

      <div className="es-field">
        <span className="es-field__label">Does this have multiple papers?</span>
        <YesNo name="as-multi" value={form.multiple_papers} onChange={(v) => set('multiple_papers', v)} />
      </div>
      <div className="es-field">
        <span className="es-field__label">Is this the master paper?</span>
        <YesNo name="as-master" value={form.master_paper} onChange={(v) => set('master_paper', v)} />
      </div>

      {error ? (
        <Notice tone="error" title="Could not save the assessment settings">
          {error}
        </Notice>
      ) : null}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <Button variant="primary" onClick={save} disabled={busy}>
          {busy ? 'Saving...' : 'Save assessment settings'}
        </Button>
        {saved ? <span className="es-caption">Saved.</span> : null}
      </div>
    </section>
  )
}
