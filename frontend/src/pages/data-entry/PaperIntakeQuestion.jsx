import { useEffect, useState } from 'react'
import { api } from '../../lib/api.js'
import { AuthImg } from '../../lib/useFileUrl.jsx'
import RichEditor from '../../components/RichEditor.jsx'
import { useToast } from '../../components/Toast.jsx'

function slotLabel(slot) {
  if (slot === 'question') return 'Question'
  if (slot === 'answer') return 'Answer'
  return `Option ${slot.replace('option:', '')}`
}

// Every label an answer names - "1, 3" -> ["1", "3"], "A/C" -> ["a", "c"] -
// each read the same way bareLabel() reads one.
function answerLabels(answer) {
  if (!answer) return []
  return answer.split(/\s*(?:,|\/|&|;|\band\b)\s*/i).map(bareLabel).filter(Boolean)
}

// "(3)", " 3 " -> "3" - the same bare-label shape mcq_correct_label()
// normalizes server-side, done here client-side because a picture-only
// MCQ has no McqInfoOut at all (split_mcq_options found no text to
// split - see service.py's _question_out), so there is no server-
// computed `correct` to compare against for this path.
function bareLabel(answer) {
  if (!answer) return null
  const m = /^\(?\s*([A-Za-z0-9]{1,4})\s*\)?$/.exec(answer.trim())
  return m ? m[1].toLowerCase() : null
}

// Same wording the server's QUESTION_TYPES uses - kept in step by hand, since
// the page only needs labels, not the detection rule.
const TYPE_LABELS = {
  descriptive: 'Descriptive',
  mcq: 'MCQ',
  fill_blank: 'Fill in the blank',
  sub: 'Sub-questions',
}

const optionsFrom = (mcq) =>
  (mcq?.options || []).map((o) => ({ label: o.label, marker: o.marker, text: o.text }))

// The next printed marker in the same style as the last one - "III." after
// "II.", "(4)" after "(3)", "D)" after "C)". Falls back to "A." for a list
// with nothing to copy the style from.
function toRoman(n) {
  const table = [[10, 'X'], [9, 'IX'], [5, 'V'], [4, 'IV'], [1, 'I']]
  let out = ''
  for (const [value, digits] of table) {
    while (n >= value) {
      out += digits
      n -= value
    }
  }
  return out
}

function fromRoman(s) {
  const value = { I: 1, V: 5, X: 10, L: 50, C: 100 }
  let total = 0
  for (let i = 0; i < s.length; i++) {
    const cur = value[s[i]]
    const next = value[s[i + 1]]
    total += next && next > cur ? -cur : cur
  }
  return total
}

// "(3)" -> { open: "(", label: "3", close: ")" }, or null for anything else.
function splitMarker(marker) {
  const m = /^(\(?)([A-Za-z0-9]+)([.)]?\)?)$/.exec((marker || '').trim())
  return m ? { open: m[1], label: m[2], close: m[3] } : null
}

// The label after `lab` in the same style - "II" -> "III", "c" -> "d", "9" -> "10".
// Roman is tried before letters, so "I" after "H" is never misread as a letter.
function stepLabel(lab) {
  if (/^\d+$/.test(lab)) return String(Number(lab) + 1)
  if (/^[IVX]+$/.test(lab)) return toRoman(fromRoman(lab) + 1)
  if (/^[ivx]+$/.test(lab)) return toRoman(fromRoman(lab.toUpperCase()) + 1).toLowerCase()
  if (/^[A-Z]$/.test(lab) && lab !== 'Z') return String.fromCharCode(lab.charCodeAt(0) + 1)
  if (/^[a-z]$/.test(lab) && lab !== 'z') return String.fromCharCode(lab.charCodeAt(0) + 1)
  return null
}

function nextMarker(options) {
  const last = options[options.length - 1]
  const parts = last && splitMarker(last.marker)
  const next = parts && stepLabel(parts.label)
  return next ? `${parts.open}${next}${parts.close}` : 'A.'
}

// The numbering style this paper's own MCQs use - "I.", "1.", "A)" and so
// on - taken from the most common first-option marker across the paper, so
// options typed in for one question match its neighbours.
export function paperOptionStyle(questions) {
  const tally = new Map()
  for (const q of questions || []) {
    const parts = q.mcq?.options?.[0] && splitMarker(q.mcq.options[0].marker)
    if (!parts) continue
    const lab = parts.label
    const kind = /^\d+$/.test(lab) ? 'digit'
      : /^[IVX]+$/.test(lab) ? 'roman'
      : /^[ivx]+$/.test(lab) ? 'lroman'
      : /^[A-Z]$/.test(lab) ? 'letter'
      : /^[a-z]$/.test(lab) ? 'lletter'
      : null
    if (!kind) continue
    const key = `${kind}|${parts.open}|${parts.close}`
    tally.set(key, (tally.get(key) || 0) + 1)
  }
  let best = null
  let count = 0
  for (const [key, n] of tally) {
    if (n > count) {
      best = key
      count = n
    }
  }
  if (!best) return null
  const [kind, open, close] = best.split('|')
  return { kind, open, close }
}

// Starter options in the given style - A. to D. when the paper gives no
// style to copy.
export function starterOptions(style, count = 4) {
  const kind = style?.kind ?? 'letter'
  const open = style?.open ?? ''
  const close = style?.close ?? '.'
  const labelAt = (i) => {
    if (kind === 'digit') return String(i + 1)
    if (kind === 'roman') return toRoman(i + 1)
    if (kind === 'lroman') return toRoman(i + 1).toLowerCase()
    if (kind === 'lletter') return String.fromCharCode(97 + i)
    return String.fromCharCode(65 + i)
  }
  return Array.from({ length: count }, (_, i) => ({
    label: null,
    marker: `${open}${labelAt(i)}${close}`,
    text: '',
  }))
}

// Renumbers options one after another from `startLabel`, keeping each
// option's own punctuation. The parser only reads a list that counts up
// without a gap, so a removed option must not leave "I, II, IV, V" behind.
function renumber(options, startLabel) {
  let lab = startLabel
  return options.map((o) => {
    const parts = splitMarker(o.marker)
    const next = { ...o, label: lab, marker: parts ? `${parts.open}${lab}${parts.close}` : o.marker }
    lab = stepLabel(lab) ?? lab
    return next
  })
}

// One draft question (or part) on an uploaded paper. Marks and topic
// save on blur; text and answer (RichEditor) save on blur too, but each
// carries its own current value straight from the editor rather than
// through local state - see RichEditor's own note on why. Nothing on a
// 100+ question paper is ever left unsaved by a missed click. Images
// attach/detach immediately, same reasoning as Remove: one-shot actions,
// not something to leave pending.
export default function PaperIntakeQuestion({
  q,
  isStem,
  images,
  optionStyle,
  comments,
  onChanged,
  onRemoved,
  onImagesChanged,
}) {
  const { Button, Notice } = window.EdSider

  const [marks, setMarks] = useState(q.marks ?? '')
  // Marks can also change from outside this card - "Apply to all questions"
  // and the per-question boxes in the Marks section above. The box only read
  // q.marks once, so after setting marks there every card still showed the
  // old figure; follow the question whenever its saved marks change.
  const [savedMarks, setSavedMarks] = useState(q.marks)
  if (q.marks !== savedMarks) {
    setSavedMarks(q.marks)
    setMarks(q.marks ?? '')
  }
  const [topic, setTopic] = useState(q.topic || '')
  const [reviewed, setReviewed] = useState(!!q.reviewed)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  // Open straight away when this question already has an answer picture, so
  // it is not hidden behind a closed panel.
  const [answerOpen, setAnswerOpen] = useState(() => (images || []).some((i) => i.slot === 'answer'))
  const [imagesOpen, setImagesOpen] = useState(false)
  const [slot, setSlot] = useState('question')
  const [file, setFile] = useState(null)
  const [imgBusy, setImgBusy] = useState(false)
  const [imgError, setImgError] = useState(null)

  // MCQ options this question's own text contains, if any, ride along
  // on `q` itself (service.py computes it inline per row) - fetching it
  // as its own request per card was measured firing 100+ concurrent GETs
  // on a real paper that queued behind each other for over 20 seconds,
  // which then starved an unrelated save sent while they were still in
  // flight. `manualOptionCount` is the one piece with no server data to
  // read: a picture-only MCQ split_mcq_options() found nothing to split.
  // Seeded from an existing bare-number answer ("4") when there is one,
  // so a question someone already marked correct by typing a number
  // does not silently lose that under a count of zero.
  const [manualOptionCount, setManualOptionCount] = useState(() => {
    // The highest number named, so a question with several correct
    // numbered options still seeds the count they imply.
    const named = answerLabels(q.answer).map(Number).filter((x) => Number.isInteger(x) && x > 0 && x <= 8)
    return named.length ? Math.max(...named) : 0
  })
  const [correctBusy, setCorrectBusy] = useState(false)
  const toast = useToast()

  // Mirrors the stem editor's live content (RichEditor's onChange) and
  // each option's current text - an option field's own blur needs the
  // CURRENT stem to reassemble the combined text, not just its own
  // value, so both have to be tracked outside RichEditor's own
  // mount-once state. Seeded once, from the row this card mounted with -
  // same reasoning as RichEditor's own `initialValue`, not reactive to
  // `q` changing later (a save this component made itself already has
  // the latest value locally; a save mounting a NEW q.id remounts this
  // whole component instead, via PaperIntake.jsx's key={q.id}).
  // The option list is editable here - options can be removed, added, or
  // typed in from scratch - so it is kept as its own array, and resynced
  // from the server whenever a save hands back a new `q.mcq`.
  const [stemText, setStemText] = useState(() => q.mcq?.stem ?? q.text ?? '')
  const [draftOptions, setDraftOptions] = useState(() => optionsFrom(q.mcq))
  useEffect(() => {
    setDraftOptions(optionsFrom(q.mcq))
  }, [q.mcq])
  // Bumped by "Clear and re-enter" so the uncontrolled editors remount with
  // their new empty content - RichEditor reads its initial value only once.
  const [resetKey, setResetKey] = useState(0)
  const [mcqSaveError, setMcqSaveError] = useState(null)
  const [mcqBusy, setMcqBusy] = useState(false)

  async function save(fields) {
    setBusy(true)
    setError(null)
    try {
      const updated = await api.updateIntakeQuestion(q.id, fields)
      onChanged(updated)
    } catch (err) {
      setError(err.message)
      if ('reviewed' in fields) setReviewed(!!q.reviewed)
    } finally {
      setBusy(false)
    }
  }

  function saveIfChanged(field, value, original) {
    if (value === (original ?? '')) return
    save({ [field]: field === 'marks' ? (value === '' ? null : Number(value)) : value })
  }

  // Reassembles the stem and every option (each led by its own real
  // printed marker - "(1)", "I.", whatever this paper used) back into
  // the question's one combined text field - the server does the actual
  // join (see /intake/questions/{id}/mcq), so correcting an option's
  // text never has to reproduce the paper's own numbering by hand.
  // `answer` (when given) replaces the stored answer in the same save - a
  // removed option's answer is cleared, and a renumbered one moves to its
  // option's new label, so the answer never names an option that is gone.
  async function saveOptions(nextStem, nextOptions, { answer } = {}) {
    setMcqBusy(true)
    setMcqSaveError(null)
    try {
      let updated = await api.updateQuestionMcq(q.id, {
        stem: nextStem,
        options: nextOptions.map((o) => ({ marker: o.marker, text: o.text })),
      })
      if (answer !== undefined) updated = await api.updateIntakeQuestion(q.id, { answer })
      onChanged(updated)
    } catch (err) {
      setMcqSaveError(err.message)
    } finally {
      setMcqBusy(false)
    }
  }

  function addOption() {
    const next = [...draftOptions, { label: null, marker: nextMarker(draftOptions), text: '' }]
    setDraftOptions(next)
    saveOptions(stemText, next)
  }

  function removeOption(index) {
    const kept = draftOptions.filter((_, i) => i !== index)
    // Renumber from the list's original first label, so removing the first
    // option leaves the next one as the first, not as a second "II".
    const start = draftOptions[0].label || splitMarker(draftOptions[0].marker)?.label
    const renumbered = renumber(kept, start)
    // A question can have several correct options - keep every one that
    // survives the removal, under its renumbered label.
    const correctNow = answerLabels(q.answer)
    const keptCorrect = renumbered
      .filter((o, j) => kept[j].label && correctNow.includes(kept[j].label.toLowerCase()))
      .map((o) => o.label)
    const removedKey = draftOptions[index].label?.toLowerCase()
    let answer
    if (keptCorrect.length) answer = keptCorrect.join(', ')
    else if (removedKey && correctNow.includes(removedKey)) answer = null
    setDraftOptions(renumbered)
    saveOptions(stemText, renumbered, answer === undefined ? {} : { answer })
  }

  // For a question whose options were not read at all, or were read as
  // nothing usable: starts a lettered list the reviewer then fills in.
  function addOptionsAsText() {
    const starter = starterOptions(optionStyle)
    setDraftOptions(starter)
    saveOptions(stemText, starter)
  }

  // Clears the question's text, options and answer, so a question with too
  // many wrong pieces can be typed in again rather than repaired piece by
  // piece. The question keeps its number, place and marks.
  async function handleRecreate() {
    if (!window.confirm("Clear this question's text, options and answer so it can be typed in again?")) {
      return
    }
    setBusy(true)
    setError(null)
    try {
      const updated = await api.updateIntakeQuestion(q.id, { text: '', answer: null, reviewed: false })
      setStemText('')
      setDraftOptions([])
      setManualOptionCount(0)
      setReviewed(false)
      setResetKey((k) => k + 1)
      onChanged(updated)
      toast.success('Question cleared - type it in again.')
    } catch (err) {
      setError(err.message)
      toast.error(`Could not clear the question: ${err.message}`)
    } finally {
      setBusy(false)
    }
  }

  // Marks one option correct, or unmarks it - a question can have several
  // correct options, so each click changes only its own option. The stored
  // answer lists them in the paper's printed order.
  async function handleMarkCorrect(label, order) {
    const key = String(label).toLowerCase()
    const current = new Set(answerLabels(q.answer))
    if (current.has(key)) current.delete(key)
    else current.add(key)
    const chosen = order.filter((l) => current.has(String(l).toLowerCase()))
    setCorrectBusy(true)
    setError(null)
    try {
      const updated = await api.updateIntakeQuestion(q.id, { answer: chosen.length ? chosen.join(', ') : null })
      onChanged(updated)
    } catch (err) {
      setError(err.message)
    } finally {
      setCorrectBusy(false)
    }
  }

  async function handleRemove() {
    setBusy(true)
    setError(null)
    try {
      await api.deleteIntakeQuestion(q.id)
      onRemoved(q.id)
      toast.success('Question removed.')
    } catch (err) {
      setError(err.message)
      toast.error(`Could not remove the question: ${err.message}`)
      setBusy(false)
    }
  }

  async function handleAttach(e) {
    e.preventDefault()
    if (!file) return
    setImgBusy(true)
    setImgError(null)
    try {
      const img = await api.attachQuestionImage(q.id, slot, file)
      onImagesChanged(q.id, [...(images || []), img])
      setFile(null)
      e.target.reset()
    } catch (err) {
      setImgError(err.message)
    } finally {
      setImgBusy(false)
    }
  }

  async function handleRemoveImage(imageId) {
    setImgBusy(true)
    setImgError(null)
    try {
      await api.deleteImage(imageId)
      onImagesChanged(q.id, (images || []).filter((i) => i.id !== imageId))
    } catch (err) {
      setImgError(err.message)
    } finally {
      setImgBusy(false)
    }
  }

  const label = q.question_number ? `Question ${q.question_number}` : `Question ${q.seq}`
  const imgCount = (images || []).length
  const answerImages = (images || []).filter((i) => i.slot === 'answer')
  const detectedOptions = q.mcq?.options || []
  // Every option a student could see as correct, read from the stored answer.
  const correctSet = new Set(answerLabels(q.answer))
  // Picture-only MCQs have no text options - numbered by the manual count.
  const manualLabels = Array.from({ length: manualOptionCount }, (_, i) => String(i + 1))
  // A picture-only MCQ - the options are photos next to a bare roman
  // numeral with nothing split_mcq_options() can key on - still needs
  // somewhere to attach an image per option, so a manual count stands
  // in for the options the extractor could not find in the text. Same
  // fallback app.py's own Paper intake page uses.
  const optionSlots =
    detectedOptions.length > 0
      ? detectedOptions.map((o) => `option:${o.label}`)
      : Array.from({ length: manualOptionCount }, (_, i) => `option:${i + 1}`)
  const slotChoices = ['question', 'answer', ...optionSlots]

  return (
    <article
      className={`es-card es-draft${isStem ? ' es-draft--stem' : ''}${q.depth > 0 ? ' pi-part' : ''}`}
      aria-label={label}
    >
      <div className="es-draft__head">
        <span className="pi-label">{label}</span>
        {q.extraction_confidence === 'low' ? (
          <span className="es-pill es-tone--review">Low-confidence read</span>
        ) : null}
        <span style={{ flex: 1 }} />
        <label className="pi-type">
          <span className="es-caption">Type</span>
          <select
            className="es-input pi-type__select"
            aria-label={`${label} type`}
            value={q.question_type || q.detected_type || 'descriptive'}
            disabled={busy}
            onChange={(e) => save({ question_type: e.target.value })}
          >
            {Object.entries(TYPE_LABELS).map(([key, text]) => (
              <option key={key} value={key}>
                {text}
              </option>
            ))}
          </select>
          {q.question_type !== q.detected_type && q.detected_type ? (
            <span className="es-caption">detected as {TYPE_LABELS[q.detected_type]}</span>
          ) : null}
        </label>
        <span className={`es-pill ${reviewed ? 'es-tone--ok' : 'es-tone--neutral'}`}>
          {reviewed ? 'Reviewed' : 'Not reviewed'}
        </span>
      </div>

      {q.last_edited_by ? (
        <p className="es-caption" style={{ margin: 0 }}>
          Last edited by {q.last_edited_by}
          {q.last_edited_at ? ` - ${new Date(q.last_edited_at).toLocaleString()}` : ''}
        </p>
      ) : null}

      {isStem ? (
        <p className="es-caption" style={{ margin: 0 }}>
          Shared text for the parts below - the scenario or stimulus they all refer to. Each
          part is answered separately.
        </p>
      ) : null}

      {(comments || []).length > 0 ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {comments.map((c) => (
            <Notice
              key={c.id}
              tone="warning"
              title={`${c.author || 'Teacher'} - ${c.created_at ? new Date(c.created_at).toLocaleString() : ''}`}
            >
              {c.comment}
            </Notice>
          ))}
        </div>
      ) : null}

      {draftOptions.length > 0 ? (
        <RichEditor
          key={`stem-opt-${resetKey}`}
          initialValue={stemText}
          onChange={setStemText}
          onBlur={(plain) => saveOptions(plain, draftOptions)}
          ariaLabel={`${label} text`}
        />
      ) : (
        <RichEditor
          key={`stem-txt-${resetKey}`}
          initialValue={q.text || ''}
          onChange={setStemText}
          onBlur={(plain) => saveIfChanged('text', plain, q.text || '')}
          ariaLabel={`${label} text`}
        />
      )}

      {draftOptions.length > 0 ? (
        <div className="es-field">
          <label className="es-field__label">
            Answer options - correct any that were read wrong, and click the right one if it
            is not already marked
          </label>
          <div className="es-mcq pi-mcq">
            {draftOptions.map((o, i) => {
              const key = o.label || o.marker.trim()
              const isCorrect = !!o.label && correctSet.has(o.label.toLowerCase())
              return (
                <div
                  key={i}
                  className={`es-mcq__opt pi-mcq__opt${isCorrect ? ' es-mcq__opt--correct' : ''}`}
                >
                  <span className="es-mcq__key">{key}</span>
                  <input
                    className="pi-mcq__input"
                    value={o.text}
                    aria-label={`${label} option ${key} text`}
                    disabled={mcqBusy}
                    onChange={(e) =>
                      setDraftOptions((prev) =>
                        prev.map((p, j) => (j === i ? { ...p, text: e.target.value } : p)),
                      )
                    }
                    onBlur={() => saveOptions(stemText, draftOptions)}
                  />
                  {isCorrect ? <span className="es-mcq__correct">Correct</span> : null}
                  {o.label ? (
                    <button
                      type="button"
                      className="pi-mcq__mark"
                      onClick={() => handleMarkCorrect(o.label, detectedOptions.map((x) => x.label))}
                      disabled={correctBusy || mcqBusy}
                    >
                      {isCorrect ? 'Unmark' : 'Mark correct'}
                    </button>
                  ) : null}
                  <button
                    type="button"
                    className="pi-mcq__mark"
                    onClick={() => removeOption(i)}
                    disabled={mcqBusy}
                  >
                    Remove
                  </button>
                </div>
              )
            })}
          </div>
          <div style={{ marginTop: 8 }}>
            <Button variant="secondary" size="sm" onClick={addOption} disabled={mcqBusy}>
              Add option
            </Button>
          </div>
          <p className="es-caption" style={{ marginTop: 6 }}>
            {correctSet.size
              ? 'Click Mark correct on every option that is right - a question can have more than one. Unmark takes one back.'
              : 'No correct answer found in the paper yet - click "Mark correct" on each right option.'}
          </p>
          {mcqSaveError ? (
            <Notice tone="error" title="Could not save the options">
              {mcqSaveError}
            </Notice>
          ) : null}
        </div>
      ) : !(q.blanks || []).length && !isStem ? (
        <div className="es-field">
          <label className="es-field__label">
            Options not split out automatically - common when every option is a picture rather
            than text, or when a question marked MCQ above was not read as one. How many does
            this question have?
          </label>
          <select
            className="es-input pi-mcq-count"
            value={manualOptionCount}
            onChange={(e) => setManualOptionCount(Number(e.target.value))}
          >
            <option value={0}>Not an MCQ, or not known yet</option>
            {[1, 2, 3, 4, 5, 6, 7, 8].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
          <div style={{ marginTop: 10 }}>
            <Button variant="secondary" size="sm" onClick={addOptionsAsText} disabled={mcqBusy}>
              Type the options in instead
            </Button>
          </div>
          {manualOptionCount > 0 ? (
            <>
              <p className="es-caption" style={{ marginTop: 10 }}>
                Attach each option's picture below under Images, then mark the correct one here.
              </p>
              <div className="es-mcq pi-mcq">
                {manualLabels.map((n) => {
                  const isCorrect = correctSet.has(n)
                  return (
                    <div
                      key={n}
                      className={`es-mcq__opt pi-mcq__opt pi-mcq__opt--manual${isCorrect ? ' es-mcq__opt--correct' : ''}`}
                    >
                      <span className="es-mcq__key">{n}</span>
                      <span>Option {n}</span>
                      {isCorrect ? <span className="es-mcq__correct">Correct</span> : null}
                      <button
                        type="button"
                        className="pi-mcq__mark"
                        onClick={() => handleMarkCorrect(n, manualLabels)}
                        disabled={correctBusy}
                      >
                        {isCorrect ? 'Unmark' : 'Mark correct'}
                      </button>
                    </div>
                  )
                })}
              </div>
            </>
          ) : null}
        </div>
      ) : null}

      <div className="es-field">
        <label className="es-field__label">Topic / subtopic (if shown on the paper)</label>
        <input
          className="es-input"
          type="text"
          value={topic}
          placeholder="e.g. Chemical and cellular basis of life - Organization of cells"
          onChange={(e) => setTopic(e.target.value)}
          onBlur={() => saveIfChanged('topic', topic, q.topic || '')}
        />
      </div>

      <div className="pi-row">
        <div className="pi-marks">
          <div className="es-field">
            <label className="es-field__label">Marks</label>
            <input
              className="es-input"
              type="number"
              min="0"
              value={marks}
              onChange={(e) => setMarks(e.target.value)}
              onBlur={() => saveIfChanged('marks', marks, q.marks ?? '')}
            />
          </div>
        </div>
        <label className="es-check" style={{ marginTop: 26 }}>
          <input
            type="checkbox"
            checked={reviewed}
            disabled={busy}
            onChange={(e) => {
              setReviewed(e.target.checked)
              save({ reviewed: e.target.checked })
            }}
          />
          Reviewed
        </label>
      </div>

      {error ? (
        <Notice tone="error" title="Could not save">
          {error}
        </Notice>
      ) : null}

      <button
        className="pi-exp"
        type="button"
        aria-expanded={answerOpen}
        onClick={() => setAnswerOpen((v) => !v)}
      >
        {q.answer ? 'Answer' : 'Answer (none found in the paper)'}
        <span className="pi-exp__chev">{answerOpen ? 'Hide' : 'Show'}</span>
      </button>
      {answerOpen ? (
        <div className="pi-panel" style={{ marginTop: -16 }}>
          <RichEditor
            key={`answer-${resetKey}`}
            initialValue={q.answer || ''}
            onBlur={(plain) => saveIfChanged('answer', plain, q.answer || '')}
            ariaLabel={`${label} answer`}
          />
          {/* A table or a picture of the answer - shown here too, so the
              reviewer sees exactly what the student's marking scheme will hold. */}
          {answerImages.length > 0 ? (
            <div className="pi-imgs">
              {answerImages.map((im) => (
                <figure className="pi-img" key={im.id}>
                  <figcaption className="pi-img__slot">Answer picture</figcaption>
                  <div className="pi-img__pic">
                    <AuthImg path={api.imagePath(im.id)} alt={`Answer picture for ${label}`} />
                  </div>
                  <Button variant="ghost" size="sm" onClick={() => handleRemoveImage(im.id)} disabled={imgBusy}>
                    Remove picture
                  </Button>
                </figure>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}

      <button
        className="pi-exp"
        type="button"
        aria-expanded={imagesOpen}
        onClick={() => setImagesOpen((v) => !v)}
      >
        {`Images (${imgCount})`}
        <span className="pi-exp__chev">{imagesOpen ? 'Hide' : 'Show'}</span>
      </button>
      {imagesOpen ? (
        <div className="pi-panel" style={{ marginTop: -16 }}>
          {imgCount > 0 ? (
            <div className="pi-imgs">
              {images.map((im) => (
                <figure className="pi-img" key={im.id}>
                  <figcaption className="pi-img__slot">{slotLabel(im.slot)}</figcaption>
                  <div className="pi-img__pic">
                    <AuthImg path={api.imagePath(im.id)} alt={`${slotLabel(im.slot)} for ${label}`} />
                  </div>
                  <Button variant="ghost" size="sm" onClick={() => handleRemoveImage(im.id)} disabled={imgBusy}>
                    Remove image
                  </Button>
                </figure>
              ))}
            </div>
          ) : null}

          {detectedOptions.length === 0 && manualOptionCount === 0 && !isStem ? (
            <Notice tone="info">
              Set "how many options does this question have?" above to attach a picture per
              option here - for now this can only attach to Question or Answer.
            </Notice>
          ) : null}

          <form className="pi-attach" onSubmit={handleAttach}>
            <div className="es-field">
              <label className="es-field__label">Attach a new image to</label>
              <select className="es-input" value={slot} onChange={(e) => setSlot(e.target.value)}>
                {slotChoices.map((s) => (
                  <option key={s} value={s}>
                    {slotLabel(s)}
                  </option>
                ))}
              </select>
            </div>
            <div className="es-field">
              <span className="es-field__label">Image file</span>
              <label className="pi-file">
                <input type="file" accept="image/*" onChange={(e) => setFile(e.target.files?.[0] || null)} />
                {file ? file.name : 'Choose a PNG or JPG'}
              </label>
            </div>
            <Button variant="secondary" size="sm" type="submit" disabled={!file || imgBusy}>
              {imgBusy ? 'Attaching...' : 'Attach image'}
            </Button>
          </form>
          {imgError ? (
            <Notice tone="error" title="Could not attach this image">
              {imgError}
            </Notice>
          ) : null}
        </div>
      ) : null}

      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
        <Button variant="ghost" size="sm" onClick={handleRecreate} disabled={busy}>
          Clear and re-enter
        </Button>
        <Button variant="ghost" size="sm" onClick={handleRemove} disabled={busy}>
          Remove
        </Button>
      </div>
    </article>
  )
}
