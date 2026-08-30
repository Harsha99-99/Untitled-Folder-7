-- 0001_init_schema.sql — recordings + device metadata for the audio hub.
--
-- Apply with the Supabase CLI (`supabase db push`) or the dashboard SQL editor.
-- Storage of the actual audio lives in the `recordings` bucket; these tables
-- make captures queryable and devices nameable.

-- ---- devices ---------------------------------------------------------------
create table if not exists public.devices (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  sample_rate int,
  last_seen   timestamptz,
  created_at  timestamptz not null default now()
);

-- ---- sessions (one row per recording) --------------------------------------
create table if not exists public.sessions (
  id          uuid primary key default gen_random_uuid(),
  device_id   uuid references public.devices(id) on delete set null,
  device_name text not null,                       -- denormalised: survives device deletion
  started_at  timestamptz not null default now(),
  ended_at    timestamptz,
  storage_key text,                                -- object path in the recordings bucket
  bytes       bigint,
  sample_rate int
);

create index if not exists sessions_started_at_idx on public.sessions (started_at desc);
create index if not exists sessions_device_idx     on public.sessions (device_id);

-- ---- storage bucket (private; dashboard reads via signed URLs) -------------
insert into storage.buckets (id, name, public)
values ('recordings', 'recordings', false)
on conflict (id) do nothing;

-- ---- row-level security ----------------------------------------------------
-- Lock everything down by default. The recorder uses the service-role key,
-- which bypasses RLS; the browser dashboard reads through policies scoped to
-- authenticated users. Tighten to per-user ownership once auth lands (phase 4).
alter table public.devices  enable row level security;
alter table public.sessions enable row level security;

create policy "authenticated read devices"
  on public.devices for select
  to authenticated using (true);

create policy "authenticated read sessions"
  on public.sessions for select
  to authenticated using (true);

-- Only authenticated users may read objects in the private bucket.
create policy "authenticated read recordings"
  on storage.objects for select
  to authenticated using (bucket_id = 'recordings');
