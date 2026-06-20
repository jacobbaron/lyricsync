-- P3-01: Personal API keys for machine-to-machine access (MCP server, scripts).
--
-- A key is a random secret of the form `lsk_<base64url>`. Only its SHA-256
-- hash is ever stored — the plaintext is shown to the user exactly once at
-- creation and is unrecoverable thereafter (not even the operator can read it
-- from the database).
--
-- Self-service: a logged-in user creates/lists/revokes their own keys through
-- their browser session, so RLS scopes every row to owner = the JWT email,
-- exactly like the other tables. The auth-time lookup-by-hash (which happens
-- before any identity is known) runs with the service-role key and bypasses
-- RLS by design.

create table api_keys (
  id           uuid primary key default gen_random_uuid(),
  owner        text not null,          -- email; matches projects.owner / JWT email
  owner_id     uuid not null,          -- auth.users id, used as the minted JWT `sub`
  key_hash     text not null unique,   -- sha256 hex of the plaintext key
  key_prefix   text not null,          -- e.g. "lsk_AbCd1234" for display only
  label        text,                   -- optional user-supplied name
  created_at   timestamptz not null default now(),
  last_used_at timestamptz,
  revoked_at   timestamptz
);

create index api_keys_owner_idx    on api_keys(owner);
create index api_keys_key_hash_idx on api_keys(key_hash);

alter table api_keys enable row level security;

create policy api_keys_owner_all on api_keys
  for all to authenticated
  using (owner = (auth.jwt() ->> 'email'))
  with check (owner = (auth.jwt() ->> 'email'));
