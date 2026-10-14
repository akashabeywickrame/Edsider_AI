import { useEffect, useState } from 'react'
import { fetchFile } from './api.js'

// A signed-in fetch of a file (see api.js fetchFile), handed back as an
// object URL an <iframe> or <img> can show - pointing them straight at the
// API's address cannot carry the sign-in token. The URL is released when
// the component goes away or the path changes, so open papers do not pile
// up in memory. `path` null fetches nothing.
export function useFileUrl(path) {
  const [state, setState] = useState({ url: null, error: null, loading: Boolean(path) })
  useEffect(() => {
    if (!path) {
      setState({ url: null, error: null, loading: false })
      return undefined
    }
    let live = true
    let url = null
    setState({ url: null, error: null, loading: true })
    fetchFile(path)
      .then((blob) => {
        url = URL.createObjectURL(blob)
        if (live) setState({ url, error: null, loading: false })
        else URL.revokeObjectURL(url)
      })
      .catch((error) => {
        if (live) setState({ url: null, error, loading: false })
      })
    return () => {
      live = false
      if (url) URL.revokeObjectURL(url)
    }
  }, [path])
  return state
}

// <img> for an attached picture, fetched signed-in. Renders nothing until
// it arrives (and nothing if it cannot be fetched), as a broken <img> did.
export function AuthImg({ path, ...props }) {
  const { url } = useFileUrl(path)
  return url ? <img src={url} {...props} /> : null
}
