-- Auto OLAP SQL Server DDL
CREATE SCHEMA dw;

CREATE TABLE dw.dim_date (
    sk_date INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    date_day NVARCHAR(255) NULL,
    date_month BIGINT NULL,
    date_quarter BIGINT NULL,
    date_year BIGINT NULL
);
CREATE UNIQUE INDEX ux_dim_date_natural ON dw.dim_date (date_day);

CREATE TABLE dw.dim_geography (
    sk_geography INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    tienda NVARCHAR(255) NULL,
    ciudad NVARCHAR(255) NULL,
    region NVARCHAR(255) NULL,
    pais NVARCHAR(255) NULL
);
CREATE UNIQUE INDEX ux_dim_geography_natural ON dw.dim_geography (tienda);

CREATE TABLE dw.dim_product (
    sk_product INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    sku NVARCHAR(255) NULL,
    producto NVARCHAR(255) NULL,
    categoria NVARCHAR(255) NULL
);
CREATE UNIQUE INDEX ux_dim_product_natural ON dw.dim_product (sku);

CREATE TABLE dw.dim_customer (
    sk_customer INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    cliente_id NVARCHAR(255) NULL,
    segmento NVARCHAR(255) NULL
);
CREATE UNIQUE INDEX ux_dim_customer_natural ON dw.dim_customer (cliente_id);

CREATE TABLE dw.fact_central (
    fact_id BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    sk_date INT NOT NULL,
    sk_geography INT NOT NULL,
    sk_product INT NOT NULL,
    sk_customer INT NOT NULL,
    invoice_id NVARCHAR(255) NULL,
    ventas DECIMAL(19,4) NULL,
    costo DECIMAL(19,4) NULL,
    cantidad DECIMAL(19,4) NULL,
    inventario DECIMAL(19,4) NULL,
    margin_pct DECIMAL(19,4) NULL,
    CONSTRAINT fk_fact_central_dim_date FOREIGN KEY (sk_date) REFERENCES dw.dim_date(sk_date),
    CONSTRAINT fk_fact_central_dim_geography FOREIGN KEY (sk_geography) REFERENCES dw.dim_geography(sk_geography),
    CONSTRAINT fk_fact_central_dim_product FOREIGN KEY (sk_product) REFERENCES dw.dim_product(sk_product),
    CONSTRAINT fk_fact_central_dim_customer FOREIGN KEY (sk_customer) REFERENCES dw.dim_customer(sk_customer)
);

-- Aggregation awareness / pre-aggregation candidates
-- CREATE MATERIALIZED VIEW dw.agg_date_date_year AS SELECT date_year, SUM(ventas) AS ventas, SUM(costo) AS costo, SUM(cantidad) AS cantidad, SUM(inventario) AS inventario FROM dw.fact_central GROUP BY date_year;
-- CREATE MATERIALIZED VIEW dw.agg_geography_pais AS SELECT pais, SUM(ventas) AS ventas, SUM(costo) AS costo, SUM(cantidad) AS cantidad, SUM(inventario) AS inventario FROM dw.fact_central GROUP BY pais;
-- CREATE MATERIALIZED VIEW dw.agg_product_categoria AS SELECT categoria, SUM(ventas) AS ventas, SUM(costo) AS costo, SUM(cantidad) AS cantidad, SUM(inventario) AS inventario FROM dw.fact_central GROUP BY categoria;
-- CREATE MATERIALIZED VIEW dw.agg_customer_segmento AS SELECT segmento, SUM(ventas) AS ventas, SUM(costo) AS costo, SUM(cantidad) AS cantidad, SUM(inventario) AS inventario FROM dw.fact_central GROUP BY segmento;
-- Schema style requested: STAR