import { useState } from 'react'
import { api } from '../../lib/api.js'
import RichEditor from '../../components/RichEditor.jsx'

function slotLabel(slot) {
  if (slot === 'question') return 'Question'
  if (slot === 'answer') return 'Answer'
  return `Option ${slot.replace('option:', '')}`
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

// One draft question (or part) on an uploaded paper. Marks and topic
// save on blur; text and answer (RichEditor) save on blur too, but each
// carries its own current value straight from the editor rather than
// through local state - see RichEditor's own note on why. Nothing on a
// 100+ question paper is ever left unsaved by a missed click. Images
// attach/detach immediately, same reasoning as Remove: one-shot actions,
// not something to leave pending.
export default function PaperIntakeQuestion({ q, isStem, images, onChanged, onRemoved, onImagesChanged }) {
  const { Button, Notice } = window.EdSider

  const [marks, setMarks] = useState(q.marks ?? '')
  const [topic, setTopic] = useState(q.topic || '')
  const [reviewed, setReviewed] = useState(!!q.reviewed)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const [answerOpen, setAnswerOpen] = useState(false)
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
    const n = Number(bareLabel(q.answer))
    return Number.isInteger(n) && n > 0 && n <= 8 ? n : 0
  })
  const [correctBusy, setCorrectBusy] = useState(false)

  // Mirrors the stem editor's live content (RichEditor's onChange) and
  // each option's current text - an option field's own blur needs the
  // CURRENT stem to reassemble the combined text, not just its own
  // value, so both have to be tracked outside RichEditor's own
  // mount-once state. Seeded once, from the row this card mounted with -
  // same reasoning as RichEditor's own `initialValue`, not reactive to
  // `q` changing later (a save this component made itself already has
  // the latest value locally; a save mounting a NEW q.id remounts this
  // whole component instead, via PaperIntake.jsx's key={q.id}).
  const [stemText, setStemText] = useState(() => q.mcq?.stem ?? '')
  const [optionTexts, setOptionTexts] = useState(() =>
    Object.fromEntries((q.mcq?.options || []).map((o) => [o.label, o.text])),
  )
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
  async function saveMcq(nextStem, nextOptionTexts) {
    if (!q.mcq) return
    setMcqBusy(true)
    setMcqSaveError(null)
    try {
      const payload = {
        stem: nextStem,
        options: q.mcq.options.map((o) => ({
          marker: o.marker,
          text: nextOptionTexts[o.label] ?? o.text,
        })),
      }
      const updated = await api.updateQuestionMcq(q.id, payload)
      onChanged(updated)
    } catch (err) {
      setMcqSaveError(err.message)
    } finally {
      setMcqBusy(false)
    }
  }

  async function handleMarkCorrect(label) {
    setCorrectBusy(true)
    setError(null)
    try {
      const updated = await api.updateIntakeQuestion(q.id, { answer: label })
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
    } catch (err) {
      setError(err.message)
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
  const detectedOptions = q.mcq?.options || []
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
        <span className={`es-pill ${reviewed ? 'es-tone--ok' : 'es-tone--neutral'}`}>
          {reviewed ? 'Reviewed' : 'Not reviewed'}
        </span>
      </div>

      {isStem ? (
        <p className="es-caption" style={{ margin: 0 }}>
          Shared text for the parts below - the scenario or stimulus they all refer to. Each
          part is answered separately.
        </p>
      ) : null}

      {detectedOptions.length > 0 ? (
        <RichEditor
          initialValue={stemText}
          onChange={setStemText}
          onBlur={(plain) => saveMcq(plain, optionTexts)}
          ariaLabel={`${label} text`}
        />
      ) : (
        <RichEditor
          initialValue={q.text || ''}
          onBlur={(plain) => saveIfChanged('text', plain, q.text || '')}
          ariaLabel={`${label} text`}
        />
      )}

      {detectedOptions.length > 0 ? (
        <div className="es-field">
          <label className="es-field__label">
            Answer options - correct any that were read wrong, and click the right one if it
            is not already marked
          </label>
          <div className="es-mcq pi-mcq">
            {detectedOptions.map((o) => {
              const isCorrect = q.mcq.correct === o.label.toLowerCase()
              return (
                <div
                  key={o.label}
                  className={`es-mcq__opt pi-mcq__opt${isCorrect ? ' es-mcq__opt--correct' : ''}`}
                >
                  <span className="es-mcq__key">{o.label}</span>
                  <input
                    className="pi-mcq__input"
                    value={optionTexts[o.label] ?? o.text}
                    aria-label={`${label} option ${o.label} text`}
                    disabled={mcqBusy}
                    onChange={(e) =>
                      setOptionTexts((prev) => ({ ...prev, [o.label]: e.target.value }))
                    }
                    onBlur={() => saveMcq(stemText, optionTexts)}
                  />
                  {isCorrect ? (
                    <span className="es-mcq__correct">Correct</span>
                  ) : (
                    <button
                      type="button"
                      className="pi-mcq__mark"
                      onClick={() => handleMarkCorrect(o.label)}
                      disabled={correctBusy}
                    >
                      Mark correct
                    </button>
                  )}
                </div>
              )
            })}
          </div>
          <p className="es-caption" style={{ marginTop: 6 }}>
            {q.mcq.correct
              ? "Marked from the paper's own answer key or bolded option - click a different one if it is wrong."
              : 'No correct answer found in the paper yet - click "Mark correct" on the right option.'}
          </p>
          {mcqSaveError ? (
            <Notice tone="error" title="Could not save the options">
              {mcqSaveError}
            </Notice>
          ) : null}
        </div>
      ) : q.depth === 0 ? (
        <div className="es-field">
          <label className="es-field__label">
            Options not split out automatically - common when every option is a picture rather
            than text. How many does this question have?
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
          {manualOptionCount > 0 ? (
            <>
              <p className="es-caption" style={{ marginTop: 10 }}>
                Attach each option's picture below under Images, then mark the correct one here.
              </p>
              <div className="es-mcq pi-mcq">
                {Array.from({ length: manualOptionCount }, (_, i) => String(i + 1)).map((n) => {
                  const isCorrect = bareLabel(q.answer) === n
                  return (
                    <div
                      key={n}
                      className={`es-mcq__opt pi-mcq__opt pi-mcq__opt--manual${isCorrect ? ' es-mcq__opt--correct' : ''}`}
                    >
                      <span className="es-mcq__key">{n}</span>
                      <span>Option {n}</span>
                      {isCorrect ? (
                        <span className="es-mcq__correct">Correct</span>
                      ) : (
                        <button
                          type="button"
                          className="pi-mcq__mark"
                          onClick={() => handleMarkCorrect(n)}
                          disabled={correctBusy}
                        >
                          Mark correct
                        </button>
                      )}
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
            initialValue={q.answer || ''}
            onBlur={(plain) => saveIfChanged('answer', plain, q.answer || '')}
            ariaLabel={`${label} answer`}
          />
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
                    <img src={api.imageUrl(im.id)} alt={`${slotLabel(im.slot)} for ${label}`} />
                  </div>
                  <Button variant="ghost" size="sm" onClick={() => handleRemoveImage(im.id)} disabled={imgBusy}>
                    Remove image
                  </Button>
                </figure>
              ))}
            </div>
          ) : null}

          {detectedOptions.length === 0 && manualOptionCount === 0 && q.depth === 0 ? (
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

      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
        <Button variant="ghost" size="sm" onClick={handleRemove} disabled={busy}>
          Remove
        </Button>
      </div>
    </article>
  )
}
