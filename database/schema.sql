-- SwingEngine PostgreSQL database and schema.
--
-- Normally apply this through scripts/setup_database.sh. Direct psql usage is
-- retained for environments that do not use the local postgres system account.
--
-- Run this file with psql while connected to an administrative/maintenance
-- database (normally "postgres"). The connected role must have CREATEDB for
-- the first run. If swingengine_owner is supplied, that role must already
-- exist and the connected role must be allowed to SET ROLE to it.
--
-- Examples:
--   psql -X --dbname="$POSTGRES_ADMIN_URL" --file=database/schema.sql
--   psql -X --dbname="$POSTGRES_ADMIN_URL" \
--     --variable=swingengine_database=swingengine \
--     --variable=swingengine_owner=swingengine_app \
--     --file=database/schema.sql
--
-- The defaults are:
--   swingengine_database = swingengine
--   swingengine_owner    = the role running this script

\set ON_ERROR_STOP on

\if :{?swingengine_database}
\else
  \set swingengine_database swingengine
\endif

\if :{?swingengine_owner}
\else
  SELECT current_user AS swingengine_owner
  \gset
\endif

-- Optional: the runtime role the application connects as (see README.md
-- "PostgreSQL database"). When set, it is granted access to every table
-- below so new tables do not silently lack runtime privileges.

-- CREATE DATABASE cannot run inside a transaction. \gexec executes the
-- generated statement only when the requested database does not yet exist.
SELECT format(
    'CREATE DATABASE %I OWNER %I',
    :'swingengine_database',
    :'swingengine_owner'
)
WHERE NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_database
    WHERE datname = :'swingengine_database'
)
\gexec

-- Reuse the current host, port, user, password, and SSL settings.
\connect :swingengine_database

BEGIN;

-- Do not wait forever for locks in production.
SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '5min';

-- Fail rather than allowing two deployments to modify this schema at once.
DO $$
BEGIN
    IF NOT pg_try_advisory_xact_lock(
        hashtextextended('swingengine:public-schema', 0)
    ) THEN
        RAISE EXCEPTION
            'Another SwingEngine schema deployment is already running';
    END IF;
END
$$;

SET ROLE :"swingengine_owner";

-- Upgrade the original tracker/tracker_details structure without discarding
-- tracked assets or their state. Renaming the tables and columns also updates
-- the foreign key references maintained by PostgreSQL.
DO $$
BEGIN
    IF to_regclass('public.tracker_details') IS NOT NULL THEN
        IF to_regclass('public.tracker') IS NULL THEN
            RAISE EXCEPTION
                'Cannot migrate tracker_details: tracker does not exist';
        END IF;

        IF to_regclass('public.assets') IS NOT NULL THEN
            RAISE EXCEPTION
                'Cannot migrate legacy tracker: assets already exists';
        END IF;

        ALTER TABLE public.tracker RENAME TO assets;
        ALTER TABLE public.assets RENAME COLUMN tracker_id TO asset_id;
        ALTER TABLE public.assets
            RENAME COLUMN asset_symbol TO trading_symbol;
        ALTER TABLE public.assets
            RENAME CONSTRAINT tracker_pkey TO assets_pkey;
        ALTER SEQUENCE IF EXISTS public.tracker_tracker_id_seq
            RENAME TO assets_asset_id_seq;

        ALTER TABLE public.tracker_details RENAME TO tracker;
        ALTER TABLE public.tracker RENAME COLUMN tracker_id TO asset_id;
        ALTER TABLE public.tracker
            RENAME CONSTRAINT tracker_details_pkey TO tracker_pkey;
        ALTER TABLE public.tracker
            RENAME CONSTRAINT tracker_details_tracker_fk TO tracker_asset_fk;
        ALTER TABLE public.tracker
            RENAME CONSTRAINT tracker_details_amount_allocated_nonnegative
            TO tracker_amount_allocated_nonnegative;
        ALTER SEQUENCE IF EXISTS
            public.tracker_details_tracker_details_id_seq
            RENAME TO tracker_tracker_details_id_seq;
        ALTER INDEX IF EXISTS public.tracker_details_tracker_id_idx
            RENAME TO tracker_asset_id_idx;
    END IF;
END
$$;

-- Keep existing tracker state while adopting trade-oriented column names.
DO $$
BEGIN
    IF to_regclass('public.tracker') IS NOT NULL THEN
        IF EXISTS (
            SELECT 1
            FROM pg_attribute
            WHERE attrelid = 'public.tracker'::regclass
              AND attname = 'is_order_created'
              AND NOT attisdropped
        ) THEN
            IF EXISTS (
                SELECT 1
                FROM pg_attribute
                WHERE attrelid = 'public.tracker'::regclass
                  AND attname = 'is_trade_created'
                  AND NOT attisdropped
            ) THEN
                RAISE EXCEPTION
                    'Cannot rename tracker.is_order_created: '
                    'tracker.is_trade_created already exists';
            END IF;
            ALTER TABLE public.tracker
                RENAME COLUMN is_order_created TO is_trade_created;
        END IF;

        IF EXISTS (
            SELECT 1
            FROM pg_attribute
            WHERE attrelid = 'public.tracker'::regclass
              AND attname = 'is_approved_for_order'
              AND NOT attisdropped
        ) THEN
            IF EXISTS (
                SELECT 1
                FROM pg_attribute
                WHERE attrelid = 'public.tracker'::regclass
                  AND attname = 'is_approved_for_trade'
                  AND NOT attisdropped
            ) THEN
                RAISE EXCEPTION
                    'Cannot rename tracker.is_approved_for_order: '
                    'tracker.is_approved_for_trade already exists';
            END IF;
            ALTER TABLE public.tracker
                RENAME COLUMN is_approved_for_order
                TO is_approved_for_trade;
        END IF;
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS public.assets (
    asset_id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    asset_name TEXT NOT NULL,
    trading_symbol TEXT NOT NULL,
    instrument_key TEXT,
    has_fno BOOLEAN NOT NULL DEFAULT FALSE
);

-- CREATE TABLE IF NOT EXISTS does not add columns during an upgrade.
ALTER TABLE public.assets
    ADD COLUMN IF NOT EXISTS instrument_key TEXT;
ALTER TABLE public.assets
    ADD COLUMN IF NOT EXISTS has_fno BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS public.tracker (
    tracker_details_id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    asset_id BIGINT NOT NULL,
    has_momentum BOOLEAN NOT NULL DEFAULT FALSE,
    is_trade_created BOOLEAN NOT NULL DEFAULT FALSE,
    is_approved_for_trade BOOLEAN NOT NULL DEFAULT FALSE,
    amount_allocated DOUBLE PRECISION NOT NULL DEFAULT 0,
    added_date DATE NOT NULL DEFAULT CURRENT_DATE,
    has_fno BOOLEAN NOT NULL DEFAULT FALSE,
    side TEXT,
    CONSTRAINT tracker_asset_fk
        FOREIGN KEY (asset_id)
        REFERENCES public.assets (asset_id),
    CONSTRAINT tracker_amount_allocated_nonnegative
        CHECK (amount_allocated >= 0),
    CONSTRAINT tracker_side_valid_values
        CHECK (side IS NULL OR side IN ('buy', 'sell'))
);

ALTER TABLE public.tracker
    ADD COLUMN IF NOT EXISTS added_date
        DATE NOT NULL DEFAULT CURRENT_DATE;
ALTER TABLE public.tracker
    ADD COLUMN IF NOT EXISTS has_fno BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE public.tracker
    ADD COLUMN IF NOT EXISTS side TEXT;

-- CREATE TABLE IF NOT EXISTS does not add constraints during an upgrade.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.tracker'::regclass
          AND conname = 'tracker_side_valid_values'
    ) THEN
        ALTER TABLE public.tracker
            ADD CONSTRAINT tracker_side_valid_values
            CHECK (side IS NULL OR side IN ('buy', 'sell'));
    END IF;
END
$$;

-- Commands address both tables by trading symbol, so keep symbols and tracker
-- membership unambiguous.
CREATE UNIQUE INDEX IF NOT EXISTS assets_trading_symbol_unique_idx
    ON public.assets (upper(trading_symbol));
CREATE UNIQUE INDEX IF NOT EXISTS tracker_asset_id_unique_idx
    ON public.tracker (asset_id);

-- PostgreSQL does not automatically index the referencing side of a foreign
-- key. This index keeps asset lookups and parent-row checks efficient.
CREATE INDEX IF NOT EXISTS tracker_asset_id_idx
    ON public.tracker (asset_id);

COMMENT ON TABLE public.assets IS
    'Assets monitored by SwingEngine.';
COMMENT ON TABLE public.tracker IS
    'Momentum, trade, approval, and allocation state for tracked assets.';

-- A tracker asset approved for trading produces one trade, which in turn
-- produces a limit entry order and, once that fills, a two-leg GTT
-- (target + stoploss) exit order. See public.trade_order below.
CREATE TABLE IF NOT EXISTS public.trade (
    trade_id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    asset_id BIGINT NOT NULL,
    tracker_details_id BIGINT,
    asset_name TEXT NOT NULL,
    asset_description TEXT,
    trading_symbol TEXT NOT NULL,
    instrument_key TEXT,
    side TEXT NOT NULL,
    allocated_amount DOUBLE PRECISION NOT NULL,
    is_future_trade BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ,
    CONSTRAINT trade_asset_fk
        FOREIGN KEY (asset_id)
        REFERENCES public.assets (asset_id),
    CONSTRAINT trade_tracker_fk
        FOREIGN KEY (tracker_details_id)
        REFERENCES public.tracker (tracker_details_id),
    CONSTRAINT trade_side_valid_values
        CHECK (side IN ('buy', 'sell')),
    CONSTRAINT trade_status_valid_values
        CHECK (status IN ('open', 'closed')),
    CONSTRAINT trade_allocated_amount_nonnegative
        CHECK (allocated_amount >= 0)
);

CREATE INDEX IF NOT EXISTS trade_asset_id_idx
    ON public.trade (asset_id);
CREATE INDEX IF NOT EXISTS trade_tracker_details_id_idx
    ON public.trade (tracker_details_id);
CREATE INDEX IF NOT EXISTS trade_status_idx
    ON public.trade (status);

-- Broker orders placed for a trade. A trade normally has exactly two rows:
-- order_type='limit' for the entry, and order_type='gtt' for the exit
-- (Kite's two-leg/OCO GTT bundles target and stoploss into one trigger, so
-- both prices live on a single row keyed by that trigger's broker_order_id).
CREATE TABLE IF NOT EXISTS public.trade_order (
    order_id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    trade_id BIGINT NOT NULL,
    broker_order_id TEXT,
    order_type TEXT NOT NULL,
    transaction_type TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price DOUBLE PRECISION,
    target_price DOUBLE PRECISION,
    stoploss_price DOUBLE PRECISION,
    exit_price DOUBLE PRECISION,
    status TEXT NOT NULL DEFAULT 'pending',
    broker_status TEXT,
    error_message TEXT,
    placed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_checked_at TIMESTAMPTZ,
    executed_at TIMESTAMPTZ,
    CONSTRAINT trade_order_trade_fk
        FOREIGN KEY (trade_id)
        REFERENCES public.trade (trade_id),
    CONSTRAINT trade_order_type_valid_values
        CHECK (order_type IN ('limit', 'gtt')),
    CONSTRAINT trade_order_transaction_type_valid_values
        CHECK (transaction_type IN ('buy', 'sell')),
    CONSTRAINT trade_order_status_valid_values
        CHECK (
            status IN ('pending', 'complete', 'cancelled', 'rejected', 'expired')
        ),
    CONSTRAINT trade_order_quantity_positive
        CHECK (quantity > 0)
);

CREATE INDEX IF NOT EXISTS trade_order_trade_id_idx
    ON public.trade_order (trade_id);
CREATE INDEX IF NOT EXISTS trade_order_status_idx
    ON public.trade_order (status);

-- Broker order/trigger ids are unique when present; a row may be briefly
-- NULL between placing the API call and recording the returned id.
CREATE UNIQUE INDEX IF NOT EXISTS trade_order_broker_order_id_unique_idx
    ON public.trade_order (broker_order_id)
    WHERE broker_order_id IS NOT NULL;

COMMENT ON TABLE public.trade IS
    'One trade opened per tracker asset approved for trading.';
COMMENT ON TABLE public.trade_order IS
    'Broker orders (limit entry, GTT target/stoploss exit) placed for a trade.';

-- Grant the runtime application role access to every table and identity
-- sequence. Re-list new tables/sequences here as they are added so runtime
-- privileges never lag behind the schema again.
\if :{?swingengine_app_role}
GRANT SELECT, INSERT, UPDATE, DELETE
    ON public.assets, public.tracker, public.trade, public.trade_order
    TO :"swingengine_app_role";
GRANT USAGE, SELECT ON SEQUENCE
    public.assets_asset_id_seq,
    public.tracker_tracker_details_id_seq,
    public.trade_trade_id_seq,
    public.trade_order_order_id_seq
    TO :"swingengine_app_role";
\endif

RESET ROLE;

COMMIT;

\echo 'SwingEngine database schema is up to date.'
