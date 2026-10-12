-- Разово: прибрати повторні завантаження того самого файлу прайсу.
-- Лишається перше завантаження кожного файлу; історія цін повторних запусків видаляється (вона ідентична).
begin;

create temp table _keep on commit drop as
select id as dup_id,
       min(id) over (partition by supplier_id, file_name) as keep_id
from escore.import_runs
where status = 'ok';

update escore.supplier_items si set last_run_id = k.keep_id
from _keep k where si.last_run_id = k.dup_id and k.dup_id <> k.keep_id;

update escore.supplier_items si set first_run_id = k.keep_id
from _keep k where si.first_run_id = k.dup_id and k.dup_id <> k.keep_id;

delete from escore.import_runs r
using _keep k where r.id = k.dup_id and k.dup_id <> k.keep_id;   -- price_snapshots видаляються каскадом

commit;

select r.id, s.label, r.file_name, r.price_date,
       (select count(*) from escore.price_snapshots p where p.import_run_id = r.id) as prices
from escore.import_runs r join escore.suppliers s on s.id = r.supplier_id
order by r.id;
