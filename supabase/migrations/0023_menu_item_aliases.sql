-- ---------------------------------------------------------------------------
-- 0023 — Menu item aliases: the short names bills print for long menu names
--
-- Petpooja prints "Donburi Chicken" on a bill for the item its menu calls
-- "Chicken Karaage Donburi". The Order Listing (D21) carries the bill form;
-- the Item Wise report (D29) carries the menu form; the per-bill attach rate
-- joins the two and lost every bill where they differ. This is the same
-- problem stock sheets had with catalogue names, and the same shape of fix
-- as inventory_item_aliases (0015): one spelling maps to exactly one item,
-- entered once by a manager, remembered forever.
--
-- Brand-level like menu_items. Matching is case-insensitive, enforced by the
-- unique index on lower(alias) so two people cannot map one spelling two ways.
-- ---------------------------------------------------------------------------

create table menu_item_aliases (
    id            uuid primary key default gen_random_uuid(),
    menu_item_id  uuid not null references menu_items (id) on delete cascade,
    alias         text not null,
    created_by    uuid references profiles (id) on delete set null,
    created_at    timestamptz not null default now(),

    constraint menu_item_aliases_alias_not_blank check (length(trim(alias)) > 0)
);

comment on table menu_item_aliases is
    'A name a bill prints for a menu item, mapped once. Matched case-insensitively when joining sales_order_items to menu_items.';

-- One spelling maps to exactly one item, or the memory is worthless.
create unique index menu_item_aliases_alias_uq on menu_item_aliases (lower(alias));
create index menu_item_aliases_item_idx on menu_item_aliases (menu_item_id);

alter table menu_item_aliases enable row level security;
alter table menu_item_aliases force row level security;
revoke all on table menu_item_aliases from anon;
grant select on table menu_item_aliases to authenticated;
create policy menu_item_aliases_read on menu_item_aliases for select to authenticated using (true);
