-- SEARCH S3 (#120): library-wide (cross-project) semantic search over
-- clip_embeddings. Mirrors search_clip_embeddings (PERCEPTION T4, #87 / PR
-- #98) exactly, minus the `p_project_id` filter — scoping to "everything the
-- caller owns" is left entirely to RLS via SECURITY INVOKER (the default),
-- same as the cross-project keyword route (S1, #83) does for clips/projects.
--
-- Also returns project_id alongside clip_id/t/score so the caller (the
-- unified /api/search route) can attach a project name without a second
-- per-row lookup.
create or replace function search_clip_embeddings_global(
  p_query       text,
  p_match_count int default 10,
  p_frames_only boolean default true
)
returns table (clip_id uuid, project_id uuid, t real, score real)
language sql
stable
as $$
  select ce.clip_id,
         c.project_id,
         ce.t,
         (1 - (ce.embedding <=> p_query::vector))::real as score
  from clip_embeddings ce
  join clips c on c.id = ce.clip_id
  where (not p_frames_only or ce.t is not null)
  order by ce.embedding <=> p_query::vector
  limit p_match_count;
$$;
