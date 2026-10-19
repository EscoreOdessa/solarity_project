-- Початкові дані: постачальники та правила ціни (перенесено з констант price_finder.py)
set search_path = escore, public;

insert into suppliers (code, label, file_regex, note) values
  ('eti',    'ETI/ДС-Електро',  '^eti',              'Ціна в грн з ПДВ'),
  ('chint',  'CHINT',           'chint',             'Знижка вже врахована у файлі'),
  ('bdk',    'Прайс БДК (DKC)', '^(bdk|бдк|dkc)',    'Рекомендована ціна БЕЗ ПДВ'),
  ('kablex', 'Kablex',          '^(kablex|каблекс)', 'Частина цін у EUR/USD, знижка за групою'),
  ('kopos',  'KOPOS (Ресурс Електро)', '^(kopos|копос)', 'EUR/грн БЕЗ ПДВ за 1000 шт/м, курс з файлу');

insert into pricing_rules (supplier_id, group_match, discount, vat_multiplier, priority, note)
select s.id, r.grp, r.disc, r.vat, r.prio, r.note
from (values
  ('eti',    null,              0.25, 1.0, 0, 'ETI_DISCOUNT'),
  ('chint',  null,              0.00, 1.0, 0, 'CHINT_DISCOUNT'),
  ('kopos',  null,              0.15, 1.2, 0, 'KOPOS: знижка 15%, ×1.2 ПДВ'),
  ('bdk',    null,              0.00, 1.2, 0, 'BDK_DISCOUNT, BDK_VAT'),
  ('kablex', null,              0.00, 1.0, 0, 'група без налаштованої знижки → на перевірку'),
  ('kablex', 'кабель',          0.37, 1.0, 10, null),
  ('kablex', 'hager',           0.30, 1.0, 10, null),
  ('kablex', 'schneider',       0.25, 1.0, 10, null),
  ('kablex', 'копос',           0.25, 1.0, 10, null),
  ('kablex', 'kopos',           0.25, 1.0, 10, null),
  ('kablex', 'реле напряжения', 0.25, 1.0, 10, 'ZUBR'),
  ('kablex', 'zubr',            0.25, 1.0, 10, null)
) as r(sup, grp, disc, vat, prio, note)
join suppliers s on s.code = r.sup;
