import { api } from '../lib/api.js'
import { AuthImg } from '../lib/useFileUrl.jsx'
import './Pictures.css'

// A question's pictures. When the paper names them ("A", "B", "C" printed
// under three ploughs), they sit side by side with each label under its
// picture, as on the paper; otherwise one under another, as before.
export default function Pictures({ images, className }) {
  if (!images || images.length === 0) return null
  if (!images.some((im) => im.label)) {
    return images.map((im) => (
      <AuthImg key={im.id} className={className} path={api.imagePath(im.id)} alt="" />
    ))
  }
  return (
    <div className="pics-row">
      {images.map((im) => (
        <figure className="pics-row__item" key={im.id}>
          <AuthImg className={className} path={api.imagePath(im.id)}
            alt={im.label ? `Picture ${im.label}` : ''} />
          {im.label ? <figcaption className="pics-row__label">{im.label}</figcaption> : null}
        </figure>
      ))}
    </div>
  )
}
