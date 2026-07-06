-- PERCEPTION T6: open-vocabulary object detection.
-- The detection worker now needs run-time inputs supplied at creation (the
-- detector `mode` — 'closed' | 'open' — and, for open-vocab, the `labels`
-- text-query list). clip_signals previously carried no request-side params
-- (the T5 closed-set run was fully implied by kind='detection'), so add a
-- generic `params` jsonb the route writes at insert and the worker reads.
--
--   params — request inputs for the run, shape depends on `kind`
--            (detection: {mode:'closed'|'open', labels?:string[]})

alter table clip_signals
  add column if not exists params jsonb;
