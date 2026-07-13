-- One row per (matter, attachment). Single-level unnest of the attachments list;
-- each attachment is a struct of {name, url}.

select
    m.file_number                    as matter_file,
    attachment_seq,
    attachment.name                  as attachment_name,
    attachment.url                   as attachment_url,
    m.ingest_date,
    m.source_file,
    m.loaded_at
from {{ source('bronze', 'matters') }} m
lateral view posexplode(m.attachments) x as attachment_seq, attachment
