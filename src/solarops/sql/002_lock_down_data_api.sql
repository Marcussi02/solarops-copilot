-- Hosted Postgres providers such as Supabase publish the public schema through an
-- auto-generated REST API. This project only talks to the database through its own
-- Lambda functions, so switch that surface off: RLS with no policies denies API roles,
-- and their grants are revoked. The owning role used by the app is unaffected.
ALTER TABLE solar_units       ENABLE ROW LEVEL SECURITY;
ALTER TABLE scada_readings    ENABLE ROW LEVEL SECURITY;
ALTER TABLE weather_obs       ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingested_files    ENABLE ROW LEVEL SECURITY;
ALTER TABLE schema_migrations ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', r);
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM %I', r);
        END IF;
    END LOOP;
END $$;
