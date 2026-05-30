-- Runs once on first Postgres init (mounted at
-- /docker-entrypoint-initdb.d/). The Postgres image already created the
-- router database (POSTGRES_DB=bp_router); this adds the SUITE database
-- and a dedicated role.
--
-- The router (bp_router DB) and the agent suite (bp_suite DB) live on the
-- same server as two databases. Today the suite connects as `postgres`
-- (see .env.prod.example); `bp_suite` is created NOLOGIN, reserved as the
-- owner for the future least-privilege wiring.
--
-- The role is created NOLOGIN ON PURPOSE: a login-capable role with a
-- placeholder password baked into this init script was a dormant
-- known-credential account reachable from the backend/suite networks that
-- no operator would think to rotate (nothing uses it yet). NOLOGIN can still
-- OWN the database; when the least-privilege wiring lands, grant login with a
-- real secret sourced from Docker/host secrets (never an init-script literal).

CREATE ROLE bp_suite NOLOGIN;
CREATE DATABASE bp_suite OWNER bp_suite;
