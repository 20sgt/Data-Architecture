-- One row per (meeting, document). Single-level unnest of the documents list;
-- each document is a struct of {document_source, document_title, document_url,
-- body_text}.

select
    m.meeting_id,
    document_seq,
    document.document_source     as document_source,
    document.document_title      as document_title,
    document.document_url        as document_url,
    document.body_text           as body_text,
    m.ingest_date,
    m.source_file,
    m.loaded_at
from {{ source('bronze', 'meetings') }} m
lateral view posexplode(m.documents) d as document_seq, document
