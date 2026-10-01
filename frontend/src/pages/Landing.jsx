import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import Footer from '../components/Footer.jsx'
import './Landing.css'

const STEPS = [
  {
    icon: 'upload',
    accent: 'blue',
    num: 'STEP 1',
    title: 'Upload a paper',
    body: 'Add a typed or scanned question paper. Its questions are read and laid out ready to check.',
  },
  {
    icon: 'search',
    accent: 'primary',
    num: 'STEP 2',
    title: 'Spot repeats',
    body: 'Questions that are already in use - even when they are worded differently - are pointed out, with a clear reason.',
  },
  {
    icon: 'check',
    accent: 'coral',
    num: 'STEP 3',
    title: 'Review and publish',
    body: 'A teacher reviews every question before the paper reaches students. Nothing is changed without a person deciding.',
  },
]

const ROLES = [
  {
    role: 'data',
    icon: 'upload',
    accent: 'blue',
    title: 'Data entry',
    body: 'Upload papers, correct the questions, add any diagrams or pictures, and make sure nothing is repeated before it goes for review.',
    cta: 'Sign in as data entry',
  },
  {
    role: 'teacher',
    icon: 'check',
    accent: 'primary',
    title: 'Teachers',
    body: 'Review the papers assigned to you question by question, comment where something is wrong, and finish the review.',
    cta: 'Sign in as a teacher',
  },
  {
    role: 'admin',
    icon: 'users',
    accent: 'gold',
    title: 'Administrators',
    body: 'Hand checked papers to teachers and follow each one through review until it is published.',
    cta: 'Sign in as an administrator',
  },
]

const SECTIONS = [
  { id: 'top', label: 'Home' },
  { id: 'how', label: 'How it works' },
  { id: 'who', label: 'Who it is for' },
]

function useActiveSection(ids) {
  const [active, setActive] = useState(ids[0])

  useEffect(() => {
    if (!('IntersectionObserver' in window)) return
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries.find((e) => e.isIntersecting)
        if (visible) setActive(visible.target.id)
      },
      // A section counts as "current" once it has passed the top 40%
      // of the viewport and before it reaches the bottom 55% - a plain
      // "is it on screen at all" test flips between two sections while
      // scrolling past the boundary between them.
      { rootMargin: '-40% 0px -55% 0px' },
    )
    const elements = ids.map((id) => document.getElementById(id)).filter(Boolean)
    elements.forEach((el) => observer.observe(el))
    return () => observer.disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return active
}

export default function Landing() {
  // Read here, at render time, not at module load time: main.jsx only
  // renders this component after bundle.js has finished loading and
  // attached window.EdSider - but a static top-of-file destructure
  // would run while the module graph is still being evaluated, before
  // that load has happened.
  const { Icon, LanguagePill } = window.EdSider
  const active = useActiveSection(SECTIONS.map((s) => s.id))

  return (
    <>
      <header className="ld-nav">
        <a className="ld-nav__logo" href="#top" aria-label="EdSider AI home">
          <img src="/edsider-ai-logo.png" alt="EdSider AI" />
        </a>
        <nav className="ld-nav__links" aria-label="Sections">
          {SECTIONS.map((s) => (
            <a key={s.id} href={`#${s.id}`} aria-current={active === s.id ? 'page' : undefined}>
              {s.label}
            </a>
          ))}
          <a href="https://www.edsider.com/">EdSider</a>
        </nav>
        <Link className="ld-cta ld-cta--primary ld-cta--sm" to="/sign-in">
          Sign In
        </Link>
      </header>

      <section id="top" className="ld-sec ld-hero">
        <div>
          <span className="ld-dot" aria-hidden="true" />
          <h1 className="ld-h1">
            <span className="accent">Check Faster.</span>
            <span>Catch Every Repeat.</span>
            <span>Publish With Confidence.</span>
          </h1>
          <p className="ld-lede">
            EdSider AI helps the EdSider team prepare exam papers. It reads uploaded
            papers, spots questions students have already seen, and leaves every
            final decision to a teacher - so every paper students practise with is
            fresh and correct.
          </p>
          <div className="ld-langs">
            <span className="ld-langs__label">Works with papers in</span>
            <LanguagePill lang="si" />
            <LanguagePill lang="ta" />
            <LanguagePill lang="en" />
          </div>
          <div className="ld-ctas">
            <Link className="ld-cta ld-cta--primary" to="/sign-in">
              Sign In to Start <span aria-hidden="true">→</span>
            </Link>
            <a className="ld-cta ld-cta--outline" href="#how">
              See how it works
            </a>
          </div>
        </div>

        <div className="ld-hero__art">
          <img src="/hero.png" alt="Two teachers in a classroom with books" />
        </div>
      </section>

      <section id="how" className="ld-sec ld-how">
        <h2>How it works</h2>
        <p>Three simple steps from a finished paper to one students can practise with.</p>
        <div className="ld-grid3">
          {STEPS.map((s) => (
            <article className="ld-card" key={s.title}>
              <span className={`es-stat__icon es-accent--${s.accent}`}>
                <Icon name={s.icon} />
              </span>
              <span className="ld-num">{s.num}</span>
              <h3 className="ld-h3">{s.title}</h3>
              <p className="ld-p">{s.body}</p>
            </article>
          ))}
        </div>
      </section>

      <section id="who" className="ld-sec ld-who">
        <h2>Who it is for</h2>
        <p>One sign-in, three roles. Each person sees only the tools for their part of the work.</p>
        <div className="ld-grid3">
          {ROLES.map((r) => (
            <article className="ld-card" key={r.title}>
              <span className={`es-stat__icon es-accent--${r.accent}`}>
                <Icon name={r.icon} />
              </span>
              <h3 className="ld-h3">{r.title}</h3>
              <p className="ld-p">{r.body}</p>
              <div>
                <Link className="ld-cta ld-cta--outline ld-cta--sm" to={`/sign-in?role=${r.role}`}>
                  {r.cta}
                </Link>
              </div>
            </article>
          ))}
        </div>
      </section>

      <Footer />
    </>
  )
}
