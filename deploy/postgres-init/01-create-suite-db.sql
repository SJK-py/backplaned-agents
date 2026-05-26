-- Runs once on first Postgres init (mounted at
-- /docker-entrypoint-initdb.d/). The Postgres image already created the
-- router database (POSTGRES_DB=bp_router); this adds the SUITE database
-- and a dedicated role.
--
-- The router (bp_router DB) and the agent suite (bp_suite DB) are owned
-- by different components and given separate credentials. Same server,
-- two databases. See docs/deployment.md.
--
-- NOTE: the suite role password here is a placeholder — override it (and
-- prefer Docker/host secrets over an init password) for real deploys.

CREATE ROLE bp_suite WITH LOGIN PASSWORD 'change-me-suite';
CREATE DATABASE bp_suite OWNER bp_suite;
