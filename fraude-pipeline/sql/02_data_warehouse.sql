-- Data warehouse (OLAP) en estrella. Grano del hecho: una transacción.
DROP SCHEMA IF EXISTS dw CASCADE;
CREATE SCHEMA dw;

CREATE TABLE dw.dim_tiempo (
    dia_id       INT PRIMARY KEY,          -- día desde la fecha de referencia de Vesta
    semana       INT NOT NULL,
    dia_semana   INT NOT NULL,
    mes_relativo INT NOT NULL,             -- bloques de 30 días
    periodo      TEXT NOT NULL             -- entreno / validacion / prueba
);
CREATE TABLE dw.dim_producto  (producto_id SERIAL PRIMARY KEY, codigo TEXT UNIQUE NOT NULL);
CREATE TABLE dw.dim_tarjeta   (tarjeta_id SERIAL PRIMARY KEY, red TEXT, tipo TEXT, UNIQUE (red, tipo));
CREATE TABLE dw.dim_canal     (canal_id SERIAL PRIMARY KEY, dispositivo TEXT, navegador TEXT, UNIQUE (dispositivo, navegador));
CREATE TABLE dw.dim_email     (email_id SERIAL PRIMARY KEY, dominio TEXT UNIQUE);

CREATE TABLE dw.fact_transaccion (
    transaction_id    BIGINT PRIMARY KEY,
    dia_id            INT REFERENCES dw.dim_tiempo,
    producto_id       INT REFERENCES dw.dim_producto,
    tarjeta_id        INT REFERENCES dw.dim_tarjeta,
    canal_id          INT REFERENCES dw.dim_canal,
    email_id          INT REFERENCES dw.dim_email,
    hora              SMALLINT,
    monto             NUMERIC(12,3),
    es_fraude         SMALLINT,
    prob_modelo       DOUBLE PRECISION,       -- solo validación y prueba
    perdida_esperada  DOUBLE PRECISION,
    alerta            BOOLEAN
);
CREATE INDEX ON dw.fact_transaccion (dia_id);
CREATE INDEX ON dw.fact_transaccion (producto_id);
