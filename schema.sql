create table if not exists public.catches (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users (id) on delete cascade,
    caught_at timestamptz not null,
    species text not null,
    bait text not null,
    notes text not null default '',
    photo_path text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    deleted_at timestamptz
);

alter table public.catches
    add column if not exists updated_at timestamptz not null default now(),
    add column if not exists deleted_at timestamptz;

create index if not exists catches_user_caught_at_idx
    on public.catches (user_id, caught_at desc);

alter table public.catches enable row level security;

drop policy if exists "Users can view their catches" on public.catches;
create policy "Users can view their catches"
    on public.catches for select
    using ((select auth.uid()) = user_id);

drop policy if exists "Users can add their catches" on public.catches;
create policy "Users can add their catches"
    on public.catches for insert
    with check ((select auth.uid()) = user_id);

drop policy if exists "Users can edit their catches" on public.catches;
create policy "Users can edit their catches"
    on public.catches for update
    using ((select auth.uid()) = user_id)
    with check ((select auth.uid()) = user_id);

drop policy if exists "Users can delete their catches" on public.catches;
create policy "Users can delete their catches"
    on public.catches for delete
    using ((select auth.uid()) = user_id);

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
    'catch-photos',
    'catch-photos',
    false,
    8388608,
    array['image/jpeg', 'image/png', 'image/webp']
)
on conflict (id) do update
set public = false,
    file_size_limit = 8388608,
    allowed_mime_types = array['image/jpeg', 'image/png', 'image/webp'];

drop policy if exists "Users can view their catch photos" on storage.objects;
create policy "Users can view their catch photos"
    on storage.objects for select
    to authenticated
    using (
        bucket_id = 'catch-photos'
        and (storage.foldername(name))[1] = (select auth.uid()::text)
    );

drop policy if exists "Users can upload their catch photos" on storage.objects;
create policy "Users can upload their catch photos"
    on storage.objects for insert
    to authenticated
    with check (
        bucket_id = 'catch-photos'
        and (storage.foldername(name))[1] = (select auth.uid()::text)
    );

drop policy if exists "Users can update their catch photos" on storage.objects;
create policy "Users can update their catch photos"
    on storage.objects for update
    to authenticated
    using (
        bucket_id = 'catch-photos'
        and (storage.foldername(name))[1] = (select auth.uid()::text)
    )
    with check (
        bucket_id = 'catch-photos'
        and (storage.foldername(name))[1] = (select auth.uid()::text)
    );

drop policy if exists "Users can delete their catch photos" on storage.objects;
create policy "Users can delete their catch photos"
    on storage.objects for delete
    to authenticated
    using (
        bucket_id = 'catch-photos'
        and (storage.foldername(name))[1] = (select auth.uid()::text)
    );
