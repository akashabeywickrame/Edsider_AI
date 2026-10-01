import React from 'react'
import ReactDOM from 'react-dom/client'
import './index.css'
import App from './App.jsx'

// bundle.js (the EdSider AI design system's component library - see
// public/bundle.js, copied from design-system/) is a plain script, not
// an ES module: it reads `window.React` once, at load time, and
// attaches its components to `window.EdSider`. It has to see the SAME
// React this app renders with - not a second copy from a CDN - or
// nothing it creates could ever be mounted into this app's tree. So
// React is published to `window` here, first, before bundle.js is
// ever requested.
window.React = React
window.ReactDOM = ReactDOM

function loadEdSiderBundle() {
  return new Promise((resolve, reject) => {
    const script = document.createElement('script')
    script.src = '/bundle.js'
    script.onload = resolve
    script.onerror = () => reject(new Error('Could not load /bundle.js'))
    document.head.appendChild(script)
  })
}

loadEdSiderBundle()
  .then(() => {
    ReactDOM.createRoot(document.getElementById('root')).render(
      <React.StrictMode>
        <App />
      </React.StrictMode>,
    )
  })
  .catch((err) => {
    document.getElementById('root').textContent =
      'Could not load the EdSider AI design system: ' + err.message
    console.error(err)
  })
