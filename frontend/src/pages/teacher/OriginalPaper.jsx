import { api } from '../../lib/api.js'
import { useFileUrl } from '../../lib/useFileUrl.jsx'

// The paper as it was uploaded, shown beside the questions so the teacher can
// compare each one with the original. A PDF and a picture are shown as they
// are; a Word file cannot be shown in a browser, so the text that was read
// from it is shown instead, with a download link for the file itself. The
// file is fetched signed-in (see useFileUrl) - an <iframe> pointed straight
// at the API cannot carry the sign-in token.

export default function OriginalPaper({ doc }) {
  const kind = doc.file_type
  // A pasted paper never had a file - nothing to fetch.
  const file = useFileUrl(kind === 'text' ? null : api.documentFilePath(doc.id))
  // A paper uploaded before its file was kept answers 404 - show the text
  // that was read from it instead. Any other failure is said as itself.
  const missing = kind === 'text' || (file.error && file.error.status === 404)
  const failed = file.error && !missing

  return (
    <div className="tr-original__body">
      <div className="tr-original__head">
        <p className="scr-h3" style={{ margin: 0 }}>Original paper</p>
        {file.url ? (
          <a className="es-caption" href={file.url} target="_blank" rel="noreferrer"
             download={kind === 'pdf' || kind === 'image' ? undefined : doc.filename}>
            {kind === 'pdf' || kind === 'image' ? 'Open in a new tab' : 'Download the file'}
          </a>
        ) : null}
      </div>

      {file.loading ? <p className="es-caption">Loading the original...</p> : null}
      {failed ? <p className="es-caption">Could not load the original: {file.error.message}</p> : null}

      {file.url && kind === 'pdf' ? (
        <iframe className="tr-original__frame" src={file.url} title={`Original paper ${doc.filename}`} />
      ) : null}

      {file.url && kind === 'image' ? (
        <img className="tr-original__img" src={file.url} alt={`Original paper ${doc.filename}`} />
      ) : null}

      {missing || (file.url && kind !== 'pdf' && kind !== 'image') ? (
        <>
          <p className="es-caption" style={{ margin: 0 }}>
            {kind === 'text'
              ? 'This paper was pasted in as text after being read on a data-entry computer, so the pasted text is shown.'
              : missing
                ? 'The original file was not kept for this paper, so the text read from it is shown.'
                : 'A Word file cannot be shown in the browser, so the text read from it is shown. Download the file to compare it in full.'}
          </p>
          <div className="tr-original__text">{doc.raw_text || 'No text was read from this paper.'}</div>
        </>
      ) : null}
    </div>
  )
}
