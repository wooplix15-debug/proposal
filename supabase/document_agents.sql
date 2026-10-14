-- Run once to allow the new individual document types in saved history.
begin;
alter table public.generated_documents drop constraint if exists generated_documents_document_type_check;
alter table public.generated_documents add constraint generated_documents_document_type_check
    check (document_type in ('proposal', 'brd', 'software_plan', 'requirements', 'ux', 'implementation', 'qa', 'documentation', 'handover'));
commit;
