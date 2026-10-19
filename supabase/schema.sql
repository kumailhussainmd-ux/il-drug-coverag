-- Illinois Drug Coverage: database schema (run once in the Supabase SQL editor)
create extension if not exists pg_trgm;

create table if not exists plans (
  contract_id text, plan_id text, segment_id text,
  contract_name text, plan_name text, formulary_id text,
  premium double precision, deductible double precision, snp text,
  primary key (contract_id, plan_id, segment_id)
);
create table if not exists formulary (
  formulary_id text, rxcui text, tier int,
  ql boolean, ql_amount double precision, ql_days int,
  pa boolean, st boolean, selected boolean,
  primary key (formulary_id, rxcui)
);
create table if not exists beneficiary_cost (
  contract_id text, plan_id text, segment_id text,
  coverage_level int, tier int, days_supply int,
  cost_type_pref int, cost_amt_pref double precision, cost_min_amt_pref double precision, cost_max_amt_pref double precision,
  cost_type_nonpref int, cost_amt_nonpref double precision, cost_min_amt_nonpref double precision, cost_max_amt_nonpref double precision,
  cost_type_mail_pref int, cost_amt_mail_pref double precision, cost_min_amt_mail_pref double precision, cost_max_amt_mail_pref double precision,
  cost_type_mail_nonpref int, cost_amt_mail_nonpref double precision, cost_min_amt_mail_nonpref double precision, cost_max_amt_mail_nonpref double precision,
  tier_specialty_yn text, ded_applies_yn text
);
create index if not exists bc_plan on beneficiary_cost (contract_id, plan_id, segment_id, tier);
create table if not exists insulin_cost (
  contract_id text, plan_id text, segment_id text, tier int, days_supply int,
  copay_pref double precision, copay_nonpref double precision, copay_mail_pref double precision, copay_mail_nonpref double precision,
  coin_pref double precision, coin_nonpref double precision, coin_mail_pref double precision, coin_mail_nonpref double precision
);
create table if not exists pharmacies (
  contract_id text, plan_id text, segment_id text, pharmacy_number text, zip text,
  pref_retail boolean, pref_mail boolean, is_retail boolean, is_mail boolean,
  floor_price double precision,
  brand_fee_30 double precision, brand_fee_60 double precision, brand_fee_90 double precision,
  generic_fee_30 double precision, generic_fee_60 double precision, generic_fee_90 double precision,
  selected_fee_30 double precision, selected_fee_60 double precision, selected_fee_90 double precision
);
create index if not exists ph_zip on pharmacies (zip, contract_id, plan_id, segment_id);
create table if not exists pricing (
  contract_id text, plan_id text, segment_id text, rxcui text, days_supply int, unit_cost double precision,
  primary key (contract_id, plan_id, segment_id, rxcui, days_supply)
);
create table if not exists drugs (
  rxcui text primary key, name text, tty text, generic_name text, brand_names text
);
create index if not exists drugs_trgm on drugs using gin ((coalesce(name,'')||' '||coalesce(generic_name,'')||' '||coalesce(brand_names,'')) gin_trgm_ops);
create table if not exists data_info (id int primary key default 1, spuf_file text, loaded_at timestamptz default now());

-- Public, read-only access for the website
do $$ declare t text; begin
  foreach t in array array['plans','formulary','beneficiary_cost','insulin_cost','pharmacies','pricing','drugs','data_info'] loop
    execute format('alter table %I enable row level security', t);
    execute format('drop policy if exists public_read on %I', t);
    execute format('create policy public_read on %I for select to anon using (true)', t);
  end loop; end $$;

create or replace function search_drugs(q text) returns setof drugs
language sql stable as $$
  select * from drugs
  where (coalesce(name,'')||' '||coalesce(generic_name,'')||' '||coalesce(brand_names,'')) ilike '%'||q||'%'
  order by similarity(coalesce(generic_name,name,''), q) desc limit 20 $$;
