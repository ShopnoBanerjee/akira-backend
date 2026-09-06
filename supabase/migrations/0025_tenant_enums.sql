-- ---------------------------------------------------------------------------
-- 0025 — Enum values for multi-tenancy (P26a, D33)
--
-- On their own, because Postgres will not let a new enum value be USED in the
-- transaction that adds it. The migration runner applies each file in its own
-- transaction, so 0026 can rely on these.
--
--   user_role.platform_admin   the person who creates organisations; belongs
--                              to none of them
--   audit_action.read          a platform admin's read inside an organisation
--                              is recorded, so the organisation can see it
--   setting_scope.organisation what 'global' meant when there was one brand
-- ---------------------------------------------------------------------------

alter type user_role add value if not exists 'platform_admin';
alter type audit_action add value if not exists 'read';
alter type setting_scope add value if not exists 'organisation';
