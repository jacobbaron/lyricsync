import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { AlignedLine, AlignedWord, AlignmentPayload } from './types'
import './App.css'

const NUDGE_SEC = 0.05
const DRAG_THRESHOLD_PX = 4
const DRAG_MS_PER_PX = 2
const DRAG_MS_PER_PX_COARSE = 20

type DragAnchor = 'both' | 'start' | 'end'
type DragState = {
  lineIdx: number
  anchor: DragAnchor
  startClientX: number
  deltaSec: number
}

function cloneLines(lines: AlignedLine[]): AlignedLine[] {
  return lines.map((line) => ({
    ...line,
    words: line.words.map((w) => ({ ...w })),
  }))
}

function recomputeLine(line: AlignedLine): AlignedLine {
  if (line.words.length === 0) return line
  return {
    ...line,
    start: line.words[0].start,
    end: line.words[line.words.length - 1].end,
  }
}

function recomputeAll(lines: AlignedLine[]): AlignedLine[] {
  return lines.map(recomputeLine)
}

function wordKey(lineIdx: number, wordIdx: number): string {
  return `W${lineIdx}-${wordIdx}`
}

function lineFullySelected(
  line: AlignedLine,
  lineIdx: number,
  selected: Set<string>,
): boolean {
  if (line.words.length === 0) return false
  return line.words.every((_, wi) => selected.has(wordKey(lineIdx, wi)))
}

export default function App() {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lines, setLines] = useState<AlignedLine[]>([])
  const [meta, setMeta] = useState<Record<string, unknown> | undefined>()
  const [schemaVersion] = useState(1)
  const [view, setView] = useState<'line' | 'word'>('line')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [undoStack, setUndoStack] = useState<AlignedLine[][]>([])
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [playingLineIdx, setPlayingLineIdx] = useState<number | null>(null)
  const [dragState, setDragState] = useState<DragState | null>(null)

  const audioRef = useRef<HTMLAudioElement | null>(null)
  const suppressNextClickRef = useRef(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch('/api/alignment')
      if (!res.ok) {
        const j = await res.json().catch(() => ({}))
        const detail =
          typeof j.detail === 'string' ? j.detail : res.statusText
        throw new Error(detail || `HTTP ${res.status}`)
      }
      const data: AlignmentPayload = await res.json()
      setLines(recomputeAll(cloneLines(data.lines)))
      setMeta(data.meta)
      setUndoStack([])
      setDirty(false)
      setSelected(new Set())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const pushUndo = useCallback(() => {
    setUndoStack((prev) => [...prev, cloneLines(lines)])
  }, [lines])

  const undo = useCallback(() => {
    setUndoStack((prev) => {
      if (prev.length === 0) return prev
      const next = [...prev]
      const snap = next.pop()!
      setLines(snap)
      setDirty(true)
      return next
    })
  }, [])

  const toggleWord = useCallback((key: string, shift: boolean) => {
    setSelected((prev) => {
      if (shift) {
        const n = new Set(prev)
        if (n.has(key)) n.delete(key)
        else n.add(key)
        return n
      }
      return new Set([key])
    })
  }, [])

  const selectLineWords = useCallback(
    (lineIdx: number, shift: boolean) => {
      const keys = lines[lineIdx].words.map((_, wi) => wordKey(lineIdx, wi))
      setSelected((prev) => {
        const n = shift ? new Set(prev) : new Set<string>()
        for (const k of keys) n.add(k)
        return n
      })
    },
    [lines],
  )

  const applyNudge = useCallback(
    (deltaSec: number) => {
      if (selected.size === 0) return
      pushUndo()
      setLines((prev) => {
        const next = cloneLines(prev)
        for (let li = 0; li < next.length; li++) {
          const line = next[li]
          for (let wi = 0; wi < line.words.length; wi++) {
            if (!selected.has(wordKey(li, wi))) continue
            const w = line.words[wi]
            w.start = Math.max(0, w.start + deltaSec)
            w.end = Math.max(w.start, w.end + deltaSec)
          }
          next[li] = recomputeLine(line)
        }
        return next
      })
      setDirty(true)
    },
    [selected, pushUndo],
  )

  const selectedCount = useMemo(() => selected.size, [selected])

  // Playback-position highlight: scan lines for the one whose window
  // contains the audio's current time. Linear scan; line counts are small.
  const handleTimeUpdate = useCallback(() => {
    const a = audioRef.current
    if (!a) return
    const t = a.currentTime
    let found: number | null = null
    for (let i = 0; i < lines.length; i++) {
      const L = lines[i]
      if (t >= L.start && t < L.end) {
        found = i
        break
      }
    }
    setPlayingLineIdx(found)
  }, [lines])

  // Drag-to-shift: pointerdown selects (if not already selected) and
  // starts a drag; pointermove accumulates delta; pointerup commits via
  // the existing applyNudge. The DragAnchor field is plumbed through so
  // edge-drag ('start'/'end') is a drop-in future change.
  const handleLinePointerDown = useCallback(
    (li: number, e: React.PointerEvent<HTMLButtonElement>) => {
      if (view !== 'line') return
      if (e.button !== undefined && e.button !== 0) return // left button only
      e.currentTarget.setPointerCapture(e.pointerId)
      if (!lineFullySelected(lines[li], li, selected)) {
        selectLineWords(li, e.shiftKey)
      }
      suppressNextClickRef.current = true
      setDragState({
        lineIdx: li,
        anchor: 'both',
        startClientX: e.clientX,
        deltaSec: 0,
      })
    },
    [view, lines, selected, selectLineWords],
  )

  const handleLinePointerMove = useCallback(
    (e: React.PointerEvent<HTMLButtonElement>) => {
      setDragState((prev) => {
        if (!prev) return prev
        const rawDelta = e.clientX - prev.startClientX
        if (Math.abs(rawDelta) < DRAG_THRESHOLD_PX && prev.deltaSec === 0) {
          return prev
        }
        const msPerPx = e.shiftKey ? DRAG_MS_PER_PX_COARSE : DRAG_MS_PER_PX
        return { ...prev, deltaSec: (rawDelta * msPerPx) / 1000 }
      })
    },
    [],
  )

  const handleLinePointerUp = useCallback(
    (_e: React.PointerEvent<HTMLButtonElement>) => {
      if (dragState && dragState.deltaSec !== 0) {
        applyNudge(dragState.deltaSec)
      }
      setDragState(null)
    },
    [dragState, applyNudge],
  )

  const handleLinePointerCancel = useCallback(
    (_e: React.PointerEvent<HTMLButtonElement>) => {
      setDragState(null)
    },
    [],
  )

  // Auto-scroll the playing line into view so long songs don't require
  // manual scrolling to follow playback. `block: 'nearest'` avoids
  // scrolling when the line is already on-screen.
  useEffect(() => {
    if (playingLineIdx === null) return
    const el = document.querySelector(
      `[data-line-idx="${playingLineIdx}"]`,
    )
    if (el instanceof HTMLElement) {
      el.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
    }
  }, [playingLineIdx])

  const save = useCallback(async () => {
    setSaving(true)
    setError(null)
    try {
      const body: AlignmentPayload = {
        schema_version: schemaVersion,
        lines: recomputeAll(cloneLines(lines)),
        meta,
      }
      const res = await fetch('/api/alignment', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const j = await res.json().catch(() => ({}))
        const detail =
          typeof j.detail === 'string' ? j.detail : res.statusText
        throw new Error(detail || `HTTP ${res.status}`)
      }
      setDirty(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }, [lines, meta, schemaVersion])

  if (loading) {
    return (
      <div className="shell">
        <p className="muted">Loading alignment…</p>
      </div>
    )
  }

  const dragActive = dragState !== null
  const dragDelta = dragState?.deltaSec ?? 0

  return (
    <div className="shell">
      <header className="top">
        <h1>Timing editor</h1>
        <div className="row">
          <label>
            View{' '}
            <select
              value={view}
              onChange={(e) =>
                setView(e.target.value as 'line' | 'word')
              }
            >
              <option value="line">Line</option>
              <option value="word">Word</option>
            </select>
          </label>
          <button type="button" onClick={() => void load()}>
            Reload
          </button>
          <button
            type="button"
            onClick={undo}
            disabled={undoStack.length === 0}
          >
            Undo
          </button>
          <span className="muted">{selectedCount} selected</span>
          <button
            type="button"
            onClick={() => applyNudge(-NUDGE_SEC)}
            disabled={selectedCount === 0}
          >
            −50 ms
          </button>
          <button
            type="button"
            onClick={() => applyNudge(NUDGE_SEC)}
            disabled={selectedCount === 0}
          >
            +50 ms
          </button>
          <button
            type="button"
            className="primary"
            onClick={() => void save()}
            disabled={saving || !dirty}
          >
            {saving ? 'Saving…' : dirty ? 'Save' : 'Saved'}
          </button>
        </div>
        <p className="muted hint">
          Tip: click a line to select, drag horizontally to shift its timing
          (Shift = coarse).
        </p>
      </header>

      {error ? (
        <p className="err" role="alert">
          {error}
        </p>
      ) : null}

      <section className="transport">
        <audio
          ref={audioRef}
          controls
          src="/api/audio"
          preload="metadata"
          onTimeUpdate={handleTimeUpdate}
          onSeeked={handleTimeUpdate}
          onPause={handleTimeUpdate}
        >
          <track kind="captions" />
        </audio>
      </section>

      <ul className="lines">
        {lines.map((line, li) => {
          const selectedHere = lineFullySelected(line, li, selected)
          // Live drag preview: any selected line shifts with the current
          // drag delta until commit. The committed state is unchanged.
          const delta = dragActive && selectedHere ? dragDelta : 0
          const displayStart = Math.max(0, line.start + delta)
          const displayEnd = Math.max(displayStart, line.end + delta)
          const isPlaying = playingLineIdx === li
          const classes = [
            'line-hit',
            selectedHere ? 'sel' : '',
            isPlaying ? 'playing' : '',
          ]
            .filter(Boolean)
            .join(' ')
          return (
            <li
              key={li}
              className="line-block"
              data-line-idx={li}
            >
              {view === 'line' ? (
                <button
                  type="button"
                  className={classes}
                  onPointerDown={(e) => handleLinePointerDown(li, e)}
                  onPointerMove={handleLinePointerMove}
                  onPointerUp={handleLinePointerUp}
                  onPointerCancel={handleLinePointerCancel}
                  onClick={(e) => {
                    if (suppressNextClickRef.current) {
                      suppressNextClickRef.current = false
                      e.preventDefault()
                      return
                    }
                    // Keyboard activation path (Enter/Space) — pointerdown
                    // didn't fire, so select here.
                    selectLineWords(li, e.shiftKey)
                  }}
                >
                  <span className="times">
                    {displayStart.toFixed(3)} → {displayEnd.toFixed(3)}
                    {delta !== 0 ? (
                      <span className="drag-delta">
                        {' '}
                        ({delta >= 0 ? '+' : ''}
                        {Math.round(delta * 1000)} ms)
                      </span>
                    ) : null}
                  </span>
                  <span className="txt">{line.text}</span>
                </button>
              ) : (
                <div
                  className={
                    isPlaying ? 'word-row playing' : 'word-row'
                  }
                >
                  <span className="line-label">{line.text}</span>
                  <div className="words">
                    {line.words.map((w: AlignedWord, wi: number) => {
                      const k = wordKey(li, wi)
                      const on = selected.has(k)
                      return (
                        <button
                          key={wi}
                          type="button"
                          className={on ? 'word sel' : 'word'}
                          onClick={(e) => {
                            e.preventDefault()
                            toggleWord(k, e.shiftKey)
                          }}
                        >
                          <span className="times">
                            {w.start.toFixed(2)}–{w.end.toFixed(2)}
                          </span>
                          {w.text}
                        </button>
                      )
                    })}
                  </div>
                </div>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
