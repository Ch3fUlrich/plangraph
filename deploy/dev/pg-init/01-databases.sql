-- Dev only: one superuser (POSTGRES_USER from deploy/dev/.env) owns these
-- databases; they default to that user as owner. Do not reuse in production.
CREATE DATABASE keeper;
CREATE DATABASE temporal;
CREATE DATABASE temporal_visibility;
