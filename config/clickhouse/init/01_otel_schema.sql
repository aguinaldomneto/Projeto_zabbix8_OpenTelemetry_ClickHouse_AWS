-- Tabelas de telemetria (OpenTelemetry) lidas e gravadas pelo Zabbix 8.0.
--
-- O Zabbix 8.0.0rc1 não traz script para criar estas tabelas. A ORDEM das
-- colunas abaixo é a mesma que o coletor do proxy usa ao gravar
-- (src/zabbix_proxy/apm/apm_dataset.c) e os nomes/tipos são os que o frontend
-- espera (ui/include/classes/data/CApmData.php). O proxy insere por posição
-- (INSERT ... FORMAT JSONCompactEachRow), então NÃO acrescente nem reordene
-- colunas sem conferir essas duas fontes.
--
-- Retenção: 7 dias (TTL), suficiente para laboratório e demo.

CREATE DATABASE IF NOT EXISTS zabbix;

CREATE TABLE IF NOT EXISTS zabbix.otel_traces
(
    Timestamp          DateTime64(9) CODEC(Delta, ZSTD(1)),
    TraceId            String CODEC(ZSTD(1)),
    SpanId             String CODEC(ZSTD(1)),
    ParentSpanId       String CODEC(ZSTD(1)),
    TraceState         String CODEC(ZSTD(1)),
    SpanName           LowCardinality(String) CODEC(ZSTD(1)),
    SpanKind           LowCardinality(String) CODEC(ZSTD(1)),
    ServiceName        LowCardinality(String) CODEC(ZSTD(1)),
    ResourceAttributes Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ScopeName          String CODEC(ZSTD(1)),
    ScopeVersion       String CODEC(ZSTD(1)),
    SpanAttributes     Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    Duration           Int64 CODEC(ZSTD(1)),
    StatusCode         LowCardinality(String) CODEC(ZSTD(1)),
    StatusMessage      String CODEC(ZSTD(1)),
    Events Nested
    (
        Timestamp  DateTime64(9),
        Name       LowCardinality(String),
        Attributes Map(LowCardinality(String), String)
    ) CODEC(ZSTD(1)),
    Links Nested
    (
        TraceId    String,
        SpanId     String,
        TraceState String,
        Attributes Map(LowCardinality(String), String)
    ) CODEC(ZSTD(1)),
    INDEX idx_trace_id TraceId TYPE bloom_filter(0.001) GRANULARITY 1
)
ENGINE = MergeTree()
PARTITION BY toDate(Timestamp)
ORDER BY (ServiceName, SpanName, toDateTime(Timestamp))
TTL toDateTime(Timestamp) + toIntervalDay(7)
SETTINGS ttl_only_drop_parts = 1;

CREATE TABLE IF NOT EXISTS zabbix.otel_logs
(
    Timestamp          DateTime64(9) CODEC(Delta, ZSTD(1)),
    TraceId            String CODEC(ZSTD(1)),
    SpanId             String CODEC(ZSTD(1)),
    TraceFlags         UInt8,
    SeverityText       LowCardinality(String) CODEC(ZSTD(1)),
    SeverityNumber     UInt8,
    ServiceName        LowCardinality(String) CODEC(ZSTD(1)),
    Body               String CODEC(ZSTD(1)),
    ResourceSchemaUrl  LowCardinality(String) CODEC(ZSTD(1)),
    ResourceAttributes Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ScopeSchemaUrl     LowCardinality(String) CODEC(ZSTD(1)),
    ScopeName          String CODEC(ZSTD(1)),
    ScopeVersion       LowCardinality(String) CODEC(ZSTD(1)),
    ScopeAttributes    Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    LogAttributes      Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    EventName          String CODEC(ZSTD(1)),
    INDEX idx_trace_id TraceId TYPE bloom_filter(0.001) GRANULARITY 1
)
ENGINE = MergeTree()
PARTITION BY toDate(Timestamp)
ORDER BY (ServiceName, toDateTime(Timestamp))
TTL toDateTime(Timestamp) + toIntervalDay(7)
SETTINGS ttl_only_drop_parts = 1;

CREATE TABLE IF NOT EXISTS zabbix.otel_metrics_gauge
(
    ResourceAttributes    Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ResourceSchemaUrl     String CODEC(ZSTD(1)),
    ScopeName             String CODEC(ZSTD(1)),
    ScopeVersion          String CODEC(ZSTD(1)),
    ScopeAttributes       Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ScopeDroppedAttrCount UInt32 CODEC(ZSTD(1)),
    ScopeSchemaUrl        String CODEC(ZSTD(1)),
    ServiceName           LowCardinality(String) CODEC(ZSTD(1)),
    MetricName            String CODEC(ZSTD(1)),
    MetricDescription     String CODEC(ZSTD(1)),
    MetricUnit            String CODEC(ZSTD(1)),
    Attributes            Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    StartTimeUnix         DateTime64(9) CODEC(Delta, ZSTD(1)),
    TimeUnix              DateTime64(9) CODEC(Delta, ZSTD(1)),
    Value                 Float64 CODEC(ZSTD(1)),
    Flags                 UInt32 CODEC(ZSTD(1)),
    Exemplars Nested
    (
        FilteredAttributes Map(LowCardinality(String), String),
        TimeUnix           DateTime64(9),
        Value              Float64,
        SpanId             String,
        TraceId            String
    ) CODEC(ZSTD(1))
)
ENGINE = MergeTree()
PARTITION BY toDate(TimeUnix)
ORDER BY (ServiceName, MetricName, Attributes, toUnixTimestamp64Nano(TimeUnix))
TTL toDateTime(TimeUnix) + toIntervalDay(7)
SETTINGS ttl_only_drop_parts = 1;

CREATE TABLE IF NOT EXISTS zabbix.otel_metrics_sum
(
    ResourceAttributes     Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ResourceSchemaUrl      String CODEC(ZSTD(1)),
    ScopeName              String CODEC(ZSTD(1)),
    ScopeVersion           String CODEC(ZSTD(1)),
    ScopeAttributes        Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ScopeDroppedAttrCount  UInt32 CODEC(ZSTD(1)),
    ScopeSchemaUrl         String CODEC(ZSTD(1)),
    ServiceName            LowCardinality(String) CODEC(ZSTD(1)),
    MetricName             String CODEC(ZSTD(1)),
    MetricDescription      String CODEC(ZSTD(1)),
    MetricUnit             String CODEC(ZSTD(1)),
    Attributes             Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    StartTimeUnix          DateTime64(9) CODEC(Delta, ZSTD(1)),
    TimeUnix               DateTime64(9) CODEC(Delta, ZSTD(1)),
    Value                  Float64 CODEC(ZSTD(1)),
    Flags                  UInt32 CODEC(ZSTD(1)),
    Exemplars Nested
    (
        FilteredAttributes Map(LowCardinality(String), String),
        TimeUnix           DateTime64(9),
        Value              Float64,
        SpanId             String,
        TraceId            String
    ) CODEC(ZSTD(1)),
    AggregationTemporality Int32 CODEC(ZSTD(1)),
    IsMonotonic            Bool CODEC(Delta, ZSTD(1))
)
ENGINE = MergeTree()
PARTITION BY toDate(TimeUnix)
ORDER BY (ServiceName, MetricName, Attributes, toUnixTimestamp64Nano(TimeUnix))
TTL toDateTime(TimeUnix) + toIntervalDay(7)
SETTINGS ttl_only_drop_parts = 1;

CREATE TABLE IF NOT EXISTS zabbix.otel_metrics_histogram
(
    ResourceAttributes     Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ResourceSchemaUrl      String CODEC(ZSTD(1)),
    ScopeName              String CODEC(ZSTD(1)),
    ScopeVersion           String CODEC(ZSTD(1)),
    ScopeAttributes        Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ScopeDroppedAttrCount  UInt32 CODEC(ZSTD(1)),
    ScopeSchemaUrl         String CODEC(ZSTD(1)),
    ServiceName            LowCardinality(String) CODEC(ZSTD(1)),
    MetricName             String CODEC(ZSTD(1)),
    MetricDescription      String CODEC(ZSTD(1)),
    MetricUnit             String CODEC(ZSTD(1)),
    Attributes             Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    StartTimeUnix          DateTime64(9) CODEC(Delta, ZSTD(1)),
    TimeUnix               DateTime64(9) CODEC(Delta, ZSTD(1)),
    Count                  UInt64 CODEC(Delta, ZSTD(1)),
    Sum                    Float64 CODEC(ZSTD(1)),
    BucketCounts           Array(UInt64) CODEC(ZSTD(1)),
    ExplicitBounds         Array(Float64) CODEC(ZSTD(1)),
    Exemplars Nested
    (
        FilteredAttributes Map(LowCardinality(String), String),
        TimeUnix           DateTime64(9),
        Value              Float64,
        SpanId             String,
        TraceId            String
    ) CODEC(ZSTD(1)),
    Flags                  UInt32 CODEC(ZSTD(1)),
    Min                    Float64 CODEC(ZSTD(1)),
    Max                    Float64 CODEC(ZSTD(1)),
    AggregationTemporality Int32 CODEC(ZSTD(1))
)
ENGINE = MergeTree()
PARTITION BY toDate(TimeUnix)
ORDER BY (ServiceName, MetricName, Attributes, toUnixTimestamp64Nano(TimeUnix))
TTL toDateTime(TimeUnix) + toIntervalDay(7)
SETTINGS ttl_only_drop_parts = 1;

CREATE TABLE IF NOT EXISTS zabbix.otel_metrics_exponential_histogram
(
    ResourceAttributes     Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ResourceSchemaUrl      String CODEC(ZSTD(1)),
    ScopeName              String CODEC(ZSTD(1)),
    ScopeVersion           String CODEC(ZSTD(1)),
    ScopeAttributes        Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ScopeDroppedAttrCount  UInt32 CODEC(ZSTD(1)),
    ScopeSchemaUrl         String CODEC(ZSTD(1)),
    ServiceName            LowCardinality(String) CODEC(ZSTD(1)),
    MetricName             String CODEC(ZSTD(1)),
    MetricDescription      String CODEC(ZSTD(1)),
    MetricUnit             String CODEC(ZSTD(1)),
    Attributes             Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    StartTimeUnix          DateTime64(9) CODEC(Delta, ZSTD(1)),
    TimeUnix               DateTime64(9) CODEC(Delta, ZSTD(1)),
    Count                  UInt64 CODEC(Delta, ZSTD(1)),
    Sum                    Float64 CODEC(ZSTD(1)),
    Scale                  Int32 CODEC(ZSTD(1)),
    ZeroCount              UInt64 CODEC(ZSTD(1)),
    PositiveOffset         Int32 CODEC(ZSTD(1)),
    PositiveBucketCounts   Array(UInt64) CODEC(ZSTD(1)),
    NegativeOffset         Int32 CODEC(ZSTD(1)),
    NegativeBucketCounts   Array(UInt64) CODEC(ZSTD(1)),
    Exemplars Nested
    (
        FilteredAttributes Map(LowCardinality(String), String),
        TimeUnix           DateTime64(9),
        Value              Float64,
        SpanId             String,
        TraceId            String
    ) CODEC(ZSTD(1)),
    Flags                  UInt32 CODEC(ZSTD(1)),
    Min                    Float64 CODEC(ZSTD(1)),
    Max                    Float64 CODEC(ZSTD(1)),
    AggregationTemporality Int32 CODEC(ZSTD(1))
)
ENGINE = MergeTree()
PARTITION BY toDate(TimeUnix)
ORDER BY (ServiceName, MetricName, Attributes, toUnixTimestamp64Nano(TimeUnix))
TTL toDateTime(TimeUnix) + toIntervalDay(7)
SETTINGS ttl_only_drop_parts = 1;

CREATE TABLE IF NOT EXISTS zabbix.otel_metrics_summary
(
    ResourceAttributes    Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ResourceSchemaUrl     String CODEC(ZSTD(1)),
    ScopeName             String CODEC(ZSTD(1)),
    ScopeVersion          String CODEC(ZSTD(1)),
    ScopeAttributes       Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ScopeDroppedAttrCount UInt32 CODEC(ZSTD(1)),
    ScopeSchemaUrl        String CODEC(ZSTD(1)),
    ServiceName           LowCardinality(String) CODEC(ZSTD(1)),
    MetricName            String CODEC(ZSTD(1)),
    MetricDescription     String CODEC(ZSTD(1)),
    MetricUnit            String CODEC(ZSTD(1)),
    Attributes            Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    StartTimeUnix         DateTime64(9) CODEC(Delta, ZSTD(1)),
    TimeUnix              DateTime64(9) CODEC(Delta, ZSTD(1)),
    Count                 UInt64 CODEC(Delta, ZSTD(1)),
    Sum                   Float64 CODEC(ZSTD(1)),
    ValueAtQuantiles Nested
    (
        Quantile Float64,
        Value    Float64
    ) CODEC(ZSTD(1)),
    Flags                 UInt32 CODEC(ZSTD(1))
)
ENGINE = MergeTree()
PARTITION BY toDate(TimeUnix)
ORDER BY (ServiceName, MetricName, Attributes, toUnixTimestamp64Nano(TimeUnix))
TTL toDateTime(TimeUnix) + toIntervalDay(7)
SETTINGS ttl_only_drop_parts = 1;
