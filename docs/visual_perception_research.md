# Visual perception — research notes (techniques beyond the Gemini VLM)

Scope: ways to extract visual information from source footage *beyond* the single
Gemini whole-clip pass, oriented at the long-term goal (raw footage in →
professional short-form edit out). Grounded in the actual library: DIY/maker
process content (acoustic panels, Home Depot runs, the dumper, audio-engineering
screens) plus talking-to-camera and one vocal/music project.

This is a survey + prioritized recommendation, not an implementation. It assumes
the roadmap in `llm_editor_roadmap.md` (Track 1 Perception) and extends it.

---

## The framing that matters: augment the VLM, don't replace it

Gemini is already *good* at naming things and reading a scene ("a woman crouches
measuring a plank, smiles up at the camera"). A specialized model that re-derives
that is wasted effort. The wins come from what a frame-sampling VLM is
structurally **bad** at:

1. **Spatial** — *where* is the subject/object in frame, how big, tracked over
   time. (Needed for reframing/zoom; a VLM gives prose, not boxes.)
2. **Temporal / motion** — the VLM samples sparse frames and largely misses
   *action* and *camera movement*. For process content ("the saw cut", "the
   whip-pan", "the push-in on the reveal") this is the core of the edit.
3. **Deterministic / exhaustive / cheap** — every frame, every clip, reproducibly,
   for pennies. Quality gates, dedup, retrieval, cut-snapping want a number on
   every frame, not a paragraph on every clip.
4. **Grading its own confidence** — "is this shot in focus / shaky / blown out"
   is something VLMs answer unreliably and a 5-line OpenCV function answers
   exactly.

So the two payoffs from every technique below are: (a) use the signal **directly**
in editor logic (gate, pick, reframe, snap), and (b) feed the signal **back into
Gemini as grounding** so it reasons over structured facts instead of guessing —
often a bigger quality jump than a bigger VLM. (See "Getting the LLM to see more".)

Architecture fit: each is a new Modal function writing a JSON sidecar to R2 next
to `transcript.json` / `audio_analysis.json` (the roadmap's `faces.json` pattern),
merged into the per-clip perception document and exposed via the API. Run on the
480p proxy (roadmap 4.3), not the 4K original.

---

## Techniques, by the gap they fill

### A. Technical quality / usability QC  *(cheapest, highest autonomy leverage)*
The single biggest blocker to "raw → finished automatically" is that most raw
footage has **unusable spans** — out of focus, blown out, violently shaky, a
thumb over the lens, a 4-second dead pause while someone repositions. A VLM
won't reliably flag these; classic CV nails them per frame for ~free.

- **Sharpness / focus**: variance of the Laplacian per frame → focus curve; flag
  soft spans, and pick the sharpest of N near-duplicate takes.
- **Exposure**: histogram clipping → over/under-exposed spans.
- **Stability / shake**: inter-frame motion magnitude (optical flow or feature
  tracking) → "handheld-shaky vs locked-off"; also drives stabilization later.
- **Black/frozen frames, dropped focus**: ffmpeg `blackdetect`/`freezedetect`
  (already in the render-lint plan 3.2 — same primitives, applied at ingest).

Output: a per-second `usable` score + reasons. Editor logic trims to usable
spans before Claude ever sees the clip; the picker stops proposing cuts that land
on garbage. **Recommend building first** — small, deterministic, immediately
raises the floor of every auto-edit.

### B. Camera motion & shot dynamics  *(cheap, high pacing value)*
Pans, tilts, push-ins, whip-pans, static holds — the grammar of where to cut and
what to emphasize, and exactly what sparse-frame VLMs miss.

- **Optical flow** (Farneback cheap / RAFT accurate) or sparse feature tracking
  (Lucas-Kanade) → classify each span: `static | pan | tilt | zoom-in | zoom-out
  | handheld | whip`. ffmpeg `vidstabdetect` gives a usable motion proxy nearly
  for free.
- **Shot-boundary detection** (PySceneDetect): a single uploaded `.mov` often
  contains several in-camera cuts; detect them so segments tile real shots, not
  arbitrary time. Also dedups "they restarted the take".

Use: snap cuts to whip-pans/static onsets; auto-pick the locked-off take for a
talking head; detect the push-in that signals "this is the reveal".

### C. People: faces + active speaker + hands/pose
Roadmap 1.2 already specs face detect/track/ReID (InsightFace / MediaPipe) — the
prerequisite for auto-reframe (2.3) and "cut to her reaction". Two additions that
matter specifically for *this* content:

- **Active-speaker detection** (TalkNet-ASD): which visible face is actually
  talking — essential once two people are on screen (the panel-build clips) so
  reframe/zoom targets the speaker, not the listener.
- **Hand tracking / pose** (MediaPipe Hands / RTMPose): for DIY the *hands* are
  the subject — measuring, marking, cutting, stapling, lifting. Hand presence +
  motion is a cheap, reliable detector of "work is happening here", which is
  exactly the B-roll beat worth keeping. Gaze direction (looking at camera vs at
  the work) also separates "address" from "demonstration".

### D. Frame & clip embeddings (CLIP / SigLIP)  *(foundational at scale)*
One embedding per sampled frame / per clip unlocks retrieval and dedup, which the
"give it a folder of raw footage" goal needs structurally:

- **Semantic search across all footage**: "the finished-panel reveal", "the
  broken cutting machine", text → matching frames (open-vocabulary, no labels).
- **Near-duplicate / repeated-take clustering**: group the 12 `IMG_262x.mov`
  takes; keep the best (pair with the QC score in A).
- **B-roll ↔ narration matching**: embed the spoken line, retrieve the frame
  whose embedding is closest → auto-overlay the right cutaway over the right
  sentence. This is a large chunk of "professional-looking" for process content.
- **Cross-clip continuity**: same object/location across clips (the blue panel
  through build → reveal) via embedding similarity.

### E. Object / product detection — the YOLO idea, done right
Closed-set YOLO (COCO's 80 classes) is a poor fit: your salient objects are
"Rockwool insulation", "Everbilt bucket", "jigsaw", "tape measure", "staple gun",
"the BROKEN panel saw" — none are COCO classes. Use **open-vocabulary detection**
instead:

- **Grounding DINO / YOLO-World / OWLv2**: query with arbitrary text → boxes.
  You ask for exactly the props you care about and get tracked boxes.

What detection adds over Gemini (which already *names* these): **boxes over time**
— a prop/tool inventory per clip, a reframe/zoom target ("punch in on the saw"),
and continuity ("the panel appears in clips 3,7,9 at these times"). Treat it as
the spatial/temporal layer under Gemini's naming, not a replacement for naming.
Medium priority — most valuable once reframe (2.3) and B-roll matching (D) exist
to consume the boxes.

### F. Action / activity recognition (temporal video models)
The clearest VLM blind spot for process content. Video models that ingest a clip
of frames (not stills) read motion directly:

- **VideoMAE / InternVideo / X3D / TimeSformer**: classify or caption short
  windows — "cutting", "drilling", "measuring", "stapling", "carrying",
  "assembling". Pre-trained action labels (Kinetics) are generic; the win is
  fine-tuning a tiny head on *your* recurring actions, or using the video
  embedding for retrieval the same way as D.

Use: automatically locate the satisfying action beats and the "money" moments
(first staple, final reveal) that make process edits feel professional. Higher
effort; revisit after A–D prove out. Note: audio is often a cheaper action
detector (see H) — a saw/drill/staple has an unmistakable sound.

### G. OCR / on-screen text  *(cheap, surprisingly relevant here)*
Your footage is full of legible text: the DAW/measurement screens ("50 ms",
"300 ms"), the "BROKEN" spray paint, product labels and price tags, the
hand-drawn plan. PaddleOCR / Tesseract (or Gemini's own OCR on a targeted frame).

Use: (1) a near-free source of **overlay copy** grounded in what's literally on
screen; (2) searchability ("the clip where the price was $X"); (3) reading the
plan/measurements to caption the build accurately.

### H. Audio perception beyond words  *(roadmap 1.4 — reinforced)*
Already planned; flagged here because for DIY it doubles as a cheap action and
pacing detector:

- **Sound-event classification** (YAMNet / PANNs CPU): laughter, impact, tool
  sounds (saw/drill/staple/hammer), music start, applause.
- **Loudness / silence** (already have VAD + waveform): cut-snapping to pauses
  (roadmap 2.1 `nudge_to_silence`), and dead-air trimming feeds straight into the
  QC gate (A).

A tool-sound spike is a more reliable "the cut happened here" marker than any
visual model, and it's nearly free given audio is already extracted.

### I. Aesthetic / hero-frame scoring
Aesthetic predictors (LAION-aesthetic / NIMA) + saliency → score frames for
thumbnail/cover worthiness and pick the hero frame of a reveal. Low effort, nice
polish; do it after the structural pieces (A–D).

---

## Getting the LLM to see more (no new models, mostly prompting/plumbing)

Often higher ROI than any new specialist:

- **Ground Gemini with the cheap signals.** Feed the motion track, OCR text,
  detected objects/boxes, and active-speaker into the VLM prompt: "Here are the
  detected camera moves, on-screen text, and tracked objects; now describe the
  *action* and pick cut points." A grounded small model beats an ungrounded big
  one, and it stops the VLM hallucinating spatial/temporal facts.
- **Force it to describe change.** Feed before/after frame *pairs* (or a
  contact-sheet of a window — you already build these) and ask what changed
  between them; this surfaces motion/action a single-frame look misses.
- **Sample denser where it matters.** Higher fps / higher media-resolution on the
  candidate windows only (the `flash_lowres` variant already quantifies the cost
  knob). Cheap whole-clip pass → dense pass on the few windows that matter.
- **Ask editorial questions, not "describe".** "Is the subject sharp and centered
  here? Is this the moment the panel is revealed? Rate hook strength." Targeted
  structured questions extract decisions the open-ended prompt leaves implicit.
- **Multi-pass zoom** (partly built): coarse whole-clip highlights → targeted
  `describe` sub-range → frame-level confirm. Push the perception document to
  carry all three resolutions.

---

## Recommended sequencing

Cheap-and-deterministic first (raises the floor of every auto-edit), foundational
retrieval next, spatial/temporal specialists last:

1. **A. Technical QC** (blur/exposure/stability/black-frozen) — gate unusable
   footage. Highest autonomy leverage, smallest build.
2. **B. Camera-motion + shot-boundary** — pacing and cut-point signals; cheap.
3. **Grounding pass** — feed A+B (+ existing transcript/audio) into Gemini so the
   *existing* analysis immediately gets sharper. Pure plumbing.
4. **D. CLIP/SigLIP embeddings** — retrieval, dedup, continuity, B-roll matching.
   Foundational for "folder of raw footage".
5. **C. Faces + active-speaker + hands/pose** — unlocks reframe (roadmap 2.3) and
   DIY action beats.
6. **E. Open-vocab detection** and **F. action recognition** — once reframe and
   B-roll matching exist to consume boxes/actions.
7. **G. OCR**, **H. sound-events**, **I. aesthetic scoring** — polish, slot in
   opportunistically (all individually cheap).

Common rule: run on the 480p proxy, write a JSON sidecar to R2, merge into the
per-clip perception document, expose read-only via the API — same shape as the
transcript and audio-analysis pipelines already in the repo.
