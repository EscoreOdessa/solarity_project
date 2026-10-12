-- ESCORE — база прайсів, довідника та зіставлень (етап 1)
-- PostgreSQL 15+ (Supabase). Ідемпотентно: можна запускати повторно на порожній схемі.

create schema if not exists escore;
set search_path = escore, public;

-- ── Постачальники ───────────────────────────────────────────────
create table suppliers (
  id          smallserial primary key,
  code        text not null unique,          -- eti | chint | bdk | kablex (як у price_finder.py)
  label       text not null,                 -- як пишеться у колонці C довідника
  file_regex  text not null,                 -- як знайти файл у папці Drive
  note        text
);

-- ── Завантаження прайсу (один файл = один запуск) ───────────────
create table import_runs (
  id            serial primary key,
  supplier_id   smallint not null references suppliers,
  drive_file_id text,
  file_name     text not null,
  price_date    date,                         -- дата прайсу (з назви / «Дійсний з»)
  rates         jsonb not null default '{}',  -- курси на день завантаження: {"EUR": 50.5, "USD": 45.1}
  rates_source  text,                         -- НБУ / резервні значення
  rows_parsed   int,
  status        text not null default 'ok' check (status in ('ok','failed')),
  error         text,
  loaded_at     timestamptz not null default now()
);

-- ── Позиція прайсу як є ─────────────────────────────────────────
create table supplier_items (
  id            serial primary key,
  supplier_id   smallint not null references suppliers,
  article       text,                         -- артикул як у прайсі
  article_norm  text,                         -- нормалізований (norm_code)
  name          text not null,                -- назва як у прайсі
  group_path    text,                         -- група/серія з рядків-заголовків прайсу
  unit          text,
  first_run_id  int references import_runs,
  last_run_id   int references import_runs,   -- якщо < останнього запуску — позиція зникла з прайсу
  -- ключ: артикул, а якщо його немає — нормалізована назва
  item_key      text generated always as (coalesce(article_norm, 'name:' || lower(name))) stored,
  unique (supplier_id, item_key)
);
create index on supplier_items (supplier_id, article_norm);

-- ── Ціна на дату (історія, не перезаписується) ──────────────────
create table price_snapshots (
  supplier_item_id int not null references supplier_items on delete cascade,
  import_run_id    int not null references import_runs on delete cascade,
  base_price       numeric(14,4),            -- ціна з прайсу до знижки; null = «за запитом»
  currency         char(3) not null default 'UAH',
  vat_included     boolean not null,
  status_raw       text,                     -- як у прайсі: Так/Ні, N/A, NEW, «виводиться»…
  status           text not null default 'unknown'
                   check (status in ('in_stock','on_order','unavailable','new','discontinued','unknown')),
  primary key (supplier_item_id, import_run_id)
);

-- ── Правила ціни (замість констант у коді) ──────────────────────
create table pricing_rules (
  id             serial primary key,
  supplier_id    smallint not null references suppliers,
  group_match    text,                       -- підрядок у group_path; null = весь постачальник
  discount       numeric(5,4) not null default 0,   -- 0.25 = −25%
  vat_multiplier numeric(5,4) not null default 1,   -- 1.2, якщо ціна в прайсі без ПДВ
  priority       int not null default 0,     -- вищий виграє (правило групи > правило постачальника)
  valid_from     date not null default current_date,
  note           text
);

-- ── Довідник (те, що зараз на вкладці «Довідник обладнання та цін») ──
create table reference_items (
  id              serial primary key,
  grp             text not null,             -- A: Група
  subgroup        text,                      -- F: Підгрупа
  subgroup2       text,                      -- G
  name            text not null,             -- B: Найменування
  unit            text,                      -- од. виміру позиції в кошторисі
  code_1c         text,                      -- код номенклатури 1С (для пункту 4)
  manual_price    numeric(14,2),             -- ціна вручну (для позицій без прив'язки)
  manual_price_at date,
  sheet_row       int,                       -- рядок у шаблоні (для синхронізації, поки потрібно)
  active          boolean not null default true,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

-- ── Зіставлення довідник ↔ прайс ────────────────────────────────
create table item_mappings (
  id                serial primary key,
  reference_item_id int not null references reference_items on delete cascade,
  supplier_item_id  int not null references supplier_items on delete cascade,
  method            text not null check (method in
                    ('manual_j','article_e','article_in_name','exact_name','rename_1c','fuzzy')),
  status            text not null default 'candidate'
                    check (status in ('candidate','confirmed','rejected')),
  is_primary        boolean not null default false,  -- з якого постачальника брати ціну
  similarity        numeric(5,2),                    -- для кандидатів, %
  unit_factor       numeric(12,4) not null default 1, -- од. довідника = factor × од. прайсу (бухта/метр, упаковка/шт)
  confirmed_at      timestamptz,
  note              text,
  unique (reference_item_id, supplier_item_id)
);
create unique index one_primary_per_item on item_mappings (reference_item_id)
  where is_primary and status = 'confirmed';

-- ════════════════════════════════════════════════════════════════
-- ПОДАННЯ
-- ════════════════════════════════════════════════════════════════

-- Остання ціна кожної позиції прайсу в грн з урахуванням правил
create view v_current_prices as
with last_snap as (
  select distinct on (ps.supplier_item_id) ps.*, r.rates, r.price_date, r.file_name
  from price_snapshots ps join import_runs r on r.id = ps.import_run_id
  where r.status = 'ok'
  order by ps.supplier_item_id, r.loaded_at desc
)
select si.id as supplier_item_id, s.code as supplier, s.label as supplier_label,
       si.article, si.name, si.group_path,
       ls.base_price, ls.currency, ls.status, ls.status_raw, ls.price_date, ls.file_name,
       coalesce((ls.rates ->> ls.currency)::numeric, 1)            as rate,
       coalesce(pr.discount, 0)                                    as discount,
       coalesce(pr.vat_multiplier, 1)                              as vat_multiplier,
       round(ls.base_price * coalesce((ls.rates ->> ls.currency)::numeric, 1)
             * (1 - coalesce(pr.discount, 0)) * coalesce(pr.vat_multiplier, 1), 2) as price_uah
from supplier_items si
join suppliers s on s.id = si.supplier_id
join last_snap ls on ls.supplier_item_id = si.id
left join lateral (
  select discount, vat_multiplier from pricing_rules p
  where p.supplier_id = si.supplier_id and p.valid_from <= current_date
    and (p.group_match is null or si.group_path ilike '%' || p.group_match || '%')
  order by p.priority desc, p.valid_from desc limit 1
) pr on true;

-- Ціна для кожної позиції довідника: з прайсу (основна прив'язка) або вручну
create view v_reference_prices as
select ri.id, ri.sheet_row, ri.grp, ri.subgroup, ri.name,
       cp.supplier_label, cp.article,
       coalesce(round(cp.price_uah * m.unit_factor, 2), ri.manual_price) as price,
       case when cp.price_uah is not null then 'price_list'
            when ri.manual_price is not null then 'manual'
            else 'none' end as price_source,
       case when cp.price_uah is not null then
              to_char(cp.price_date, 'DD.MM.YYYY') || ' | ' || cp.supplier_label || ' | арт. ' || cp.article
              || ' | ' || round(cp.base_price, 2) || case when cp.currency <> 'UAH' then ' ' || cp.currency else '' end || case when cp.discount > 0 then ' −' || round(cp.discount*100) || '%' else '' end
              || ' = ' || round(cp.price_uah * m.unit_factor, 2)
            when ri.manual_price is not null then 'вручну ' || coalesce(to_char(ri.manual_price_at,'DD.MM.YYYY'),'')
       end as source_note                                         -- колонка K
from reference_items ri
left join item_mappings m on m.reference_item_id = ri.id and m.is_primary and m.status = 'confirmed'
left join v_current_prices cp on cp.supplier_item_id = m.supplier_item_id
where ri.active;

-- Черга «На перевірку»
create view v_review as
select ri.id, ri.sheet_row, ri.grp, ri.name, 'Немає прив''язки до прайсу' as reason
from reference_items ri
where ri.active and not exists (
  select 1 from item_mappings m where m.reference_item_id = ri.id and m.status = 'confirmed')
union all
select ri.id, ri.sheet_row, ri.grp, ri.name, 'Позиція зникла з прайсу ' || s.label
from reference_items ri
join item_mappings m on m.reference_item_id = ri.id and m.is_primary and m.status = 'confirmed'
join supplier_items si on si.id = m.supplier_item_id
join suppliers s on s.id = si.supplier_id
where si.last_run_id < (select max(id) from import_runs r where r.supplier_id = si.supplier_id and r.status = 'ok');
