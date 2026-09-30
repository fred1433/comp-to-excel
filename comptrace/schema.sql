-- Staging schema for sale records. PostgreSQL 14+.
-- Owner: comp_owner. Application roles: comp_extractor, comp_reviewer, comp_writer (log in as comp_app, then SET ROLE).
create schema comp;

create table comp.source_document (
  doc text primary key,
  file_sha256 text not null unique,           -- the same file ingested twice is a replay, not a new source
  layer_sha256 text not null,                 -- hash of the page text layer (OCR or PDF text) the checks ran on
  text_source text not null,
  label text not null,
  url text,
  retrieved_at text
);

create table comp.span (
  id text primary key,
  doc text not null references comp.source_document,
  page int not null,
  text text not null,
  bbox numeric[]
);

create table comp.property (
  id bigserial primary key,
  label text not null
);

create table comp.parcel (
  parid text primary key,
  property_id bigint not null references comp.property
);

-- A recorded instrument and the economic transaction are kept apart: a corrective deed is not a resale.
create table comp.sale_transaction (
  id bigserial primary key,
  property_id bigint not null references comp.property,
  instrument_no text unique,                  -- the database refuses a second transaction on the same instrument
  sale_date date,
  sale_period text,                           -- when only a month is known
  price numeric(14,2),
  kind text not null default 'sale' check (kind in ('sale','corrective')),
  created_by text not null default current_user
);

create table comp.transaction_parcel (
  transaction_id bigint not null references comp.sale_transaction,
  parid text not null references comp.parcel,
  primary key (transaction_id, parid)
);

create table comp.fact (
  id bigserial primary key,
  doc text not null references comp.source_document,
  meaning_code text not null,
  meaning text not null,
  value text not null,
  span_ids text[] not null,
  quote text not null,
  derived_from jsonb,
  check_status text not null check (check_status in ('verified','rejected')),
  reject_reason text,
  scope text not null check (scope in ('transaction','property','authorization','listing')),
  transaction_id bigint references comp.sale_transaction,
  property_id bigint references comp.property,
  created_by text not null default current_user,
  unique (doc, meaning_code, value, span_ids)
);

create table comp.match_review (
  id bigserial primary key,
  doc text not null,
  verdict text not null check (verdict in ('same_document_replay','same_sale_other_source','resale','held_for_review','property_facts')),
  transaction_id bigint references comp.sale_transaction,
  reasons jsonb not null,
  decided_by text not null default current_user,
  confirmed_by text,
  confirmed_at timestamptz
);

create table comp.comp_record (
  id bigserial primary key,
  transaction_id bigint not null unique references comp.sale_transaction,
  entry jsonb not null,                       -- values per grid field, each with its fact ids
  status text not null check (status in ('extracted','evidence_checked','approved_for_demo_entry')),
  appraiser_verification text not null default 'pending',
  version_sha256 text not null
);

create table comp.approval (
  id bigserial primary key,
  comp_id bigint not null references comp.comp_record,
  version_sha256 text not null,               -- record + evidence + input map + template + destination
  approved_by text not null,
  approved_at timestamptz not null default now(),
  destination text not null
);

create table comp.write_operation (
  op_id uuid primary key,
  approval_id bigint not null references comp.approval,
  version_sha256 text not null,
  template_sha256 text not null,
  expected_patched_sha256 text not null,
  output_path text not null,
  status text not null check (status in ('intent','done')),
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  output_sha256 text
);
create unique index one_done_write_per_approval on comp.write_operation (approval_id) where status = 'done';

-- Audit trail: hash-chained, insert-only.
create table comp.audit_log (
  seq bigserial primary key,
  at timestamptz not null default clock_timestamp(),
  actor text not null default current_user,
  action text not null,
  object text,
  detail jsonb not null default '{}',
  prev_sha256 text not null,
  row_sha256 text not null
);

create function comp.audit_row_hash(r comp.audit_log) returns text language sql immutable as $$
  select encode(sha256(convert_to(concat_ws('|', r.seq::text, to_char(r.at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
         r.actor, r.action, coalesce(r.object, ''), r.detail::text, r.prev_sha256), 'UTF8')), 'hex')
$$;

create function comp.audit_chain() returns trigger language plpgsql security definer set search_path = comp, pg_temp as $$
begin
  perform pg_advisory_xact_lock(724001);
  new.prev_sha256 := coalesce((select row_sha256 from comp.audit_log order by seq desc limit 1), repeat('0', 64));
  new.row_sha256 := comp.audit_row_hash(new);
  return new;
end $$;
create trigger audit_chain before insert on comp.audit_log for each row execute function comp.audit_chain();

create function comp.audit_refuse() returns trigger language plpgsql as $$
begin
  raise exception 'audit_log is append-only (% refused)', tg_op;
end $$;
create trigger audit_no_update before update or delete on comp.audit_log for each row execute function comp.audit_refuse();
create trigger audit_no_truncate before truncate on comp.audit_log for each statement execute function comp.audit_refuse();

create function comp.verify_audit_chain() returns table(seq bigint, ok boolean) language sql stable as $$
  select a.seq,
         a.row_sha256 = comp.audit_row_hash(a)
         and a.prev_sha256 = coalesce(lag(a.row_sha256) over (order by a.seq), repeat('0', 64))
  from comp.audit_log a order by a.seq
$$;

-- Roles and privileges.
create role comp_extractor nologin;
create role comp_reviewer nologin;
create role comp_writer nologin;
create role comp_app login;
grant comp_extractor, comp_reviewer, comp_writer to comp_app;
grant usage on schema comp to comp_extractor, comp_reviewer, comp_writer;
grant usage on all sequences in schema comp to comp_extractor, comp_reviewer, comp_writer;

grant select, insert on comp.source_document, comp.span, comp.property, comp.parcel, comp.sale_transaction,
  comp.transaction_parcel, comp.fact, comp.match_review, comp.comp_record to comp_extractor;
grant update (transaction_id, property_id, scope) on comp.fact to comp_extractor;
grant update (price, sale_date) on comp.sale_transaction to comp_extractor;
grant select on all tables in schema comp to comp_reviewer;
grant update (status, version_sha256, entry) on comp.comp_record to comp_reviewer;
grant update (confirmed_by, confirmed_at) on comp.match_review to comp_reviewer;
grant insert on comp.approval to comp_reviewer;
grant select on comp.comp_record, comp.approval, comp.fact, comp.source_document, comp.sale_transaction to comp_writer;
grant select, insert, update (status, finished_at, output_sha256) on comp.write_operation to comp_writer;
grant select, insert on comp.audit_log to comp_extractor, comp_reviewer, comp_writer;
revoke update, delete, truncate on comp.audit_log from public, comp_extractor, comp_reviewer, comp_writer, comp_app;
grant execute on function comp.verify_audit_chain() to comp_extractor, comp_reviewer, comp_writer;

-- Row-level security: the writer sees only records approved for entry.
alter table comp.comp_record enable row level security;
create policy extractor_all on comp.comp_record to comp_extractor using (true) with check (status = 'extracted');
create policy reviewer_all on comp.comp_record to comp_reviewer using (true) with check (true);
create policy writer_approved_only on comp.comp_record for select to comp_writer using (status = 'approved_for_demo_entry');
