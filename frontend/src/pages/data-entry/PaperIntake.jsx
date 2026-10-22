import { Fragment, useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '../../lib/api.js'
import { useFileUrl } from '../../lib/useFileUrl.jsx'
import PaperIntakeQuestion, { paperOptionStyle } from './PaperIntakeQuestion.jsx'
import AssessmentSettings from './AssessmentSettings.jsx'
import EarlyDuplicateCheck from './EarlyDuplicateCheck.jsx'
import { useToast } from '../../components/Toast.jsx'
import MarksSection from './MarksSection.jsx'
import AddQuestion from './AddQuestion.jsx'
import PaperFinder from './PaperFinder.jsx'
import EdsiderLabels from './EdsiderLabels.jsx'
import { AddGroupInstruction, GroupInstructionBanner, groupsStartingAt } from './GroupInstructions.jsx'
import './PaperIntake.css'

const OCR_LANGS = [
  { key: 'eng', label: 'English' },
  { key: 'sin', label: 'Sinhala' },
  { key: 'tam', label: 'Tamil' },
]
const SPLIT_STYLES = [
  { key: 'number', label: 'Numbered at the left margin (1.  2.  01.  Q1  (1))' },
  { key: 'question_word', label: "Lines containing the word 'Question'" },
]
const RAIL = [
  { id: 'upload', icon: 'upload', label: 'Upload' },
  { id: 'paper', icon: 'paper', label: 'Questions' },
  { id: 'dup', icon: 'copy', label: 'Duplicate check' },
  { id: 'status', icon: 'check', label: 'Review status' },
]
const KIND_LABEL = { mcq: 'Multiple choice', structured: 'Structured' }

function gradeLabel(code) {
  const m = /^G(\d{2})$/.exec(code || '')
  return m ? `Grade ${Number(m[1])}` : code
}

// Depth-0 questions in seq order, each followed immediately by its own
// children (parent_seq pointing at it) - a flat list from the API
// becomes the tree a reviewer actually reads.
function orderedTree(questions) {
  const childrenOf = new Map()
  questions.forEach((q) => {
    if (q.parent_seq != null) {
      if (!childrenOf.has(q.parent_seq)) childrenOf.set(q.parent_seq, [])
      childrenOf.get(q.parent_seq).push(q)
    }
  })
  const out = []
  function visit(q) {
    out.push(q)
    for (const child of childrenOf.get(q.seq) || []) visit(child)
  }
  questions.filter((q) => q.parent_seq == null).forEach((q) => visit(q))
  const seen = new Set(out.map((q) => q.id))
  questions.forEach((q) => {
    if (!seen.has(q.id)) out.push(q)
  })
  return out
}

// Groups the tree back into the paper's own parts, in document order -
// a run of consecutive questions sharing one `section` value is one
// part, with that part's own metadata (kind, instructions, marks_each)
// from doc.sections when a label matches.
function groupByPart(tree, sectionsMeta) {
  const byLabel = Object.fromEntries((sectionsMeta || []).map((s) => [s.label, s]))
  const groups = []
  let current = null
  for (const q of tree) {
    const key = q.section || null
    if (!current || current.key !== key) {
      current = { key, meta: key ? byLabel[key] : null, questions: [] }
      groups.push(current)
    }
    current.questions.push(q)
  }
  return groups
}

export default function PaperIntake() {
  const { PageHeader, Button, Notice, StatusPill, PairReview, QuestionCard, Icon } = window.EdSider
  const navigate = useNavigate()
  const toast = useToast()
  const [searchParams, setSearchParams] = useSearchParams()
  const selectedId = searchParams.get('doc')

  const [facets, setFacets] = useState({ grades: [], subjects: [] })

  const [file, setFile] = useState(null)
  const [lang, setLang] = useState('eng')
  // 'file' - uploaded and read here; 'text' - already read elsewhere (the
  // local OCR tool on a data-entry computer) and pasted in.
  const [source, setSource] = useState('file')
  const [pasted, setPasted] = useState('')
  const [pastedTitle, setPastedTitle] = useState('')
  // A PDF typed in a legacy Sinhala font has a text layer of font codes, not
  // Sinhala - reading each page as a picture is the only reliable text for it.
  const [forceOcr, setForceOcr] = useState(false)
  const [uploadStyle, setUploadStyle] = useState('number')
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState(null)

  const [listVersion, setListVersion] = useState(0)

  const [doc, setDoc] = useState(null)
  const [questions, setQuestions] = useState(null)
  const optionStyle = useMemo(() => paperOptionStyle(questions), [questions])
  const [images, setImages] = useState({})
  // Keyed by question id (string) - a teacher's comments from the Teacher
  // review app, read-only here. Fetched whenever a paper reaches this page
  // without regard to its status: a paper most needs them right after the
  // teacher sends it back (status "refer_back"), but nothing stops a
  // comment existing earlier either.
  const [comments, setComments] = useState({})
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState(null)

  const [title, setTitle] = useState('')
  const [grade, setGrade] = useState('')
  const [subject, setSubject] = useState('')
  const [notes, setNotes] = useState('')
  const [applyBusy, setApplyBusy] = useState(false)

  const [splitStyle, setSplitStyle] = useState('number')
  const [resplitOpen, setResplitOpen] = useState(false)
  const [preview, setPreview] = useState(null)
  const [resplitBusy, setResplitBusy] = useState(false)
  const [resplitError, setResplitError] = useState(null)

  const [instrOpenKey, setInstrOpenKey] = useState(null)
  const [instrText, setInstrText] = useState('')
  const [instrBusy, setInstrBusy] = useState(false)
  const [groupBusy, setGroupBusy] = useState(false)
  const [instrError, setInstrError] = useState(null)

  const [addOpen, setAddOpen] = useState(false)

  const [checkBusy, setCheckBusy] = useState(false)
  const [checkError, setCheckError] = useState(null)
  const [checkResult, setCheckResult] = useState(null)

  const [previewMode, setPreviewMode] = useState('file')
  // The paper itself, fetched signed-in (a src cannot carry the sign-in
  // token) - only for a PDF or picture, and only while it is on show.
  const showsFile = Boolean(doc && previewMode === 'file' && ['pdf', 'image'].includes(doc.file_type))
  const paperFile = useFileUrl(showsFile ? api.documentFilePath(selectedId) : null)

  useEffect(() => {
    api.questionFacets().then(setFacets).catch(() => {})
  }, [])

  // The paper finder reads its own page of papers; bumping this makes it
  // read again after something here changed a paper's status or title.
  function refreshList() {
    setListVersion((v) => v + 1)
  }

  function loadSelected(id) {
    if (!id) {
      setDoc(null)
      setQuestions(null)
      setImages({})
      setComments({})
      return
    }
    setLoading(true)
    setLoadError(null)
    setCheckResult(null)
    Promise.all([
      api.getIntakeDocument(id),
      api.listIntakeQuestions(id),
      api.listDocumentImages(id),
      api.listDocumentComments(id),
    ])
      .then(([d, qs, imgs, cmts]) => {
        setDoc(d)
        setQuestions(qs)
        setImages(imgs)
        setComments(cmts)
        setTitle(d.paper_title || '')
        setGrade(d.paper_grade || '')
        setSubject(d.paper_subject || '')
        setNotes(d.notes || '')
        setSplitStyle(d.split_style || 'number')
        setPreview(null)
        setResplitOpen(false)
        setInstrOpenKey(null)
        setAddOpen(false)
        setPreviewMode('file')
      })
      .catch((err) => setLoadError(err.message))
      .finally(() => setLoading(false))
  }
  useEffect(() => {
    loadSelected(selectedId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId])

  async function handleUpload(e) {
    e.preventDefault()
    if (source === 'file' ? !file : !pasted.trim()) return
    setUploading(true)
    setUploadError(null)
    try {
      const d =
        source === 'file'
          ? await api.uploadIntakeDocument(file, lang, uploadStyle, forceOcr)
          : await api.createDocumentFromText(pasted, pastedTitle.trim(), uploadStyle)
      e.target.reset()
      setFile(null)
      setPasted('')
      setPastedTitle('')
      refreshList()
      setSearchParams({ doc: String(d.id) })
      toast.success(`${d.filename} added - its questions have been extracted.`)
    } catch (err) {
      setUploadError(err.message)
      toast.error(`Upload failed: ${err.message}`)
    } finally {
      setUploading(false)
    }
  }

  async function saveMeta(fields) {
    try {
      const d = await api.updateIntakeDocument(selectedId, fields)
      setDoc(d)
      refreshList()
    } catch (err) {
      setLoadError(err.message)
    }
  }

  // Every group instruction is saved as one list - see GroupInstructions.jsx.
  async function saveGroups(list) {
    setGroupBusy(true)
    try {
      setDoc(await api.updateIntakeDocument(selectedId, { instruction_groups: list }))
      toast.success('Instructions saved.')
    } catch (err) {
      toast.error(`Could not save the instructions: ${err.message}`)
      throw err
    } finally {
      setGroupBusy(false)
    }
  }

  // The printed question numbers in one part, for the from/to choices.
  function questionNumbersIn(section) {
    return (questions || [])
      .filter((q) => !q.depth && (q.section ?? null) === (section ?? null) && q.question_number)
      .map((q) => q.question_number)
  }

  async function handleApplyToAll() {
    if (!questions?.length) return
    setApplyBusy(true)
    try {
      const updated = await Promise.all(
        questions.map((q) => api.updateIntakeQuestion(q.id, { grade, subject })),
      )
      setQuestions(updated)
    } catch (err) {
      setLoadError(err.message)
    } finally {
      setApplyBusy(false)
    }
  }

  function handleQuestionChanged(updated) {
    setQuestions((prev) => prev.map((q) => (q.id === updated.id ? updated : q)))
  }
  function handleQuestionRemoved(id) {
    setQuestions((prev) => prev.filter((q) => q.id !== id))
  }
  function handleImagesChanged(qid, next) {
    setImages((prev) => ({ ...prev, [String(qid)]: next }))
  }

  async function handlePreview() {
    setResplitBusy(true)
    setResplitError(null)
    try {
      setPreview(await api.previewResplit(selectedId, splitStyle))
    } catch (err) {
      setResplitError(err.message)
    } finally {
      setResplitBusy(false)
    }
  }
  async function handleApplyResplit() {
    setResplitBusy(true)
    setResplitError(null)
    try {
      await api.resplitDocument(selectedId, splitStyle)
      setPreview(null)
      loadSelected(selectedId)
      refreshList()
      toast.success('Paper re-split - its questions have been replaced.')
    } catch (err) {
      setResplitError(err.message)
      toast.error(`Re-split failed: ${err.message}`)
    } finally {
      setResplitBusy(false)
    }
  }

  function toggleInstructions(key, current) {
    if (instrOpenKey === key) {
      setInstrOpenKey(null)
      return
    }
    setInstrOpenKey(key)
    setInstrText(current || '')
    setInstrError(null)
  }
  async function saveInstructions(key) {
    setInstrBusy(true)
    setInstrError(null)
    try {
      const nextSections = (doc.sections || []).map((s) =>
        s.label === key ? { ...s, instructions: instrText || null } : s,
      )
      const d = await api.updateIntakeDocument(selectedId, { sections: nextSections })
      setDoc(d)
      setInstrOpenKey(null)
    } catch (err) {
      setInstrError(err.message)
    } finally {
      setInstrBusy(false)
    }
  }

  // A question added by hand is placed in the middle of the paper, so the
  // ones after it have moved down a place - read the list again.
  async function handleQuestionAdded(number) {
    setQuestions(await api.listIntakeQuestions(selectedId))
    toast.success(`Question ${number} added.`)
  }

  async function handleCheckDuplicates() {
    setCheckBusy(true)
    setCheckError(null)
    try {
      const res = await api.checkDocumentDuplicates(selectedId)
      setCheckResult(res)
      setDoc(await api.getIntakeDocument(selectedId))
      refreshList()
      const exact = res.exact_groups.length
      const similar = res.similar_pairs.length
      toast.success(
        exact || similar
          ? `Duplicate check completed - ${exact} exact repeat group(s) and ${similar} similar pair(s) found.`
          : 'Duplicate check completed - no repeats found.',
      )
    } catch (err) {
      setCheckError(err.message)
      toast.error(`Duplicate check failed: ${err.message}`)
    } finally {
      setCheckBusy(false)
    }
  }

  const pending = questions ? questions.filter((q) => !q.reviewed).length : 0
  // The student view is only offered once the paper has been through both
  // checks the workflow requires: every question reviewed, and the
  // duplicate check run (which sets the status to 'checked' or later).
  const DUPLICATES_CHECKED = ['checked', 'teacher_review', 'refer_back', 'review_done', 'published']
  const duplicatesChecked = !!doc && DUPLICATES_CHECKED.includes(doc.status)
  const previewReady = !!doc && !!questions && pending === 0 && duplicatesChecked
  const previewBlocker = !questions
    ? ''
    : pending > 0
      ? `Mark the remaining ${pending} question(s) as reviewed to preview the paper as a student.`
      : !duplicatesChecked
        ? 'Run the duplicate check to preview the paper as a student.'
        : ''
  const lowConf = questions ? questions.filter((q) => q.extraction_confidence === 'low').length : 0
  const tree = questions ? orderedTree(questions) : []
  const parts = doc ? groupByPart(tree, doc.sections) : []
  const byId = useMemo(() => Object.fromEntries((questions || []).map((q) => [q.id, q])), [questions])

  function brief(id) {
    const q = byId[id]
    if (!q) return { id: String(id), text: '(question no longer on this paper)' }
    return {
      id: String(q.id),
      text: q.text || '',
      grade: doc.paper_grade,
      subject: doc.paper_subject,
      number: q.question_number || q.seq,
    }
  }

  return (
    <div className="pi-page">
      <nav className="pi-rail-col" aria-label="Paper intake sections">
        <ul className="es-rail">
          {RAIL.map((r) => (
            <li key={r.id}>
              <a href={`#${r.id}`} aria-label={r.label} title={r.label}>
                <Icon name={r.icon} />
              </a>
            </li>
          ))}
        </ul>
      </nav>

      <div className="pi-main">
        <PageHeader
          accent="Paper"
          title="intake"
          subtitle="Upload a question paper - PDF, Word, or an image of a handwritten one. Its questions are pulled out, you correct them, then the batch is checked for repeats against itself before any of it joins the bank. Nothing here is trusted until you have confirmed it."
        />

        <div className="es-row" role="radiogroup" aria-label="How to add the paper" style={{ marginBottom: 8 }}>
          <label className="scr-radio">
            <input type="radio" name="src" checked={source === 'file'} onChange={() => setSource('file')} />
            Upload a file
          </label>
          <label className="scr-radio">
            <input type="radio" name="src" checked={source === 'text'} onChange={() => setSource('text')} />
            Paste text
          </label>
        </div>

        <section id="upload" className="es-card pi-upload__grid" aria-label="Add a paper">
          {source === 'file' ? (
            <label className="pi-dropzone">
              <input
                type="file"
                accept=".pdf,.doc,.docx,.png,.jpg,.jpeg,.tif,.tiff,.bmp"
                onChange={(e) => setFile(e.target.files?.[0] || null)}
              />
              <span className="es-stat__icon es-accent--primary">
                <Icon name="upload" />
              </span>
              <p className="scr-h3">{file ? file.name : 'Drop the question paper here'}</p>
              <p className="es-caption">PDF, Word (.docx, .doc), or an image of the paper - PNG, JPG, TIFF or BMP</p>
              <span className="es-btn es-btn--secondary es-btn--sm">Choose a file</span>
            </label>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              <div className="es-field">
                <label className="es-field__label" htmlFor="pi-paste-title">
                  Name of the paper
                </label>
                <input
                  id="pi-paste-title"
                  className="es-input"
                  value={pastedTitle}
                  placeholder="e.g. Grade 12 Business Statistics - Term 1"
                  onChange={(e) => setPastedTitle(e.target.value)}
                />
              </div>
              <div className="es-field">
                <label className="es-field__label" htmlFor="pi-paste-text">
                  The paper's text
                </label>
                <textarea
                  id="pi-paste-text"
                  className="es-input"
                  rows={14}
                  value={pasted}
                  placeholder="Paste the text the reading tool gave you - the whole paper, questions and answers."
                  onChange={(e) => setPasted(e.target.value)}
                />
              </div>
              <p className="es-caption" style={{ margin: 0 }}>
                For a scanned paper read on your own computer. Keep each question, choice and part on
                its own line, starting with its number or letter - that is how the questions are
                found.
              </p>
            </div>
          )}

          <form className="pi-upload__opts" onSubmit={handleUpload}>
            {source === 'file' ? (
            <>
            <fieldset style={{ border: 0, margin: 0, padding: 0 }}>
              <legend className="es-field__label" style={{ marginBottom: 8 }}>
                Language of the paper
              </legend>
              <div className="es-row">
                {OCR_LANGS.map((l) => (
                  <label className="scr-radio" key={l.key}>
                    <input type="radio" name="ul" checked={lang === l.key} onChange={() => setLang(l.key)} />
                    {l.label}
                  </label>
                ))}
              </div>
              <p className="es-caption" style={{ marginTop: 6 }}>
                Used to read scanned pages and questions pasted in as pictures. Typed text is read
                directly whatever this says.
              </p>
            </fieldset>

            <label className="scr-radio" style={{ alignItems: 'flex-start' }}>
              <input type="checkbox" checked={forceOcr} onChange={(e) => setForceOcr(e.target.checked)} />
              <span>
                Read every page as a picture
                <span className="es-caption" style={{ display: 'block' }}>
                  Use for a PDF whose Sinhala or Tamil text looks like wrong letters after upload - the page is
                  read from its picture instead. Slower.
                </span>
              </span>
            </label>
            </>
            ) : null}

            <fieldset style={{ border: 0, margin: 0, padding: 0 }}>
              <legend className="es-field__label" style={{ marginBottom: 8 }}>
                Where does a new question start in this paper?
              </legend>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8, alignItems: 'flex-start' }}>
                {SPLIT_STYLES.map((s) => (
                  <label className="scr-radio" key={s.key}>
                    <input
                      type="radio"
                      name="us"
                      checked={uploadStyle === s.key}
                      onChange={() => setUploadStyle(s.key)}
                    />
                    {s.label}
                  </label>
                ))}
              </div>
              <p className="es-caption" style={{ marginTop: 6 }}>
                Pick "Question" for a worked-solutions or marking-scheme document, where the
                numbered lists are answer steps rather than questions.
              </p>
            </fieldset>

            <div>
              <Button
                variant="primary"
                arrow
                type="submit"
                disabled={(source === 'file' ? !file : !pasted.trim()) || uploading}
              >
                {uploading ? 'Extracting...' : 'Extract questions'}
              </Button>
            </div>
            {uploadError ? (
              <Notice tone="error" title="Could not upload this paper">
                {uploadError}
              </Notice>
            ) : null}
          </form>
        </section>

        <section id="paper" className="es-card" aria-label="Paper details" style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
          <h2 className="scr-h2" style={{ margin: 0 }}>Uploaded papers</h2>
          <PaperFinder
            selectedId={selectedId}
            onSelect={(id, opts) => setSearchParams({ doc: id }, opts)}
            refreshKey={listVersion}
          />
          {doc ? (
            <div className="pi-paper__head">
              <p className="scr-h3" style={{ margin: 0 }}>
                Open: #{doc.id} - {doc.paper_title || doc.filename}
              </p>
              <StatusPill status={doc.status} />
            </div>
          ) : null}

          {doc && doc.status === 'refer_back' ? (
            <Notice tone="warning" title="Referred back by the teacher">
              This paper was sent back for correction. The teacher's comments are shown on
              the questions they apply to, below - fix what they flagged, then run the
              duplicate check again to send it back to assignment.
            </Notice>
          ) : null}

          {loadError ? (
            <Notice tone="error" title="Could not load this paper">
              {loadError}
            </Notice>
          ) : null}
          {loading ? <Notice tone="info">Loading...</Notice> : null}

          {!loading && !doc ? (
            <Notice tone="info">
              No paper is open. Choose one from the list above to carry on reviewing it, or upload a new paper.
            </Notice>
          ) : null}

          {doc ? (
            <>
              <p className="es-caption" style={{ marginTop: -8 }}>
                Extractor: {doc.extractor || 'unknown'}
                {doc.uploaded_at ? ` - uploaded ${doc.uploaded_at.slice(0, 10)}` : ''}
              </p>

              {/* A scanned paper is pictures, not text, and this server has no
                  OCR (Tesseract cannot be installed on its Azure hosting) - it
                  came back as one empty draft with only the caption above to
                  say why. Scans are read with the local reader instead. */}
              {/not read|^unavailable/i.test(doc.extractor || '') ? (
                <Notice tone="warning" title="Some pages of this paper could not be read here">
                  They are pictures - a scanned paper - and this server cannot read text out of
                  pictures. Read the paper with the EdsiderAI reader on a data-entry computer, copy
                  its text, then choose "Paste text" above and paste it in. Sinhala and Tamil scans
                  should always go that way.
                </Notice>
              ) : null}

              <button
                className="pi-exp"
                type="button"
                aria-expanded={resplitOpen}
                onClick={() => setResplitOpen((v) => !v)}
              >
                Split it differently
                <span className="pi-exp__chev">{resplitOpen ? 'Hide' : 'Show'}</span>
              </button>
              {resplitOpen ? (
                <div className="pi-panel" style={{ marginTop: -18 }}>
                  <p className="es-caption" style={{ margin: 0 }}>
                    The paper's text is kept, so it can be re-split without uploading it again.
                    Re-splitting REPLACES the questions below, including any edits already made to
                    them.
                  </p>
                  <div className="es-row">
                    <select className="es-input" value={splitStyle} onChange={(e) => setSplitStyle(e.target.value)}>
                      {SPLIT_STYLES.map((s) => (
                        <option key={s.key} value={s.key}>
                          {s.label}
                        </option>
                      ))}
                    </select>
                    <Button variant="secondary" size="sm" onClick={handlePreview} disabled={resplitBusy}>
                      Preview
                    </Button>
                  </div>
                  {preview ? (
                    <p style={{ margin: 0, fontSize: 15 }}>
                      That style finds <b>{preview.question_count}</b> question(s) across{' '}
                      {preview.sections.length} part(s). Currently stored: <b>{preview.currently_stored}</b>.
                    </p>
                  ) : null}
                  {resplitError ? (
                    <Notice tone="error" title="Could not re-split">
                      {resplitError}
                    </Notice>
                  ) : null}
                  {preview ? (
                    <div>
                      <Button variant="primary" size="sm" onClick={handleApplyResplit} disabled={resplitBusy}>
                        {resplitBusy ? 'Re-splitting...' : 'Re-split this paper'}
                      </Button>
                    </div>
                  ) : null}
                </div>
              ) : null}

              <h2 className="pi-paper__title">
                {doc.paper_subject || 'Subject not set'}{' '}
                <span className="accent">
                  - {doc.paper_grade ? gradeLabel(doc.paper_grade) : 'grade not set'} -{' '}
                  {title || 'title not set'}
                </span>
              </h2>
              <h3 className="scr-h3" style={{ marginTop: 4 }}>
                Paper details
              </h3>
              <div className="pi-paper__fields">
                <div className="es-field">
                  <label className="es-field__label" htmlFor="pi-grade">
                    Grade
                  </label>
                  <select
                    id="pi-grade"
                    className="es-input"
                    value={grade}
                    onChange={(e) => {
                      setGrade(e.target.value)
                      saveMeta({ paper_grade: e.target.value })
                    }}
                  >
                    <option value="">Not set</option>
                    {facets.grades.map((g) => (
                      <option key={g} value={g}>
                        {gradeLabel(g)}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="es-field">
                  <label className="es-field__label" htmlFor="pi-subject">
                    Subject
                  </label>
                  <select
                    id="pi-subject"
                    className="es-input"
                    value={subject}
                    onChange={(e) => {
                      setSubject(e.target.value)
                      saveMeta({ paper_subject: e.target.value })
                    }}
                  >
                    <option value="">Not set</option>
                    {facets.subjects.map((s) => (
                      <option key={s} value={s}>
                        {s}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="es-field">
                  <label className="es-field__label" htmlFor="pi-title">
                    Title
                  </label>
                  <input
                    id="pi-title"
                    className="es-input"
                    value={title}
                    onChange={(e) => setTitle(e.target.value)}
                    onBlur={() => title !== (doc.paper_title || '') && saveMeta({ paper_title: title })}
                  />
                </div>
                <Button variant="secondary" size="sm" onClick={handleApplyToAll} disabled={applyBusy}>
                  {applyBusy ? 'Applying...' : 'Apply to all'}
                </Button>
              </div>
              <EdsiderLabels key={`edsider-${doc.id}`} doc={doc} onSaved={setDoc} />

            </>
          ) : null}
        </section>

        {doc && questions ? (
          <EarlyDuplicateCheck key={`precheck-${doc.id}`} doc={doc} questions={questions} />
        ) : null}

        {doc ? <AssessmentSettings key={doc.id} doc={doc} onSaved={setDoc} /> : null}

        {doc && questions ? (
          <MarksSection
            key={`marks-${doc.id}`}
            doc={doc}
            questions={questions}
            onQuestionChanged={handleQuestionChanged}
            onSaved={setDoc}
          />
        ) : null}

        {doc && questions ? (
          <>
            <div className="pi-questions__head">
              <h2 className="scr-h2">Questions ({questions.length})</h2>
              <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
                <p className="es-caption" style={{ margin: 0 }}>
                  {questions.length - pending} of {questions.length} marked reviewed
                </p>
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={!previewReady}
                  onClick={() => navigate(`/app/data-entry/paper-intake/preview?doc=${doc.id}`)}
                >
                  Preview as student
                </Button>
              </div>
            </div>
            {!previewReady ? (
              <p className="es-caption" style={{ marginTop: -8 }}>
                {previewBlocker}
              </p>
            ) : null}
            {lowConf > 0 ? (
              <Notice tone="warning" title={`${lowConf} of ${questions.length} came out low-confidence`}>
                Almost no text was read for these. They are marked below.
              </Notice>
            ) : null}
            <p className="es-caption" style={{ marginTop: -12 }}>
              Grade and subject come from Paper details above - use "Apply to all" there if the
              extractor set them per question differently. Set the marks per question, fix the
              text, and tick Reviewed once a question is correct.
            </p>

            <div className="pi-two-col">
              <section aria-label="Questions" className="pi-question-list">
                {/* Instructions, from the widest to the narrowest: the whole
                    paper here, each part under its heading, and groups of
                    questions above the first question they cover. */}
                <div className="pi-panel" style={{ display: 'grid', gap: 8, marginBottom: 12 }}>
                  <label className="es-field__label" htmlFor="pi-notes" style={{ margin: 0 }}>
                    Instructions for the whole paper
                  </label>
                  <textarea
                    id="pi-notes"
                    className="es-input"
                    rows={3}
                    value={notes}
                    placeholder='e.g. "This paper consists of 50 questions. Answer all the questions. Time: 2 hours."'
                    onChange={(e) => setNotes(e.target.value)}
                    onBlur={() => notes !== (doc.notes || '') && saveMeta({ notes: notes || null })}
                  />
                  <p className="es-caption" style={{ margin: 0 }}>
                    Filled in from the paper's opening text where the extractor found it - correct or
                    shorten it as needed. For instructions that cover only some questions (&quot;answer 20
                    to 25...&quot;) or a part whose heading was not picked up, add a group below.
                  </p>
                  <div>
                    <AddGroupInstruction
                      groups={doc.instruction_groups || []}
                      numbersFor={questionNumbersIn}
                      partLabels={(doc.sections || []).map((s) => s.label).filter(Boolean)}
                      onSave={saveGroups}
                      busy={groupBusy}
                    />
                  </div>
                </div>
                {parts.map((part, pi) => {
                  const hasHeader = !!part.key
                  const meta = part.meta
                  const caption = meta
                    ? [
                        KIND_LABEL[meta.kind] || 'Mixed',
                        `${meta.questions} question(s)`,
                        meta.marks_each ? `${meta.marks_each} marks each` : null,
                      ]
                        .filter(Boolean)
                        .join('  |  ')
                    : null
                  const childrenSeqs = new Set(questions.map((q) => q.parent_seq).filter((x) => x != null))
                  return (
                    <div key={`${part.key}-${pi}`}>
                      {hasHeader ? (
                        <>
                          <div className="pi-parthead">
                            <div>
                              <p className="scr-h2" style={{ color: 'var(--primary)' }}>
                                {part.key}
                              </p>
                              {caption ? <p className="es-caption">{caption}</p> : null}
                            </div>
                            <Button
                              variant="ghost"
                              size="sm"
                              onClick={() => toggleInstructions(part.key, meta?.instructions)}
                            >
                              Instructions for this part
                            </Button>
                          </div>
                          {instrOpenKey === part.key ? (
                            <div className="pi-panel" style={{ marginBottom: 12 }}>
                              <textarea
                                className="es-input"
                                rows={3}
                                value={instrText}
                                placeholder="e.g. Answer all questions. Each question carries 2 marks."
                                onChange={(e) => setInstrText(e.target.value)}
                              />
                              {instrError ? (
                                <Notice tone="error" title="Could not save">
                                  {instrError}
                                </Notice>
                              ) : null}
                              <div>
                                <Button
                                  variant="primary"
                                  size="sm"
                                  onClick={() => saveInstructions(part.key)}
                                  disabled={instrBusy}
                                >
                                  {instrBusy ? 'Saving...' : 'Save instructions'}
                                </Button>
                              </div>
                            </div>
                          ) : null}
                        </>
                      ) : null}
                      <div className="pi-draft-list">
                        {part.questions.map((q) => (
                          <Fragment key={q.id}>
                          {!q.depth
                            ? groupsStartingAt(doc.instruction_groups, q.section, q.question_number).map((g) => {
                                const index = doc.instruction_groups.indexOf(g)
                                return (
                                  <GroupInstructionBanner
                                    key={`group-${index}`}
                                    group={g}
                                    index={index}
                                    groups={doc.instruction_groups}
                                    numbersFor={questionNumbersIn}
                                    partLabels={(doc.sections || []).map((s) => s.label).filter(Boolean)}
                                    onSave={saveGroups}
                                    busy={groupBusy}
                                  />
                                )
                              })
                            : null}
                          <PaperIntakeQuestion
                            q={q}
                            isStem={childrenSeqs.has(q.seq)}
                            images={images[String(q.id)] || []}
                            comments={comments[String(q.id)] || []}
                            optionStyle={optionStyle}
                            onChanged={handleQuestionChanged}
                            onRemoved={handleQuestionRemoved}
                            onImagesChanged={handleImagesChanged}
                          />
                          </Fragment>
                        ))}
                      </div>
                    </div>
                  )
                })}

                <button
                  className="pi-exp"
                  type="button"
                  aria-expanded={addOpen}
                  onClick={() => setAddOpen((v) => !v)}
                >
                  Add a question the extractor missed
                  <span className="pi-exp__chev">{addOpen ? 'Hide' : 'Show'}</span>
                </button>
                {addOpen ? (
                  <AddQuestion docId={selectedId} tree={tree} parts={parts} onAdded={handleQuestionAdded} />
                ) : null}
              </section>

              <aside className="es-card pi-original" aria-label="Original paper">
                <div className="pi-original__head">
                  <h2 className="scr-h3">Original paper</h2>
                  <div className="es-row" style={{ gap: 6 }}>
                    <Button
                      variant={previewMode === 'file' ? 'secondary' : 'ghost'}
                      size="sm"
                      onClick={() => setPreviewMode('file')}
                    >
                      File
                    </Button>
                    <Button
                      variant={previewMode === 'text' ? 'secondary' : 'ghost'}
                      size="sm"
                      onClick={() => setPreviewMode('text')}
                    >
                      Extracted text
                    </Button>
                  </div>
                </div>

                {previewMode === 'file' ? (
                  doc.file_type === 'pdf' ? (
                    paperFile.url ? (
                      <iframe className="pi-original__frame" title="Original uploaded PDF" src={paperFile.url} />
                    ) : (
                      <p className="es-caption">
                        {paperFile.error ? `Could not load the paper: ${paperFile.error.message}` : 'Loading the paper...'}
                      </p>
                    )
                  ) : doc.file_type === 'image' ? (
                    <div className="pi-original__body pi-original__body--img">
                      {paperFile.url ? <img src={paperFile.url} alt="The uploaded paper" /> : null}
                    </div>
                  ) : (
                    <Notice tone="info">
                      {doc.file_type === 'docx'
                        ? "This is a Word document - there is no in-browser preview for it, only the text the extractor read. Switch to \"Extracted text\", or open the original .docx file directly."
                        : doc.file_type === 'text'
                          ? 'This paper was pasted in as text, so there is no file to show. Switch to "Extracted text" to see what was pasted.'
                          : 'No file preview available for this upload. Switch to "Extracted text" instead.'}
                    </Notice>
                  )
                ) : (
                  <div className="pi-original__body">
                    {doc.raw_text || 'No stored text for this paper (uploaded before this was kept).'}
                  </div>
                )}

                <p className="es-caption" style={{ margin: 0 }}>
                  {previewMode === 'file'
                    ? 'The real uploaded file, scrollable and (for a PDF) selectable - check a suspect read against it without losing your place in the list.'
                    : 'The plain text the extractor actually read from the file, for comparing against a suspect question.'}
                </p>
              </aside>
            </div>

            <section id="dup" className="es-card" aria-label="Duplicate check" style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
              <h2 className="scr-h2">Duplicate check within this paper</h2>
              <p className="es-caption">
                The same question must not appear twice on one paper. Runs only once every
                question is marked reviewed, because it is meaningless on text that has not been
                corrected yet.
              </p>
              {pending > 0 ? (
                <Notice tone="info">{pending} question(s) still not marked reviewed.</Notice>
              ) : null}
              {checkError ? (
                <Notice tone="error" title="Could not run the check">
                  {checkError}
                </Notice>
              ) : null}
              <div>
                <Button variant="primary" onClick={handleCheckDuplicates} disabled={pending > 0 || checkBusy}>
                  {checkBusy ? 'Checking...' : 'Check for duplicates'}
                </Button>
              </div>

              {checkResult ? (
                <div className="pi-check__results">
                  {checkResult.exact_groups.length === 0 && checkResult.similar_pairs.length === 0 ? (
                    <Notice tone="success">No repeats found on this paper.</Notice>
                  ) : null}

                  {checkResult.exact_groups.map((ids, i) => (
                    <div className="es-card" key={`exact-${i}`} style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                      <span className="es-pill es-tone--dup">Exact copies</span>
                      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 14 }}>
                        {ids.map((id) => (
                          <QuestionCard key={id} {...brief(id)} />
                        ))}
                      </div>
                    </div>
                  ))}

                  {checkResult.similar_pairs.map((p, i) => (
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
            </section>

            <section id="status" className="es-card pi-status" aria-label="Review status">
              <div>
                <h2 className="scr-h2">Review status</h2>
                <p className="es-caption pi-status__text" style={{ marginTop: 4 }}>
                  Data entry's own job stops at the duplicate check above. Assigning a checked
                  paper to a teacher is a separate person's job, in the paper-assignment app. Run
                  the duplicate check above first.
                </p>
              </div>
              <StatusPill status={doc.status} />
            </section>
          </>
        ) : null}
      </div>
    </div>
  )
}
