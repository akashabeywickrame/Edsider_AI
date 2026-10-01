// A placeholder for a data-entry page not built yet - keeps every nav
// link live (no dead 404s) while the rest of the app is built out one
// page at a time, same as /sign-in itself was before SignIn.jsx existed.
export default function ComingSoon({ title }) {
  return (
    <>
      <h1 className="scr-h2">{title}</h1>
      <p className="es-caption">This page is not built yet.</p>
    </>
  )
}
