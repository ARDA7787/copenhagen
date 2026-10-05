-- Runs once, on an empty data volume.
-- Two roles (plan §4.5):
--   copenhagen_owner: runs migrations, owns every table.
--   copenhagen_app:   the runtime role. Gets SELECT/INSERT (and only what each table needs);
--                     never UPDATE/DELETE/TRUNCATE on audit_events (I9).
-- Dev-only passwords. Production credentials come from the secret store.

CREATE ROLE copenhagen_owner LOGIN PASSWORD 'copenhagen_owner';
CREATE ROLE copenhagen_app LOGIN PASSWORD 'copenhagen_app';

CREATE DATABASE copenhagen OWNER copenhagen_owner;
CREATE DATABASE copenhagen_test OWNER copenhagen_owner;

\connect copenhagen
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO copenhagen_app;
GRANT ALL ON SCHEMA public TO copenhagen_owner;

\connect copenhagen_test
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO copenhagen_app;
GRANT ALL ON SCHEMA public TO copenhagen_owner;
