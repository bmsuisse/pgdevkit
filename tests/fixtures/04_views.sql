CREATE OR REPLACE VIEW myapp.active_users AS
SELECT id, email FROM myapp.users WHERE status = 'active';
