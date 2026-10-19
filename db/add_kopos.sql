-- Новий постачальник KOPOS (Ресурс Електро). Разово, у Supabase → SQL Editor.
-- Повторний запуск безпечний.
set search_path = escore, public;

insert into suppliers (code, label, file_regex, note) values
  ('kopos', 'KOPOS (Ресурс Електро)', '^(kopos|копос)',
   'Ціни в EUR (частина в грн) БЕЗ ПДВ за 1000 шт/м; курс — з файлу постачальника')
on conflict (code) do nothing;

insert into pricing_rules (supplier_id, group_match, discount, vat_multiplier, priority, note)
select s.id, null, 0.15, 1.2, 0, 'KOPOS: знижка 15%, ×1.2 ПДВ'
from suppliers s
where s.code = 'kopos'
  and not exists (select 1 from pricing_rules p where p.supplier_id = s.id);
