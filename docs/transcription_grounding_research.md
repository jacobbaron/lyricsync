# Grounding transcription in project context — research notes

Idea: feed the transcriber context from the user's own projects so it gets the
domain vocabulary, proper nouns, and spellings right — and improves over time as
the user creates more content. Does this need fine-tuning? Mostly **no**. The
cheap levers get you most of the way; fine-tuning is a later, data-gated option.

Grounded in the current pipeline (`modal/app.py`):

- **Transcription**: OpenAI `whisper-1` API, `verbose_json`, word+segment
  timestamps (`_transcribe_worker`). **No `prompt` is passed today** — the single
  biggest missed lever.
- **Alignment**: WhisperX wav2vec2 forced alignment (`_align_worker`) re-derives
  word timings from the raw transcript **text**. Key consequence: *if you correct
  the raw text before alignment, the corrected words still get accurate
  timestamps.* This makes a text-level correction step safe.

So there are two independent grounding points, both available with zero
fine-tuning, plus a flywheel that makes them better over time.

---

## Lever 1 — Whisper `prompt` biasing  *(smallest change, do first)*

`client.audio.transcriptions.create(...)` accepts a `prompt` string. Whisper uses
it as preceding context to bias vocabulary and spelling toward the terms it
contains — exactly "give the transcriber context." We pass nothing today.

Build a per-clip prompt from project context:
- Project name / description ("Hanging ceiling acoustic panels").
- A **domain glossary** of recurring terms: brands/products (Rockwool, Everbilt,
  MDF), tools (jigsaw, staple gun), and the audio-engineering jargon that shows
  up in the vocal/acoustics projects (RT60, decay, "300 ms", reverb, frequency
  response). These are precisely the words a generic model mis-hears or
  mis-spells.
- Proper nouns: people's names, the channel name.

Caveats (real, design around them):
- **~224-token budget.** You can't dump everything — you must *select* the most
  relevant terms per clip (see the flywheel below).
- `whisper-1` conditions mainly the **first window**; the effect decays over a
  long clip and can induce hallucination if the prompt is overloaded or
  off-topic. Keep it a tight, comma-separated term list, not prose.
- It **biases, doesn't guarantee** — pair with Lever 2 for the misses.

Insertion point: `_transcribe_worker`, build `prompt=` before the
`transcriptions.create` call from a project-glossary lookup.

> Model note: `gpt-4o-transcribe` / `gpt-4o-mini-transcribe` take a `prompt` too
> and are more steerable (freer instructions, not just a word list). Word-level
> timing isn't their strength, but it doesn't matter here — WhisperX supplies the
> timings — so the raw-text engine is swappable if the newer models transcribe
> this domain better. Worth an A/B once Lever 1 exists.

## Lever 2 — LLM glossary-correction pass  *(strongest grounding, no token cap)*

Run a cheap LLM (Haiku/Flash) over the raw transcript with the **full** project
context (no 224-token limit): "Here is the raw transcript and the project's known
terms / people / product names. Fix misheard proper nouns and domain terms; do
not change anything else." Because WhisperX aligns from text, do this **before
alignment** and the corrected words get correct timestamps for free:

```
raw whisper text → LLM glossary-correction → WhisperX align → merged.json
```

Why this is the best single lever: it has the entire project context and an
actual reasoning model, so it catches what 224 tokens of biasing can't, and it
can't break timing. Constrain it to substitutions/spelling (not rewrites) so it
stays faithful. Store the diff in a debug blob (mirrors the repo's existing
debug discipline) so corrections are auditable.

Insertion point: end of `_transcribe_worker` (before writing `transcript.json`),
or start of `_align_worker` (before `whisperx.align`).

## Lever 3 — Local faster-whisper with `hotwords`  *(optional engine change)*

WhisperX already pulls in **faster-whisper**, which supports `initial_prompt`
*and* a dedicated `hotwords=` parameter (stronger, cleaner biasing than the prompt
hack) and `prefix`. Moving raw transcription onto the existing align image would:
- give a real hotword/bias API instead of the prompt workaround,
- remove the **25 MB OpenAI limit** (the code already bails on large clips —
  `WHISPER_LIMIT_BYTES`),
- unify the two passes into one image.

Trade-off: you own the GPU compute and model management instead of an API call.
Reasonable once the glossary flywheel proves the biasing is worth leaning on.

---

## The flywheel — "learns to be more accurate over time"

The part that makes this compound. Maintain a **per-user / per-project lexicon**
(a new table: term, weight/frequency, source, embedding) that grows automatically,
and select the top-N terms relevant to each clip (by embedding similarity to the
project) to fit Lever 1's budget while Lever 2 can take the whole list.

Sources that populate it, cheapest → strongest signal:
1. **Prior transcripts** in this project and across the user's account — mine
   rare/capitalized/proper-noun tokens and recurring jargon by frequency.
2. **OCR / on-screen text** (the visual-perception thread): product labels, the
   DAW/measurement screens, price tags, the "BROKEN" spray paint → *exact*
   spellings of the very brands Whisper mangles. This is a strong cross-feature
   synergy — the vision pipeline feeds the transcription lexicon.
3. **Story / overlay copy the user wrote or approved** — their preferred spellings
   and names, already curated by them.
4. **User-corrected transcripts** (needs a small correction UI) — the gold signal,
   and the same data that would later justify fine-tuning.

Each new project both *consumes* the accumulated lexicon and *contributes* to it,
so accuracy on the user's recurring vocabulary climbs without any model training.

---

## RAG retrieval layer — context corpus, searched per clip

The flywheel above is a *structured term list*. A RAG layer generalizes it: a
**document corpus** of richer context (not just terms) that you semantically
search and inject. It's the retrieval engine *under* Levers 1+2 — the answer to
"how do we pick what to inject" once the corpus is bigger than a hand-curated
list.

### The crux: there's no query until you've transcribed

Normal RAG embeds a **text query** and retrieves against it. Transcription has no
text query up front — the thing you'd search with is the audio you haven't
transcribed yet. Naive "RAG for transcription" trips on exactly this. Resolve it
by forming the query from signals you *do* have, in two passes:

1. **Pre-transcription query** (before any audio): project name/description, clip
   filename, sibling clips in the project, and **OCR / visual analysis** of this
   clip (on-screen product labels, the DAW screen). Retrieve a first context set.
2. **Post-first-pass query** (the natural fit): run the cheap raw Whisper pass,
   then **use that noisy transcript as the query** to retrieve precise context,
   and feed it into the **correction pass (Lever 2)** — re-decoding isn't needed
   because alignment runs on the corrected text anyway. The raw transcript *is*
   the query; this is the cleanest design.

Granularity: retrieve **per clip** (sweet spot), or **per window** for long
technical clips where the topic drifts (acoustics → mixing → measurement).

### What gets injected where — match retrieval to the budget

RAG returns *passages*; the two levers want different shapes:
- **→ Lever 2 (LLM correction): the primary home.** No 224-token cap — feed the
  retrieved chunks straight in ("here are reference passages about this project's
  domain; fix misheard terms"). The LLM reasons over prose and ignores irrelevant
  hits. RAG fits here naturally.
- **→ Lever 1 (Whisper prompt): distill, don't dump.** 224 tokens wants
  high-precision *terms*, not paragraphs — and off-topic prose actively induces
  hallucination. So extract the salient entities from the retrieved chunks into a
  short term list. For the tight prompt, the curated lexicon is often still better
  than raw RAG; use RAG to *keep the lexicon fresh*, not to fill the prompt with
  prose.

### Corpus sources

- Everything the flywheel already mines (prior transcripts, OCR, overlay copy,
  user corrections) — now stored as searchable chunks, not just term counts.
- The user's **project notes / descriptions / scripts**.
- **External reference docs** that a term list can't capture: product spec sheets
  & manuals (brand spellings, model numbers), an audio-engineering glossary, tool
  terminology. This is where RAG earns its keep over the lexicon — large,
  unstructured, domain corpora you'd never hand-curate.

### Infra — no new datastore

Postgres is already there → use **pgvector** (`context_documents` +
`context_chunks(embedding vector)` tables). Embed chunks on ingest (an embedding
API or a small Modal function), cosine-search at transcription time. No separate
vector DB; matches the repo's "JSON sidecar + a table" pattern.

### Risks

- **Wrong-context injection** is the main failure: a confident-but-irrelevant
  retrieval biases toward words not actually said (worst in the Whisper prompt,
  where it hallucinates). Mitigate with a similarity floor, anchoring relevance to
  the first-pass transcript, and preferring the correction-pass home where the LLM
  can disregard bad hits.
- **Cold start**: empty corpus for a new user → seed with the external domain
  reference docs + project metadata until their own content accumulates.
- **Cost/latency**: two passes + retrieval per clip; cache project-level
  retrievals across clips.

---

## Fine-tuning — when it's actually warranted (not first)

Be honest about the heavy path: fine-tuning is rarely needed for "get the jargon
right" — Levers 1+2 plus the flywheel cover it. Fine-tuning earns its cost only
when there's a **consistent acoustic/domain mismatch** (a strong accent, stable
recording conditions, a large specialized corpus) **and** you have enough labeled
data (hundreds+ of corrected utterances). Note two constraints:
- You **can't** fine-tune the hosted `whisper-1`; you'd fine-tune **open** Whisper
  (HuggingFace) and self-host — i.e. it implies the Lever 3 engine move anyway.
- The training data is exactly the **user-correction** stream from the flywheel
  (source 4). So building the flywheel first is non-regret: it powers the cheap
  levers now and becomes the fine-tuning dataset later if the data ever justifies
  it.

---

## Recommended sequencing

1. **Lever 1** — pass a project-glossary `prompt` to Whisper. Tiny change, real
   gain. Seed the glossary from project name + a hand-list to start.
2. **Flywheel v1** — lexicon table populated from prior transcripts + OCR +
   overlay copy; relevance-rank to fill the 224-token budget.
3. **Lever 2** — LLM glossary-correction before alignment (timing-safe). Biggest
   accuracy lift; uses the full lexicon.
4. **RAG layer** — pgvector corpus + two-pass retrieval (raw transcript as query)
   feeding the Lever 2 correction pass; add external reference docs. Generalizes
   the flywheel once the corpus outgrows a curated term list.
5. **Correction UI** — capture user edits → strongest lexicon/corpus signal +
   future training data.
6. **Lever 3 / fine-tuning** — only if A/B shows the API engine is the ceiling, or
   the correction corpus grows enough to justify training. Data-gated, not now.
