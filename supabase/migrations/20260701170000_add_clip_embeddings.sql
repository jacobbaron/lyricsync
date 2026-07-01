-- PERCEPTION T4: per-frame + pooled clip embeddings in pgvector, for semantic
-- search across footage, near-duplicate clustering, and B-roll↔narration
-- matching later. Independent of the clip_signals track (own table).
--
--   t          — timestamp (seconds from clip start) of the sampled frame;
--                NULL = the mean-pooled, whole-clip vector.
--   embedding  — L2-normalized CLIP image/text vector (cosine == dot product).
--   model      — the encoder id + dim live alongside the vector so we can
--                re-embed with a different model later without guessing.
--
-- Model: sentence-transformers "clip-ViT-B-32" → 512-dim shared image/text
-- space (a text query and a frame land in the same space, so cosine search
-- works cross-modal). Keep the column dim in sync with embedding.EMBED_DIM.

create extension if not exists vector;

create table if not exists clip_embeddings (
  id          uuid primary key default gen_random_uuid(),
  clip_id     uuid not null references clips(id) on delete cascade,
  t           real,                         -- NULL = pooled clip-level vector
  embedding   vector(512) not null,
  model       text not null,
  created_at  timestamptz not null default now()
);

create index if not exists clip_embeddings_clip_id_idx
  on clip_embeddings(clip_id);

-- HNSW cosine index for approximate nearest-neighbour search. HNSW needs no
-- training pass (unlike ivfflat) so it works from the first row — right for a
-- table that starts empty and grows incrementally.
create index if not exists clip_embeddings_embedding_hnsw_idx
  on clip_embeddings using hnsw (embedding vector_cosine_ops);

-- RLS: scope each embedding to the owner of the clip's project (mirrors
-- visual_analyses / clip_signals).
alter table clip_embeddings enable row level security;

create policy clip_embeddings_owner_all on clip_embeddings
  for all to authenticated
  using (exists (
    select 1 from clips c
    join projects p on p.id = c.project_id
    where c.id = clip_embeddings.clip_id
      and p.owner = (auth.jwt() ->> 'email')
  ))
  with check (exists (
    select 1 from clips c
    join projects p on p.id = c.project_id
    where c.id = clip_embeddings.clip_id
      and p.owner = (auth.jwt() ->> 'email')
  ));

-- Cross-clip semantic search within one project. Takes the query vector as a
-- text-encoded pgvector literal ('[0.1,0.2,...]') so PostgREST can pass it
-- through cleanly, and casts it inside. SECURITY INVOKER (default) means the
-- join to clips is filtered by the caller's RLS policy — a non-owner sees
-- nothing even though we pass a project id.
create or replace function search_clip_embeddings(
  p_project_id  uuid,
  p_query       text,
  p_match_count int default 10,
  p_frames_only boolean default true
)
returns table (clip_id uuid, t real, score real)
language sql
stable
as $$
  select ce.clip_id,
         ce.t,
         (1 - (ce.embedding <=> p_query::vector))::real as score
  from clip_embeddings ce
  join clips c on c.id = ce.clip_id
  where c.project_id = p_project_id
    and (not p_frames_only or ce.t is not null)
  order by ce.embedding <=> p_query::vector
  limit p_match_count;
$$;
