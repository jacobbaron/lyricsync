"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

// Music bed picker + lyric captions for a cut (issue #160).
//   • Bed: choose a project song, song_start (blank = lip-sync to the first
//     clip using the song's automatic sync), gain and fades →
//     POST /api/stories/[id]/music, which re-renders.
//   • Lyrics: align a lyric sheet against the bed song
//     (POST /api/songs/[id]/align-lyrics, polled), then put the lines on the
//     cut in one step (POST /api/stories/[id]/lyric-captions).

type Song = { id: string; filename: string | null; status: string };

type Bed = {
  song_id: string;
  song_start: number;
  gain_db?: number;
  fade_in_s?: number;
  fade_out_s?: number;
};

type LyricAlignment = {
  id: string;
  status: "aligning" | "ready" | "error";
  result: { lines?: unknown[]; coverage?: number; mean_score?: number } | null;
  error: string | null;
};

const POLL_MS = 4000;

const inputCls =
  "w-full rounded-lg border border-zinc-300 bg-white px-2 py-1.5 text-sm dark:border-zinc-700 dark:bg-zinc-900";

function numOrUndef(v: string): number | undefined {
  if (v.trim() === "") return undefined;
  const n = Number(v);
  return Number.isFinite(n) ? n : undefined;
}

export function MusicBed({
  storyId,
  projectId,
  initialBed,
}: {
  storyId: string;
  projectId: string;
  initialBed: Bed | null;
}) {
  const router = useRouter();
  const [songs, setSongs] = useState<Song[]>([]);
  const [songId, setSongId] = useState(initialBed?.song_id ?? "");
  const [songStart, setSongStart] = useState(
    initialBed ? String(initialBed.song_start) : "",
  );
  const [gain, setGain] = useState(String(initialBed?.gain_db ?? 0));
  const [fadeIn, setFadeIn] = useState(String(initialBed?.fade_in_s ?? 0));
  const [fadeOut, setFadeOut] = useState(String(initialBed?.fade_out_s ?? 0));
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const [lyrics, setLyrics] = useState("");
  const [alignment, setAlignment] = useState<LyricAlignment | null>(null);
  const [captioning, setCaptioning] = useState(false);

  const bedSongId = initialBed?.song_id ?? null;

  useEffect(() => {
    fetch(`/api/projects/${projectId}/songs`)
      .then((r) => (r.ok ? r.json() : { songs: [] }))
      .then((b: { songs: Song[] }) =>
        setSongs(b.songs.filter((s) => s.status === "ready")),
      )
      .catch(() => {});
  }, [projectId]);

  // Latest lyric alignment of the bed song; poll while it's running.
  const loadAlignment = useCallback(async () => {
    if (!bedSongId) return;
    const r = await fetch(`/api/songs/${bedSongId}/align-lyrics`).catch(() => null);
    if (!r?.ok) return;
    const b = (await r.json()) as { alignments: LyricAlignment[] };
    setAlignment(b.alignments[0] ?? null);
  }, [bedSongId]);

  useEffect(() => {
    loadAlignment();
  }, [loadAlignment]);

  useEffect(() => {
    if (alignment?.status !== "aligning") return;
    const t = setInterval(loadAlignment, POLL_MS);
    return () => clearInterval(t);
  }, [alignment?.status, loadAlignment]);

  async function saveBed(clear = false) {
    setSaving(true);
    setMsg(null);
    const body = clear
      ? { song_id: null }
      : {
          song_id: songId,
          song_start: numOrUndef(songStart),
          gain_db: numOrUndef(gain),
          fade_in_s: numOrUndef(fadeIn),
          fade_out_s: numOrUndef(fadeOut),
        };
    try {
      const r = await fetch(`/api/stories/${storyId}/music`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const b = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(b.error ?? `Server error ${r.status}`);
      if (b.music?.song_start != null) setSongStart(String(b.music.song_start));
      setMsg({
        ok: true,
        text: clear
          ? "Bed removed — re-rendering."
          : b.song_start_source === "first_clip"
            ? `Bed set, lip-synced to the first clip (song_start ${b.music.song_start}s) — re-rendering.`
            : "Bed set — re-rendering.",
      });
      router.refresh();
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Failed" });
    } finally {
      setSaving(false);
    }
  }

  async function alignLyrics() {
    if (!bedSongId || !lyrics.trim()) return;
    setMsg(null);
    const r = await fetch(`/api/songs/${bedSongId}/align-lyrics`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lyrics }),
    }).catch(() => null);
    if (!r?.ok) {
      const b = await r?.json().catch(() => ({}));
      setMsg({ ok: false, text: b?.error ?? "Failed to start lyric alignment" });
      return;
    }
    await loadAlignment();
  }

  async function addCaptions() {
    if (!alignment) return;
    setCaptioning(true);
    setMsg(null);
    try {
      const r = await fetch(`/api/stories/${storyId}/lyric-captions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ alignment_id: alignment.id }),
      });
      const b = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(b.error ?? b.detail ?? `Server error ${r.status}`);
      setMsg({ ok: true, text: `Added ${b.added} caption(s) — re-rendering.` });
      router.refresh();
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Failed" });
    } finally {
      setCaptioning(false);
    }
  }

  if (songs.length === 0 && !initialBed) return null;

  return (
    <section className="flex flex-col gap-3 rounded-xl border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
      <h2 className="text-sm font-semibold text-zinc-800 dark:text-zinc-200">
        Music bed
      </h2>

      <label className="flex flex-col gap-1 text-xs text-zinc-500">
        Song
        <select
          value={songId}
          onChange={(e) => setSongId(e.target.value)}
          className={inputCls}
        >
          <option value="">Choose a song…</option>
          {songs.map((s) => (
            <option key={s.id} value={s.id}>
              {s.filename ?? s.id.slice(0, 8)}
            </option>
          ))}
        </select>
      </label>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <label className="flex flex-col gap-1 text-xs text-zinc-500">
          Song start (s)
          <input
            value={songStart}
            onChange={(e) => setSongStart(e.target.value)}
            placeholder="auto"
            inputMode="decimal"
            className={inputCls}
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-zinc-500">
          Gain (dB)
          <input value={gain} onChange={(e) => setGain(e.target.value)} inputMode="decimal" className={inputCls} />
        </label>
        <label className="flex flex-col gap-1 text-xs text-zinc-500">
          Fade in (s)
          <input value={fadeIn} onChange={(e) => setFadeIn(e.target.value)} inputMode="decimal" className={inputCls} />
        </label>
        <label className="flex flex-col gap-1 text-xs text-zinc-500">
          Fade out (s)
          <input value={fadeOut} onChange={(e) => setFadeOut(e.target.value)} inputMode="decimal" className={inputCls} />
        </label>
      </div>
      <p className="text-xs text-zinc-400">
        Leave song start blank to lip-sync the song to the cut&apos;s first clip.
      </p>

      <div className="flex gap-3">
        <button
          onClick={() => saveBed(false)}
          disabled={saving || !songId}
          className="rounded-lg bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-zinc-900"
        >
          {saving ? "Saving…" : initialBed ? "Update bed" : "Set bed"}
        </button>
        {initialBed && (
          <button
            onClick={() => saveBed(true)}
            disabled={saving}
            className="text-sm text-zinc-500 hover:text-red-500 disabled:opacity-50"
          >
            Remove bed
          </button>
        )}
      </div>

      {bedSongId && (
        <div className="flex flex-col gap-2 border-t border-zinc-200 pt-3 dark:border-zinc-800">
          <h3 className="text-xs font-semibold text-zinc-700 dark:text-zinc-300">
            Lyric captions
          </h3>
          <textarea
            value={lyrics}
            onChange={(e) => setLyrics(e.target.value)}
            rows={5}
            placeholder={"Paste the lyrics as performed, one caption per line.\n[Chorus] headings and blank lines are ignored."}
            className={inputCls}
          />
          <div className="flex flex-wrap items-center gap-3">
            <button
              onClick={alignLyrics}
              disabled={!lyrics.trim() || alignment?.status === "aligning"}
              className="rounded-lg border border-zinc-300 px-3 py-1.5 text-sm disabled:opacity-50 dark:border-zinc-700"
            >
              {alignment?.status === "aligning" ? "Aligning…" : "Align to song"}
            </button>
            {alignment?.status === "ready" && (
              <>
                <span className="text-xs text-zinc-500">
                  {alignment.result?.lines?.length ?? 0} lines timed
                  {alignment.result?.coverage != null &&
                    ` · ${Math.round(alignment.result.coverage * 100)}% coverage`}
                </span>
                <button
                  onClick={addCaptions}
                  disabled={captioning}
                  className="rounded-lg bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-zinc-900"
                >
                  {captioning ? "Adding…" : "Add captions to cut"}
                </button>
              </>
            )}
            {alignment?.status === "error" && (
              <span className="text-xs text-red-500">{alignment.error}</span>
            )}
          </div>
        </div>
      )}

      {msg && (
        <p className={`text-xs ${msg.ok ? "text-green-600 dark:text-green-400" : "text-red-500"}`}>
          {msg.text}
        </p>
      )}
    </section>
  );
}
