-- Run once in Supabase Dashboard → SQL Editor for the `proposal` project.
-- Keeps generated documents private and isolated to their owner.

begin;

create table if not exists public.generated_documents (
    id uuid primary key,
    user_id uuid not null references auth.users (id) on delete cascade,
    title text not null,
    filename text not null,
    document_type text not null check (document_type in ('proposal', 'brd')),
    storage_path text not null unique,
    created_at timestamptz not null default now()
);

alter table public.generated_documents enable row level security;
revoke all on table public.generated_documents from anon;
grant select, insert, delete on table public.generated_documents to authenticated;

drop policy if exists "Users can read their generated documents" on public.generated_documents;
create policy "Users can read their generated documents"
    on public.generated_documents for select to authenticated
    using ((select auth.uid()) = user_id);

drop policy if exists "Users can save their generated documents" on public.generated_documents;
create policy "Users can save their generated documents"
    on public.generated_documents for insert to authenticated
    with check ((select auth.uid()) = user_id);

drop policy if exists "Users can delete their generated documents" on public.generated_documents;
create policy "Users can delete their generated documents"
    on public.generated_documents for delete to authenticated
    using ((select auth.uid()) = user_id);

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
    'generated-documents',
    'generated-documents',
    false,
    52428800,
    array[
        'application/pdf',
        'application/zip',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'application/octet-stream'
    ]
)
on conflict (id) do update
set public = false,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

drop policy if exists "Users can view their saved output files" on storage.objects;
create policy "Users can view their saved output files"
    on storage.objects for select to authenticated
    using (
        bucket_id = 'generated-documents'
        and (storage.foldername(name))[1] = (select auth.uid()::text)
    );

drop policy if exists "Users can upload their saved output files" on storage.objects;
create policy "Users can upload their saved output files"
    on storage.objects for insert to authenticated
    with check (
        bucket_id = 'generated-documents'
        and (storage.foldername(name))[1] = (select auth.uid()::text)
    );

drop policy if exists "Users can delete their saved output files" on storage.objects;
create policy "Users can delete their saved output files"
    on storage.objects for delete to authenticated
    using (
        bucket_id = 'generated-documents'
        and (storage.foldername(name))[1] = (select auth.uid()::text)
    );

commit;
