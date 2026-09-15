-- Run once in the Supabase SQL editor against the live database.
-- Adds salary tracking, sourced from the GraphQL specifics_info block.
-- Existing rows will have NULL for these columns until the next sync run.

alter table opportunities_snapshot
  add column salary numeric,
  add column salary_currency text,
  add column salary_periodicity text;

alter table opportunities_dim
  add column current_salary numeric,
  add column current_salary_currency text,
  add column current_salary_periodicity text;
