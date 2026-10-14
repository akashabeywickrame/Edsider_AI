import { useEffect, useState } from 'react'
import { api } from './api.js'

// Fetched once per app load by every top-level Layout (data-entry,
// teacher, admin) through RequireUser - a 403 here is the real "check
// the signed-in email against the allowlist" gate (see service.py's
// /me and current_identity()), not something re-checked route by
// route. Locally, with no Easy Auth header at all, /me falls back to
// {identity: "reviewer", role: null} rather than erroring - the same
// "everything is open on a local dev server" rule the rest of the app
// already follows.
export function useMe() {
  const [state, setState] = useState({ me: null, loading: true, error: null })
  useEffect(() => {
    let live = true
    api
      .me()
      .then((me) => {
        if (live) setState({ me, loading: false, error: null })
      })
      .catch((err) => {
        if (live) setState({ me: null, loading: false, error: err })
      })
    return () => {
      live = false
    }
  }, [])
  return state
}
