import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { AlignedLine, AlignedWord, AlignmentPayload } from './types'
import './App.css'

const NUDGE_SEC = 0.05
const DRAG_THRESHOLD_PX = 4
const DRAG_MS_PER_PX = 2
const DRAG_MS_PER_PX_COARSE = 20
const EDGE_HANDLE_PX = 14
const ZOOM_PAD_SEC = 2.0

type DragAnchor = 'both' | 'start' | 'end'

type DragState = {
  lineIdx: number
  anchor: DragAnchor
  startClientX: number
  msPerPx: number
  deltaSec: number
  source: 'list' | 'zoom'
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

// The exclusively-selected line, if any: returns the line index iff all
// selected word keys belong to a single fully-selected line. Used to
// decide when the zoom-loop sub-panel should appear.
function getSingleSelectedLine(
  lines: AlignedLine[],
  selected: Set<string>,
): number | null {
  if (selected.size === 0) return null
  let target: number | null = null
  for (const key of selected) {
    const m = /^W(\d+)-(\d+)$/.exec(key)
    if (!m) return null
    const li = Number(m[1])
    if (target === null) target = li
    else if (target !== li) return null
  }
  if (target === null) return null
  if (!lineFullySelected(lines[target], target, selected)) return null
  return target
}

function hitTestAnchor(relX: number, boxWidth: number): DragAnchor {
  if (relX < EDGE_HANDLE_PX) return 'start'
  if (relX > boxWidth - EDGE_HANDLE_PX) return 'end'
  return 'both'
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
  const [loopEnabled, setLoopEnabled] = useState(false)
  const [currentTime, setCurrentTime] = useState(0)
  const [editingLineIdx, setEditingLineIdx] = useState<number | null>(null)
  const [editingText, setEditingText] = useState('')
  const [editingIsNew, setEditingIsNew] = useState(false)

  const audioRef = useRef<HTMLAudioElement | null>(null)
  const suppressNextClickRef = useRef(false)
  const zoomPanelRef = useRef<HTMLDivElement | null>(null)

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

  // Shift ALL selected words by deltaSec. Used for 'both'-anchor drags
  // and the existing ±50ms buttons.
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

  // Shift one edge of a single line: 'start' moves the first word's
  // start; 'end' moves the last word's end. Interior word timings are
  // unchanged. Used by edge-drag in the main list and the zoom-loop view.
  const applyEdgeShift = useCallback(
    (lineIdx: number, anchor: 'start' | 'end', deltaSec: number) => {
      if (deltaSec === 0) return
      pushUndo()
      setLines((prev) => {
        const next = cloneLines(prev)
        const line = next[lineIdx]
        if (line.words.length === 0) return prev
        if (anchor === 'start') {
          const w = line.words[0]
          // Clamp: don't push start past end, and never below 0.
          const newStart = Math.max(0, Math.min(w.end, w.start + deltaSec))
          w.start = newStart
        } else {
          const w = line.words[line.words.length - 1]
          // Clamp: don't drag end before start.
          const newEnd = Math.max(w.start, w.end + deltaSec)
          w.end = newEnd
        }
        next[lineIdx] = recomputeLine(line)
        return next
      })
      setDirty(true)
    },
    [pushUndo],
  )

  // Reflow words evenly across [start, end] from raw text. Original
  // per-word timings are lost — the user re-drags to refine, or v2 can
  // offer "re-align this section" via WhisperX.
  const reflowWords = useCallback(
    (text: string, start: number, end: number): AlignedWord[] => {
      const tokens = text.trim().split(/\s+/).filter(Boolean)
      if (tokens.length === 0) return []
      const dur = Math.max(0, end - start)
      const step = tokens.length > 0 ? dur / tokens.length : 0
      return tokens.map((t, i) => ({
        text: t,
        start: start + i * step,
        end: start + (i + 1) * step,
      }))
    },
    [],
  )

  const beginEdit = useCallback(
    (li: number) => {
      setEditingLineIdx(li)
      setEditingText(lines[li].text)
      setEditingIsNew(false)
    },
    [lines],
  )

  const cancelEdit = useCallback(() => {
    // Canceling on a freshly-inserted line removes it.
    if (editingIsNew && editingLineIdx !== null) {
      const li = editingLineIdx
      pushUndo()
      setLines((prev) => prev.filter((_, i) => i !== li))
      setSelected(new Set())
      setDirty(true)
    }
    setEditingLineIdx(null)
    setEditingText('')
    setEditingIsNew(false)
  }, [editingIsNew, editingLineIdx, pushUndo])

  const commitEdit = useCallback(() => {
    if (editingLineIdx === null) return
    const li = editingLineIdx
    const trimmed = editingText.trim()
    pushUndo()
    if (trimmed === '') {
      // Empty save = delete.
      setLines((prev) => prev.filter((_, i) => i !== li))
      setSelected(new Set())
    } else {
      setLines((prev) => {
        const next = cloneLines(prev)
        const line = next[li]
        const newWords = reflowWords(trimmed, line.start, line.end)
        next[li] = recomputeLine({ ...line, text: trimmed, words: newWords })
        return next
      })
    }
    setDirty(true)
    setEditingLineIdx(null)
    setEditingText('')
    setEditingIsNew(false)
  }, [editingLineIdx, editingText, pushUndo, reflowWords])

  // Insert a new blank line at index `idx`. Timings are taken from the
  // gap between neighbors; when no gap exists, a 1-second window is
  // carved out after the previous line (or before the first / after the
  // last, at the list boundaries).
  const insertLineAt = useCallback(
    (idx: number) => {
      pushUndo()
      setLines((prev) => {
        const next = cloneLines(prev)
        let start: number
        let end: number
        const before = idx > 0 ? next[idx - 1] : null
        const after = idx < next.length ? next[idx] : null
        if (before && after) {
          const gap = after.start - before.end
          if (gap >= 0.4) {
            start = before.end
            end = after.start
          } else {
            // No room — carve out 1s after `before` and push the
            // following lines wouldn't be right; instead, place the new
            // line at `before.end` with a nominal 1s duration and let
            // the user drag.
            start = before.end
            end = before.end + 1.0
          }
        } else if (before) {
          start = before.end
          end = before.end + 2.0
        } else if (after) {
          end = after.start
          start = Math.max(0, end - 2.0)
        } else {
          start = 0
          end = 2.0
        }
        const blank: AlignedLine = { text: '', start, end, words: [] }
        next.splice(idx, 0, blank)
        return next
      })
      setEditingLineIdx(idx)
      setEditingText('')
      setEditingIsNew(true)
      setDirty(true)
    },
    [pushUndo],
  )

  const selectedCount = useMemo(() => selected.size, [selected])
  const singleSelectedLine = useMemo(
    () => getSingleSelectedLine(lines, selected),
    [lines, selected],
  )

  // Seek the audio element to a given time and start playback. Used by
  // the ▶ "jump to" button on each line and the zoom-loop controls.
  const seekTo = useCallback((sec: number, play = true) => {
    const a = audioRef.current
    if (!a) return
    a.currentTime = Math.max(0, sec)
    setCurrentTime(a.currentTime)
    if (play) void a.play().catch(() => {})
  }, [])

  // Playback-position highlight + zoom-loop wrap-around. Scans lines
  // for the one whose window contains the current time; if looping is
  // active and the time has passed the selected line's end (+ pad),
  // wraps back to the window start.
  const handleTimeUpdate = useCallback(() => {
    const a = audioRef.current
    if (!a) return
    const t = a.currentTime
    if (
      loopEnabled &&
      singleSelectedLine !== null &&
      lines[singleSelectedLine]
    ) {
      const line = lines[singleSelectedLine]
      const winStart = Math.max(0, line.start - ZOOM_PAD_SEC)
      const winEnd = line.end + ZOOM_PAD_SEC
      if (t >= winEnd || t < winStart) {
        a.currentTime = winStart
        setCurrentTime(winStart)
        return
      }
    }
    setCurrentTime(t)
    let found: number | null = null
    for (let i = 0; i < lines.length; i++) {
      const L = lines[i]
      if (t >= L.start && t < L.end) {
        found = i
        break
      }
    }
    setPlayingLineIdx(found)
  }, [lines, loopEnabled, singleSelectedLine])

  // Drag-to-shift (list view). Hit-tests for edge vs. body anchor.
  // Keyboard activation goes through onClick as before.
  const handleLinePointerDown = useCallback(
    (li: number, e: React.PointerEvent<HTMLButtonElement>) => {
      if (view !== 'line') return
      if (e.button !== undefined && e.button !== 0) return
      const rect = e.currentTarget.getBoundingClientRect()
      const relX = e.clientX - rect.left
      const anchor = hitTestAnchor(relX, rect.width)
      e.currentTarget.setPointerCapture(e.pointerId)
      if (!lineFullySelected(lines[li], li, selected)) {
        selectLineWords(li, e.shiftKey)
      }
      suppressNextClickRef.current = true
      setDragState({
        lineIdx: li,
        anchor,
        startClientX: e.clientX,
        msPerPx: DRAG_MS_PER_PX,
        deltaSec: 0,
        source: 'list',
      })
    },
    [view, lines, selected, selectLineWords],
  )

  const handleLinePointerMove = useCallback(
    (e: React.PointerEvent<HTMLButtonElement>) => {
      setDragState((prev) => {
        if (!prev || prev.source !== 'list') return prev
        const rawDelta = e.clientX - prev.startClientX
        if (Math.abs(rawDelta) < DRAG_THRESHOLD_PX && prev.deltaSec === 0) {
          return prev
        }
        const msPerPx = e.shiftKey ? DRAG_MS_PER_PX_COARSE : DRAG_MS_PER_PX
        return { ...prev, msPerPx, deltaSec: (rawDelta * msPerPx) / 1000 }
      })
    },
    [],
  )

  const commitDrag = useCallback(
    (d: DragState) => {
      if (d.deltaSec === 0) return
      if (d.anchor === 'both') {
        applyNudge(d.deltaSec)
      } else {
        applyEdgeShift(d.lineIdx, d.anchor, d.deltaSec)
      }
    },
    [applyNudge, applyEdgeShift],
  )

  const handleLinePointerUp = useCallback(
    (_e: React.PointerEvent<HTMLButtonElement>) => {
      if (dragState && dragState.source === 'list') {
        commitDrag(dragState)
      }
      setDragState(null)
    },
    [dragState, commitDrag],
  )

  const handleLinePointerCancel = useCallback(() => setDragState(null), [])

  // Drive playback-position highlight from requestAnimationFrame rather
  // than the browser's timeupdate event (which fires only ~4x/sec at
  // browser-chosen intervals — highlight onset jitters run-to-run). RAF
  // gives ~16ms precision. setPlayingLineIdx is a no-op when the index
  // doesn't change, so 60fps polling is cheap.
  useEffect(() => {
    const a = audioRef.current
    if (!a) return
    let raf = 0
    const tick = () => {
      handleTimeUpdate()
      raf = requestAnimationFrame(tick)
    }
    const start = () => {
      if (raf === 0) raf = requestAnimationFrame(tick)
    }
    const stop = () => {
      if (raf !== 0) {
        cancelAnimationFrame(raf)
        raf = 0
      }
    }
    a.addEventListener('play', start)
    a.addEventListener('pause', stop)
    a.addEventListener('ended', stop)
    if (!a.paused) start()
    return () => {
      stop()
      a.removeEventListener('play', start)
      a.removeEventListener('pause', stop)
      a.removeEventListener('ended', stop)
    }
  }, [handleTimeUpdate])

  // Global spacebar toggles play/pause. Ignored when focus is in a text
  // field or the audio element's own controls (so the browser's native
  // spacebar-on-focused-controls still works and we don't steal keys
  // from form inputs).
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.code !== 'Space' && e.key !== ' ') return
      const t = e.target as HTMLElement | null
      if (t) {
        const tag = t.tagName
        if (
          tag === 'INPUT' ||
          tag === 'TEXTAREA' ||
          tag === 'SELECT' ||
          tag === 'AUDIO' ||
          t.isContentEditable
        ) {
          return
        }
      }
      const a = audioRef.current
      if (!a) return
      e.preventDefault()
      if (a.paused) void a.play().catch(() => {})
      else a.pause()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  // Auto-scroll the playing line into view during playback, but never
  // during a drag (the user is interacting, don't yank the viewport).
  useEffect(() => {
    if (playingLineIdx === null || dragState !== null) return
    const el = document.querySelector(
      `[data-line-idx="${playingLineIdx}"]`,
    )
    if (el instanceof HTMLElement) {
      el.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
    }
  }, [playingLineIdx, dragState])

  // When the loop toggle flips on, seek to the window start so the
  // user hears the loop from the beginning.
  useEffect(() => {
    if (!loopEnabled || singleSelectedLine === null) return
    const line = lines[singleSelectedLine]
    if (!line) return
    const winStart = Math.max(0, line.start - ZOOM_PAD_SEC)
    seekTo(winStart, true)
    // Only re-fire when loop toggles on or the target line changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loopEnabled, singleSelectedLine])

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

  // Compute the live-preview delta for a given line index, respecting
  // anchor. 'both' shifts both edges; 'start'/'end' shift only one.
  function previewDeltaFor(li: number): {
    dStart: number
    dEnd: number
  } {
    if (!dragState) return { dStart: 0, dEnd: 0 }
    // For 'both' drags, the delta applies to every selected line; for
    // edge drags, only to the dragged line.
    if (dragState.anchor === 'both') {
      const selHere = lineFullySelected(lines[li], li, selected)
      return selHere
        ? { dStart: dragState.deltaSec, dEnd: dragState.deltaSec }
        : { dStart: 0, dEnd: 0 }
    }
    if (dragState.lineIdx !== li) return { dStart: 0, dEnd: 0 }
    return dragState.anchor === 'start'
      ? { dStart: dragState.deltaSec, dEnd: 0 }
      : { dStart: 0, dEnd: dragState.deltaSec }
  }

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
          Tip: click a line to select. Drag the middle to shift both ends;
          drag the left/right edge to move just that boundary. Shift =
          coarse. ▶ jumps to that line.
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
          onSeeked={handleTimeUpdate}
          onPause={handleTimeUpdate}
        >
          <track kind="captions" />
        </audio>
      </section>

      {singleSelectedLine !== null ? (
        <ZoomLoop
          panelRef={zoomPanelRef}
          line={lines[singleSelectedLine]}
          lineIdx={singleSelectedLine}
          currentTime={currentTime}
          loopEnabled={loopEnabled}
          setLoopEnabled={setLoopEnabled}
          seekTo={seekTo}
          setDragState={setDragState}
          commitDrag={commitDrag}
          previewDeltaFor={previewDeltaFor}
        />
      ) : null}

      <ul className="lines">
        {view === 'line' ? (
          <li className="insert-row-wrap">
            <button
              type="button"
              className="insert-row"
              onClick={() => insertLineAt(0)}
              title="Insert line at top"
            >
              +
            </button>
          </li>
        ) : null}
        {lines.map((line, li) => {
          const selectedHere = lineFullySelected(line, li, selected)
          const { dStart, dEnd } = previewDeltaFor(li)
          const displayStart = Math.max(0, line.start + dStart)
          const displayEnd = Math.max(displayStart, line.end + dEnd)
          const isPlaying = playingLineIdx === li
          const isEditing = editingLineIdx === li
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
              {view === 'line' && isEditing ? (
                <div className="line-edit">
                  <textarea
                    value={editingText}
                    autoFocus
                    onChange={(e) => setEditingText(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Escape') {
                        e.preventDefault()
                        cancelEdit()
                      } else if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault()
                        commitEdit()
                      }
                    }}
                    placeholder="Line text…"
                  />
                  <div className="edit-actions">
                    <span className="muted">
                      {line.start.toFixed(3)} → {line.end.toFixed(3)}
                    </span>
                    <button
                      type="button"
                      className="primary"
                      onClick={commitEdit}
                    >
                      Save
                    </button>
                    <button type="button" onClick={cancelEdit}>
                      Cancel
                    </button>
                  </div>
                </div>
              ) : view === 'line' ? (
                <div className="line-row">
                  <button
                    type="button"
                    className="jump-btn"
                    title={`Jump to ${line.start.toFixed(2)}s`}
                    onClick={(e) => {
                      e.preventDefault()
                      e.stopPropagation()
                      seekTo(line.start, true)
                    }}
                    onPointerDown={(e) => e.stopPropagation()}
                  >
                    ▶
                  </button>
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
                      selectLineWords(li, e.shiftKey)
                    }}
                  >
                    <span className="edge edge-l" />
                    <span className="edge edge-r" />
                    <span className="times">
                      {displayStart.toFixed(3)} → {displayEnd.toFixed(3)}
                      {dStart !== 0 || dEnd !== 0 ? (
                        <span className="drag-delta">
                          {' '}
                          ({dStart !== dEnd
                            ? dStart !== 0
                              ? `start ${dStart >= 0 ? '+' : ''}${Math.round(dStart * 1000)} ms`
                              : `end ${dEnd >= 0 ? '+' : ''}${Math.round(dEnd * 1000)} ms`
                            : `${dStart >= 0 ? '+' : ''}${Math.round(dStart * 1000)} ms`})
                        </span>
                      ) : null}
                    </span>
                    <span className="txt">{line.text}</span>
                  </button>
                  <button
                    type="button"
                    className="jump-btn edit-btn"
                    title="Edit text"
                    onClick={(e) => {
                      e.preventDefault()
                      e.stopPropagation()
                      beginEdit(li)
                    }}
                    onPointerDown={(e) => e.stopPropagation()}
                  >
                    ✎
                  </button>
                </div>
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
              {view === 'line' && !isEditing ? (
                <div className="insert-row-wrap">
                  <button
                    type="button"
                    className="insert-row"
                    onClick={() => insertLineAt(li + 1)}
                    title="Insert line below"
                  >
                    +
                  </button>
                </div>
              ) : null}
            </li>
          )
        })}
      </ul>
    </div>
  )
  // (dragActive is referenced by CSS via body-level state if desired
  // in a future iteration; currently only used to gate auto-scroll.)
  void dragActive
}

// ---------------------------------------------------------------------
// Zoom-loop sub-panel
// ---------------------------------------------------------------------

type ZoomLoopProps = {
  panelRef: React.RefObject<HTMLDivElement | null>
  line: AlignedLine
  lineIdx: number
  currentTime: number
  loopEnabled: boolean
  setLoopEnabled: (v: boolean) => void
  seekTo: (sec: number, play?: boolean) => void
  setDragState: React.Dispatch<React.SetStateAction<DragState | null>>
  commitDrag: (d: DragState) => void
  previewDeltaFor: (li: number) => { dStart: number; dEnd: number }
}

function ZoomLoop({
  panelRef,
  line,
  lineIdx,
  currentTime,
  loopEnabled,
  setLoopEnabled,
  seekTo,
  setDragState,
  commitDrag,
  previewDeltaFor,
}: ZoomLoopProps) {
  const trackRef = useRef<HTMLDivElement | null>(null)

  const winStart = Math.max(0, line.start - ZOOM_PAD_SEC)
  const winEnd = line.end + ZOOM_PAD_SEC
  const winDur = winEnd - winStart

  const { dStart, dEnd } = previewDeltaFor(lineIdx)
  const blockStart = Math.max(0, line.start + dStart)
  const blockEnd = Math.max(blockStart, line.end + dEnd)

  const pctStart = ((blockStart - winStart) / winDur) * 100
  const pctEnd = ((blockEnd - winStart) / winDur) * 100
  const pctWidth = Math.max(0.5, pctEnd - pctStart)
  const pctPlayhead = ((currentTime - winStart) / winDur) * 100
  const playheadInRange = pctPlayhead >= 0 && pctPlayhead <= 100

  const startZoomDrag = useCallback(
    (anchor: DragAnchor, e: React.PointerEvent<HTMLDivElement>) => {
      if (e.button !== undefined && e.button !== 0) return
      const rect = trackRef.current?.getBoundingClientRect()
      if (!rect) return
      const secPerPx = winDur / rect.width
      e.currentTarget.setPointerCapture(e.pointerId)
      e.stopPropagation()
      setDragState({
        lineIdx,
        anchor,
        startClientX: e.clientX,
        msPerPx: secPerPx * 1000,
        deltaSec: 0,
        source: 'zoom',
      })
    },
    [lineIdx, setDragState, winDur],
  )

  const onZoomPointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      setDragState((prev) => {
        if (!prev || prev.source !== 'zoom') return prev
        const rawDelta = e.clientX - prev.startClientX
        const msPerPx = prev.msPerPx * (e.shiftKey ? 5 : 1)
        return {
          ...prev,
          deltaSec: (rawDelta * msPerPx) / 1000,
        }
      })
    },
    [setDragState],
  )

  const onZoomPointerUp = useCallback(() => {
    setDragState((prev) => {
      if (prev && prev.source === 'zoom') commitDrag(prev)
      return null
    })
  }, [commitDrag, setDragState])

  // Time-ruler ticks every 100ms. Position by percentage so SVG/ruler
  // resizes with the panel width.
  const ticks: number[] = useMemo(() => {
    const out: number[] = []
    const startTick = Math.ceil(winStart * 10) / 10
    for (let t = startTick; t <= winEnd + 1e-9; t += 0.1) {
      out.push(Math.round(t * 1000) / 1000)
    }
    return out
  }, [winStart, winEnd])

  return (
    <section className="zoom-loop" ref={panelRef}>
      <div className="zoom-head">
        <span className="zoom-title">
          Zoom: <em>{line.text}</em>
        </span>
        <span className="zoom-times">
          {blockStart.toFixed(3)} → {blockEnd.toFixed(3)} s
        </span>
        <button
          type="button"
          onClick={() => seekTo(winStart, true)}
          title="Play from window start"
        >
          ▶ Play
        </button>
        <label className="loop-toggle">
          <input
            type="checkbox"
            checked={loopEnabled}
            onChange={(e) => setLoopEnabled(e.target.checked)}
          />
          Loop
        </label>
      </div>
      <div
        className="zoom-track"
        ref={trackRef}
        onPointerMove={onZoomPointerMove}
        onPointerUp={onZoomPointerUp}
        onPointerCancel={onZoomPointerUp}
        onClick={(e) => {
          // Click on empty track area seeks to that time.
          if (e.target !== e.currentTarget) return
          const rect = trackRef.current?.getBoundingClientRect()
          if (!rect) return
          const relX = e.clientX - rect.left
          const t = winStart + (relX / rect.width) * winDur
          seekTo(t, false)
        }}
      >
        <div className="zoom-ruler" aria-hidden="true">
          {ticks.map((t) => {
            const pct = ((t - winStart) / winDur) * 100
            const major = Math.abs(t - Math.round(t * 2) / 2) < 1e-6
            return (
              <span
                key={t}
                className={major ? 'tick major' : 'tick'}
                style={{ left: `${pct}%` }}
              >
                {major ? (
                  <span className="tick-label">{t.toFixed(1)}</span>
                ) : null}
              </span>
            )
          })}
        </div>
        <div
          className="zoom-block"
          style={{ left: `${pctStart}%`, width: `${pctWidth}%` }}
          onPointerDown={(e) => startZoomDrag('both', e)}
          title="Drag to shift both ends"
        >
          <div
            className="zoom-handle zoom-handle-l"
            onPointerDown={(e) => startZoomDrag('start', e)}
            title="Drag to move start"
          />
          <div
            className="zoom-handle zoom-handle-r"
            onPointerDown={(e) => startZoomDrag('end', e)}
            title="Drag to move end"
          />
        </div>
        {playheadInRange ? (
          <div
            className="zoom-playhead"
            style={{ left: `${pctPlayhead}%` }}
            aria-hidden="true"
          />
        ) : null}
      </div>
      <p className="muted hint zoom-hint">
        Click the track to seek. Drag the block body to shift both ends;
        drag a side handle to move just that boundary. Shift = coarse.
      </p>
    </section>
  )
}
