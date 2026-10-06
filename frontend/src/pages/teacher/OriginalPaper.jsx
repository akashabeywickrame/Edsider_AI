import { useEffect, useState } from 'react'
import { api } from '../../lib/api.js'

// The paper as it was uploaded, shown beside the questions so the teacher can
// compare each one with the original. A PDF and a picture are shown as they
// are; a Word file cannot be shown in a browser, so the text that was read
// from it is shown instead, with a download link for the file itself.

export default function OriginalPaper({ doc }) {
  const [state, setState] = useState({ status: 'checking' })
  const url = api.documentFileUrl(doc.id)

  useEffect(() => {
    let live = true
    setState({ status: 'checking' })
    // A paper uploaded before its file was kept has no file to show - fall back
    // to the text that was read from it.
    // The file route only answers GET, so this asks for it and drops the body
    // at once - it is only the status that decides whether to show it.
    fetch(url)
      .then((res) => {
        res.body?.cancel()
        if (live) setState({ status: res.ok ? 'file' : 'missing' })
      })
      .catch(() => {
        if (live) setState({ status: 'missing' })
      })
    return () => {
      live = false
    }
  }, [url])

  const kind = doc.file_type

  return (
    <div className="tr-original__body">
      <div className="tr-original__head">
        <p className="scr-h3" style={{ margin: 0 }}>Original paper</p>
        {state.status === 'file' ? (
          <a className="es-caption" href={url} target="_blank" rel="noreferrer">
            Open in a new tab
          </a>
        ) : null}
      </div>

      {state.status === 'checking' ? <p className="es-caption">Loading the original...</p> : null}

      {state.status === 'file' && kind === 'pdf' ? (
        <iframe className="tr-original__frame" src={url} title={`Original paper ${doc.filename}`} />
      ) : null}

      {state.status === 'file' && kind === 'image' ? (
        <img className="tr-original__img" src={url} alt={`Original paper ${doc.filename}`} />
      ) : null}

      {(state.status === 'missing' || (state.status === 'file' && kind !== 'pdf' && kind !== 'image')) ? (
        <>
          <p className="es-caption" style={{ margin: 0 }}>
            {state.status === 'missing'
              ? 'The original file was not kept for this paper, so the text read from it is shown.'
              : 'A Word file cannot be shown in the browser, so the text read from it is shown. Open the file to compare it in full.'}
          </p>
          <div className="tr-original__text">{doc.raw_text || 'No text was read from this paper.'}</div>
        </>
      ) : null}
    </div>
  )
}
