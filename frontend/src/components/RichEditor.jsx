import { useEffect, useRef } from 'react'
import Quill from 'quill'
import 'quill/dist/quill.core.css'
import { deltaToPlainText } from '../lib/richText.js'

// A real rich-text editor (Quill, the same library app.py's own question
// editor already uses - see CLAUDE.md) for correcting OCR-damaged
// question/answer text, not a hand-rolled textarea.
//
// intake_questions.text is a plain string column, never HTML, so every
// format here is preserved by converting the formatted run into real
// Unicode characters (richText.js) rather than kept as markup or
// discarded - what is visible in the editor is what gets saved, letter
// for letter. Bold/italic only cover the Latin alphabet and digits
// (Unicode has no bold/italic Sinhala or Tamil); underline/strikethrough
// use a combining mark instead, which works on any script.
//
// Uncontrolled after mount, like a textarea's defaultValue - Quill owns
// its own editing state; `initialValue` only seeds it once. A caller
// that needs a hard reset (after a re-split regenerates every question
// with a new id) already gets one for free, since PaperIntake.jsx keys
// each question card on q.id and a new id mounts a fresh instance.
// `onChange`, if given, mirrors the live plain text on every keystroke -
// for a caller (an MCQ's stem) that another field's own save needs to
// read before this one next blurs, not for driving the editor itself.
export default function RichEditor({ initialValue, onChange, onBlur, ariaLabel, placeholder, minHeight = 84 }) {
  const toolbarRef = useRef(null)
  const editorRef = useRef(null)
  const quillRef = useRef(null)
  // Always the latest callbacks, read by listeners bound once at mount -
  // without this a stale closure would keep comparing against the
  // onBlur captured on the FIRST render, the same bug class the plain-
  // textarea toolbar had (see RichEditor's sibling, now removed).
  const onBlurRef = useRef(onBlur)
  onBlurRef.current = onBlur
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange

  useEffect(() => {
    const quill = new Quill(editorRef.current, {
      modules: { toolbar: { container: toolbarRef.current } },
      placeholder,
    })
    quill.setText(initialValue || '')
    quillRef.current = quill
    quill.root.setAttribute('aria-label', ariaLabel || '')

    function handleBlur() {
      onBlurRef.current(deltaToPlainText(quill.getContents()))
    }
    quill.root.addEventListener('blur', handleBlur)

    function handleTextChange() {
      if (onChangeRef.current) onChangeRef.current(deltaToPlainText(quill.getContents()))
    }
    quill.on('text-change', handleTextChange)

    return () => {
      quill.root.removeEventListener('blur', handleBlur)
      quill.off('text-change', handleTextChange)
      quillRef.current = null
    }
    // Mount-once: initialValue/placeholder/ariaLabel only ever seed the
    // editor, they do not drive it afterwards (see the note above).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="pi-rte">
      <div className="pi-rte__bar" ref={toolbarRef} role="toolbar" aria-label="Formatting">
        {/* Quill's own toolbar module only preventDefault()s on `click`
            (see node_modules/quill/modules/toolbar.js), not `mousedown` -
            so a plain <button> still steals focus and blurs the editor
            BEFORE its click handler applies the format, saving the
            pre-format text. Every format here changes what gets saved
            (see richText.js), so every button needs the guard - this is
            exactly the bug the first, plain-textarea toolbar had. */}
        <button type="button" className="ql-bold" aria-label="Bold" onMouseDown={(e) => e.preventDefault()}>
          <b>B</b>
        </button>
        <button type="button" className="ql-italic" aria-label="Italic" onMouseDown={(e) => e.preventDefault()}>
          <i>I</i>
        </button>
        <button
          type="button"
          className="ql-underline"
          aria-label="Underline"
          onMouseDown={(e) => e.preventDefault()}
        >
          <u>U</u>
        </button>
        <button type="button" className="ql-strike" aria-label="Strikethrough" onMouseDown={(e) => e.preventDefault()}>
          <s>S</s>
        </button>
        <span className="pi-rte__sep" />
        <button
          type="button"
          className="ql-script"
          value="sub"
          aria-label="Subscript"
          onMouseDown={(e) => e.preventDefault()}
        >
          x<sub>2</sub>
        </button>
        <button
          type="button"
          className="ql-script"
          value="super"
          aria-label="Superscript"
          onMouseDown={(e) => e.preventDefault()}
        >
          x<sup>2</sup>
        </button>
        <span className="pi-rte__sep" />
        <button type="button" className="ql-clean" aria-label="Clear formatting" onMouseDown={(e) => e.preventDefault()}>
          Clear
        </button>
        <span className="pi-rte__sep" />
        <button
          type="button"
          aria-label="Undo"
          onMouseDown={(e) => e.preventDefault()}
          onClick={() => quillRef.current?.history.undo()}
        >
          &#8630;
        </button>
        <button
          type="button"
          aria-label="Redo"
          onMouseDown={(e) => e.preventDefault()}
          onClick={() => quillRef.current?.history.redo()}
        >
          &#8631;
        </button>
      </div>
      <div ref={editorRef} className="pi-rte__area" style={{ '--pi-rte-min-h': `${minHeight}px` }} />
    </div>
  )
}
