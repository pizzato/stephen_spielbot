import { useEffect, useId, useRef, useState } from 'react'
import { Banner, Button } from '../components.jsx'

export default function FaceRepairPicker({ options, onClose, onApply }) {
  const { characters, frame_url: frameUrl } = options
  const titleId = useId()
  const hintId = useId()
  const characterId = useId()
  const dialogRef = useRef(null)
  const [character, setCharacter] = useState(characters[0])
  const [targets, setTargets] = useState([])
  const [frameError, setFrameError] = useState(!frameUrl)
  const point = targets.find((target) => target.character === character)

  useEffect(() => {
    const dialog = dialogRef.current
    dialog.showModal()
    return () => dialog.close()
  }, [])

  const mark = (x, y) => {
    const clamp = (n) => Math.min(1, Math.max(0, n))
    setTargets((old) => [...old.filter((t) => t.character !== character), { character, x: clamp(x), y: clamp(y) }])
  }
  const handleClick = (e) => {
    if (!e.detail) { mark(point?.x ?? .5, point?.y ?? .5); return }
    const rect = e.currentTarget.getBoundingClientRect()
    mark((e.clientX - rect.left) / rect.width, (e.clientY - rect.top) / rect.height)
  }
  const nudge = (e) => {
    const move = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[e.key]
    if (!move) return
    e.preventDefault()
    mark((point?.x ?? .5) + move[0] * .01, (point?.y ?? .5) + move[1] * .01)
  }

  return (
    <dialog ref={dialogRef} className="face-picker" aria-labelledby={titleId} onCancel={onClose}>
      <h2 id={titleId} className="h-title">Choose faces to repair</h2>
      <p className="muted" id={hintId}>
        Choose a character and click the centre of their face in the opening frame.
        Repeat for each face you want to repair. You can also focus the frame and use the arrow keys to position a marker.
      </p>
      <div className="row center gap-10 row--wrap">
        <label htmlFor={characterId}>Character</label>
        <select id={characterId} className="select" style={{ width: 'auto' }} value={character} onChange={(e) => setCharacter(e.target.value)}>
          {characters.map((name, i) => <option key={name} value={name}>{i + 1}. {name}{targets.some((t) => t.character === name) ? ' (marked)' : ''}</option>)}
        </select>
        <Button size="sm" disabled={!point} onClick={() => setTargets(targets.filter((t) => t.character !== character))}>Clear marker</Button>
      </div>
      {frameError ? (
        <Banner tone="danger">Could not display the opening frame. Close and reopen Repair face to reload the take.</Banner>
      ) : (
        <div className="face-picker__preview">
          <button className="face-picker__frame" type="button" aria-label={`Mark ${character} in the opening frame`}
            aria-describedby={hintId} onClick={handleClick} onKeyDown={nudge}>
            <img src={frameUrl} alt="Opening frame of the selected video take" draggable={false} onError={() => setFrameError(true)} />
            {targets.map((t) => (
              <span key={t.character} className="face-picker__marker" title={t.character}
                style={{ left: `${t.x * 100}%`, top: `${t.y * 100}%` }}>{characters.indexOf(t.character) + 1}</span>
            ))}
          </button>
        </div>
      )}
      <div className="row center gap-10 row--wrap" style={{ justifyContent: 'flex-end' }}>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="primary" disabled={!targets.length || frameError} onClick={() => onApply(targets)}>
          {targets.length > 1 ? `Repair ${targets.length} faces` : 'Repair face'}
        </Button>
      </div>
    </dialog>
  )
}
