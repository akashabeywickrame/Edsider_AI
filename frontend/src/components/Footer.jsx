import './Footer.css'

// Shared across every page - the same footer appeared on both Landing
// and SignIn's own design mockups, so it is one component rather than
// two copies of the same markup.
export default function Footer() {
  return (
    <footer className="app-foot">
      <span>
        Powered by <a href="https://www.infyinnos.com/">Infinity Innovators (Pvt) Ltd</a>
      </span>
      <span className="app-foot__brand">
        <img src="/edsider-logo.png" alt="EdSider" />
        <small>&copy; 2026 EdSider. All rights reserved.</small>
      </span>
      <span className="app-foot__right">
        <a href="mailto:info@edsider.com">info@edsider.com</a>
      </span>
    </footer>
  )
}
