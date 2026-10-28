import { useState } from 'react'
import { api } from '../../lib/api.js'
import { useToast } from '../../components/Toast.jsx'
import { OCR_LANGS, SPLIT_STYLES } from '../data-entry/paperOptions.js'
import '../data-entry/PaperIntake.css'

// Adding a paper - an upload, or text read on a data-entry computer and
// pasted in. Only on the admin side: whoever assigns papers adds them, then
// gives each to a Data Entry Operator in the list below this form.
export default function UploadPaper({ onUploaded }) {
  const { Button, Notice, Icon } = window.EdSider
  const toast = useToast()
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
      onUploaded(d)
      toast.success(`${d.filename} added - its questions have been extracted.`)
    } catch (err) {
      setUploadError(err.message)
      toast.error(`Upload failed: ${err.message}`)
    } finally {
      setUploading(false)
    }
  }

  return (
    <>
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
    </>
  )
}
