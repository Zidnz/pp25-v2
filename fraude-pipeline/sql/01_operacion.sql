-- Base de datos operativa (OLTP): la cola de alertas que trabaja el equipo de riesgos.
CREATE SCHEMA IF NOT EXISTS operacion;

CREATE TABLE IF NOT EXISTS operacion.alertas (
    alerta_id          BIGSERIAL PRIMARY KEY,
    transaction_id     BIGINT      NOT NULL UNIQUE,
    dia                DOUBLE PRECISION NOT NULL,
    monto              NUMERIC(12,3) NOT NULL CHECK (monto > 0),
    producto           CHAR(1),
    cliente            TEXT,
    prob               DOUBLE PRECISION NOT NULL CHECK (prob BETWEEN 0 AND 1),
    perdida_esperada   DOUBLE PRECISION NOT NULL,
    estado             TEXT NOT NULL DEFAULT 'pendiente'
                       CHECK (estado IN ('pendiente', 'en_revision', 'fraude_confirmado', 'descartada')),
    id_lote            INT,
    accion_sugerida    TEXT,
    explicacion        TEXT,
    casos_similares    TEXT,
    creada_en          TIMESTAMPTZ NOT NULL DEFAULT now(),
    actualizada_en     TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- La cola se atiende por pérdida esperada, de mayor a menor
CREATE INDEX IF NOT EXISTS ix_alertas_cola ON operacion.alertas (estado, perdida_esperada DESC);
CREATE INDEX IF NOT EXISTS ix_alertas_cliente ON operacion.alertas (cliente);
