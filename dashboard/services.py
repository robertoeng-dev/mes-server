import json

from django.db import connection


TABLE_NAME = "mes_test_results"

PASS_RESULTS = ("PASS", "PASSED", "OK")
FAIL_RESULTS = ("FAIL", "FAILED", "NG", "NOK")
CHANNELS = list(range(1, 21))
DEFAULT_HOUR_START = 6
DEFAULT_HOUR_END = 23
HOT_LIMIT = 5

# Gate de sanidade dos dropdowns: começa com alfanumérico, depois
# alnum/espaço/._+#()/-, máx. 64 chars. Mantém 'BR-PCMTEST-03', 'A06';
# descarta lixo binário vindo de linhas corrompidas na origem.
SANE_FILTER_VALUE_RE = r"^[[:alnum:]][[:alnum:] ._+#()/-]*$"


def dictfetchall(cursor):
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def table_columns():
    sql = """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = %s
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, [TABLE_NAME])
        return {row[0] for row in cursor.fetchall()}


def get_json_column(cols):
    if "row_data" in cols:
        return "row_data"
    if "raw_data" in cols:
        return "raw_data"
    return None


def json_text(json_col, key):
    return f"{json_col} ->> '{key}'"


def json_text_clean(json_col, key, *bad_literals):
    """Extração de texto do JSON que trata '' e literais de cabeçalho
    conhecidos (ex.: valor 'Station' na chave 'Station') como NULL,
    para o próximo ramo do COALESCE vencer."""
    expr = f"NULLIF({json_text(json_col, key)}, '')"
    for lit in bad_literals:
        expr = f"NULLIF({expr}, '{lit}')"
    return expr


def safe_timestamp(expr):
    return f"""
        CASE
            WHEN {expr} ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}[ T]\\d{{2}}:\\d{{2}}:\\d{{2}}'
            THEN REPLACE(LEFT({expr}, 19), 'T', ' ')::timestamp
            WHEN {expr} ~ '^\\d{{2}}/\\d{{2}}/\\d{{4}} \\d{{2}}:\\d{{2}}:\\d{{2}}'
            THEN TO_TIMESTAMP({expr}, 'MM/DD/YYYY HH24:MI:SS')::timestamp
            ELSE NULL
        END
    """


def dedupe_doubled_scan(expr):
    """Corrige o leitor de código de barras disparando duas vezes: quando o
    valor é a mesma string colada duas vezes seguidas (ex.: 'A06T01A06T01'),
    mantém só a primeira metade ('A06T01') — sem isso, o mesmo carrier
    fragmenta em dois "carriers" diferentes nas estatísticas."""
    # %% escapado: psycopg2 faz substituição estilo % nos params do cursor,
    # então um '%' literal (módulo, aqui) precisa virar '%%' no texto do SQL.
    return f"""
        CASE
            WHEN length({expr}) > 0
                 AND length({expr}) %% 2 = 0
                 AND left({expr}, length({expr}) / 2) = right({expr}, length({expr}) / 2)
            THEN left({expr}, length({expr}) / 2)
            ELSE {expr}
        END
    """


def normalize_channel_expr(channel_expr):
    digits = f"NULLIF(regexp_replace(COALESCE({channel_expr}::text, ''), '[^0-9]', '', 'g'), '')"
    return f"""
        CASE
            WHEN {digits} IS NOT NULL AND ({digits})::int BETWEEN 1 AND 20
            THEN ({digits})::int
            ELSE NULL
        END
    """


def get_exprs():
    cols = table_columns()
    json_col = get_json_column(cols)
    created = "created_at" if "created_at" in cols else "NOW()"

    if json_col:
        station = f"""
            COALESCE(
                NULLIF({json_text(json_col, "station_id")}, ''),
                {json_text_clean(json_col, "Station", "Station")},
                NULLIF({json_text(json_col, "machine_no")}, ''),
                NULLIF({json_text(json_col, "device_name")}, '')
            )
        """

        model = f"""
            COALESCE(
                {json_text_clean(json_col, "Model", "Model")},
                {json_text_clean(json_col, "model", "model")},
                NULLIF({json_text(json_col, "proj_code")}, ''),
                {json_text_clean(json_col, "PN", "Material", "PN")}
            )
        """

        result = f"""
            COALESCE(
                NULLIF({json_text(json_col, "test_result")}, ''),
                NULLIF({json_text(json_col, "TestResult")}, ''),
                NULLIF({json_text(json_col, "result")}, ''),
                NULLIF({json_text(json_col, "status")}, '')
            )
        """

        event_time_text = f"""
            COALESCE(
                NULLIF({json_text(json_col, "test_time")}, ''),
                NULLIF({json_text(json_col, "TestTime")}, ''),
                NULLIF({json_text(json_col, "datetime")}, ''),
                NULLIF({json_text(json_col, "timestamp")}, '')
            )
        """

        failure = f"""
            COALESCE(
                NULLIF({json_text(json_col, "error_code")}, ''),
                NULLIF({json_text(json_col, "SC_ERR_CODE")}, ''),
                NULLIF({json_text(json_col, "SC_RESULT_CODE")}, ''),
                NULLIF({json_text(json_col, "error_msg")}, ''),
                NULLIF({json_text(json_col, "List of Failing Tests")}, ''),
                'UNKNOWN'
            )
        """

        channel = f"""
            COALESCE(
                NULLIF({json_text(json_col, "channel_no")}, ''),
                NULLIF({json_text(json_col, "Channel")}, ''),
                NULLIF({json_text(json_col, "CH")}, ''),
                NULLIF({json_text(json_col, "channel")}, ''),
                'N/A'
            )
        """

        # device_name NÃO entra aqui: é o serial da fixture de cada canal
        # (1:1 com o canal, ex.: PT3009293614), não o carrier. O carrier real
        # é o barcode escaneado (ex.: A06T10, A17RT1).
        #
        # Duas correções sobre o barcode bruto, para não perder estatística:
        # 1) leitura duplicada do scanner ('A06T01A06T01') colapsa para a
        #    primeira metida ('A06T01') via dedupe_doubled_scan();
        # 2) linha com resultado de teste real mas barcode vazio não cai
        #    silenciosamente em 'N/A' (que a matriz Carrier×Canal exclui) —
        #    fica marcada como 'SEM BARCODE', visível e agrupável à parte.
        carrier = f"""
            COALESCE(
                {dedupe_doubled_scan(f"NULLIF({json_text(json_col, 'barcode')}, '')")},
                NULLIF({json_text(json_col, "carrier")}, ''),
                NULLIF({json_text(json_col, "Carrier")}, ''),
                CASE WHEN {result} IS NOT NULL THEN 'SEM BARCODE' END,
                'N/A'
            )
        """
    else:
        station = "NULL"
        model = "NULL"
        result = "NULL"
        event_time_text = "NULL"
        failure = "'UNKNOWN'"
        channel = "'N/A'"
        carrier = "'N/A'"

    event_time = f"""
        COALESCE(
            {safe_timestamp(event_time_text)},
            {created}
        )
    """

    channel_no = normalize_channel_expr(channel)

    return {
        "station": station,
        "model": model,
        "result": result,
        "created": created,
        "event_time": event_time,
        "failure": failure,
        "channel": channel,
        "channel_no": channel_no,
        "carrier": carrier,
    }


def get_filters(request):
    return {
        "station": request.GET.get("station", "").strip(),
        "model": request.GET.get("model", "").strip(),
        "date_from": request.GET.get("date_from", "").strip(),
        "date_to": request.GET.get("date_to", "").strip(),
        "channel": request.GET.get("channel", "").strip(),
        "carrier": request.GET.get("carrier", "").strip(),
        "failure": request.GET.get("failure", "").strip(),
    }


def build_where(filters):
    exprs = get_exprs()
    where = []
    params = []

    if filters.get("station"):
        where.append(f"{exprs['station']} = %s")
        params.append(filters["station"])

    if filters.get("model"):
        where.append(f"{exprs['model']} = %s")
        params.append(filters["model"])

    if filters.get("date_from"):
        where.append(f"{exprs['event_time']} >= %s")
        params.append(filters["date_from"])

    if filters.get("date_to"):
        where.append(f"{exprs['event_time']} <= %s")
        params.append(filters["date_to"])

    if filters.get("channel"):
        try:
            channel_no = int(filters["channel"])
            where.append(f"{exprs['channel_no']} = %s")
            params.append(channel_no)
        except (TypeError, ValueError):
            pass

    if filters.get("carrier"):
        where.append(f"{exprs['carrier']} = %s")
        params.append(filters["carrier"])

    if filters.get("failure"):
        where.append(f"{exprs['failure']} = %s")
        params.append(filters["failure"])

    if not where:
        return "", params

    return "WHERE " + " AND ".join(where), params


def summary(filters):
    exprs = get_exprs()
    where_sql, params = build_where(filters)

    sql = f"""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE UPPER(COALESCE({exprs["result"]}::text, '')) IN %s) AS pass_count,
            COUNT(*) FILTER (WHERE UPPER(COALESCE({exprs["result"]}::text, '')) IN %s) AS fail_count,
            COALESCE(
                ROUND(
                    100.0 * COUNT(*) FILTER (WHERE UPPER(COALESCE({exprs["result"]}::text, '')) IN %s)
                    / NULLIF(COUNT(*), 0),
                    2
                ),
                0
            ) AS yield_percent,
            MAX({exprs["event_time"]}) AS last_event_time
        FROM {TABLE_NAME}
        {where_sql}
    """

    with connection.cursor() as cursor:
        cursor.execute(sql, [PASS_RESULTS, FAIL_RESULTS, PASS_RESULTS] + params)
        return dictfetchall(cursor)[0]


def top_failures(filters, limit=10):
    exprs = get_exprs()
    where_sql, params = build_where(filters)
    fail_condition = f"UPPER(COALESCE({exprs['result']}::text, '')) IN %s"

    sql = f"""
        SELECT
            {exprs["failure"]} AS failure,
            COUNT(*) AS total
        FROM {TABLE_NAME}
        {where_sql}
        {'AND' if where_sql else 'WHERE'} {fail_condition}
        GROUP BY failure
        ORDER BY total DESC
        LIMIT %s
    """

    with connection.cursor() as cursor:
        cursor.execute(sql, params + [FAIL_RESULTS, limit])
        return dictfetchall(cursor)


def hourly_yield(filters):
    exprs = get_exprs()
    where_sql, params = build_where(filters)

    sql = f"""
        WITH hours AS (
            SELECT generate_series(
                date_trunc('day', COALESCE(%s::timestamp, NOW())) + interval '{DEFAULT_HOUR_START} hour',
                date_trunc('day', COALESCE(%s::timestamp, NOW())) + interval '{DEFAULT_HOUR_END} hour',
                interval '1 hour'
            ) AS hour_ts
        ), base AS (
            SELECT
                date_trunc('hour', {exprs["event_time"]}) AS hour_ts,
                UPPER(COALESCE({exprs["result"]}::text, '')) AS result_text
            FROM {TABLE_NAME}
            {where_sql}
        )
        SELECT
            TO_CHAR(h.hour_ts, 'YYYY-MM-DD HH24:00') AS hour_label,
            TO_CHAR(h.hour_ts, 'HH24:00') AS hour_short,
            COALESCE(COUNT(*) FILTER (WHERE b.result_text IN %s), 0) AS pass_count,
            COALESCE(COUNT(*) FILTER (WHERE b.result_text IN %s), 0) AS fail_count,
            COALESCE(COUNT(b.*), 0) AS total,
            COALESCE(
                ROUND(
                    100.0 * COUNT(*) FILTER (WHERE b.result_text IN %s)
                    / NULLIF(COUNT(b.*), 0),
                    2
                ),
                0
            ) AS yield_percent
        FROM hours h
        LEFT JOIN base b ON b.hour_ts = h.hour_ts
        GROUP BY h.hour_ts
        ORDER BY h.hour_ts
    """

    date_base = filters.get("date_from") or None

    with connection.cursor() as cursor:
        cursor.execute(sql, [date_base, date_base] + params + [PASS_RESULTS, FAIL_RESULTS, PASS_RESULTS])
        return dictfetchall(cursor)


def channel_summary(filters):
    exprs = get_exprs()
    where_sql, params = build_where(filters)

    sql = f"""
        SELECT
            {exprs["channel_no"]} AS channel,
            COUNT(*) FILTER (WHERE UPPER(COALESCE({exprs["result"]}::text, '')) IN %s) AS pass_count,
            COUNT(*) FILTER (WHERE UPPER(COALESCE({exprs["result"]}::text, '')) IN %s) AS fail_count,
            COUNT(*) AS total,
            COALESCE(
                ROUND(
                    100.0 * COUNT(*) FILTER (WHERE UPPER(COALESCE({exprs["result"]}::text, '')) IN %s)
                    / NULLIF(COUNT(*), 0),
                    2
                ),
                0
            ) AS yield_percent
        FROM {TABLE_NAME}
        {where_sql}
        GROUP BY channel
        ORDER BY channel NULLS LAST
        LIMIT 100
    """

    with connection.cursor() as cursor:
        cursor.execute(sql, [PASS_RESULTS, FAIL_RESULTS, PASS_RESULTS] + params)
        return dictfetchall(cursor)


# Um ciclo = uma passagem do carrier pelo testador (rajada de ~20 registros
# com ≤3s entre si). Novo ciclo quando o intervalo desde o registro anterior
# do mesmo carrier excede este valor — 30s separa retestes consecutivos
# (~1 min entre passagens) sem quebrar uma rajada em dois ciclos.
CYCLE_GAP_SECONDS = 30


# Tabela PRÓPRIA do dashboard para gestão de vida útil dos carriers
# (mes_test_results continua somente leitura). baseline_at = instante em que
# o carrier foi instalado/substituído: os ciclos contam a partir dela.
CARRIER_TABLE = "dashboard_carriers"


def ensure_carrier_table():
    sql = f"""
        CREATE TABLE IF NOT EXISTS {CARRIER_TABLE} (
            carrier      TEXT PRIMARY KEY,
            cycle_limit  INTEGER NULL,
            baseline_at  TIMESTAMP NOT NULL DEFAULT TIMESTAMP '1970-01-01',
            updated_at   TIMESTAMP NOT NULL DEFAULT NOW(),
            notes        TEXT NULL
        )
    """
    with connection.cursor() as cursor:
        cursor.execute(sql)


def carrier_cycles():
    """Ciclos por carrier desde o baseline (instalação/última substituição).

    Sem filtro de período: é contador de vida útil. Carriers gerenciados que
    ainda não têm registros após o baseline aparecem com 0 ciclos."""
    ensure_carrier_table()
    exprs = get_exprs()

    sql = f"""
        WITH managed AS (
            SELECT carrier, cycle_limit, baseline_at, notes
            FROM {CARRIER_TABLE}
        ), r AS (
            SELECT
                {exprs["carrier"]} AS carrier,
                {exprs["event_time"]} AS event_time
            FROM {TABLE_NAME}
            WHERE {exprs["carrier"]} IS NOT NULL
              AND {exprs["carrier"]} <> ''
              AND {exprs["carrier"]} <> 'N/A'
        ), f AS (
            SELECT
                r.carrier,
                r.event_time,
                LAG(r.event_time) OVER (
                    PARTITION BY r.carrier ORDER BY r.event_time
                ) AS prev_time
            FROM r
            LEFT JOIN managed m ON m.carrier = r.carrier
            WHERE r.event_time >= COALESCE(m.baseline_at, TIMESTAMP '1970-01-01')
        )
        SELECT
            f.carrier,
            COUNT(*) FILTER (
                WHERE f.prev_time IS NULL
                   OR f.event_time - f.prev_time > interval '{int(CYCLE_GAP_SECONDS)} seconds'
            ) AS cycles,
            COUNT(*) AS total_tests,
            MIN(f.event_time) AS first_seen,
            MAX(f.event_time) AS last_seen,
            m.cycle_limit,
            m.baseline_at,
            m.notes
        FROM f
        LEFT JOIN managed m ON m.carrier = f.carrier
        GROUP BY f.carrier, m.cycle_limit, m.baseline_at, m.notes
        ORDER BY cycles DESC
    """

    with connection.cursor() as cursor:
        # params=[] (não bare execute(sql)): força o psycopg2 a rodar sua
        # passagem de substituição %-style, que colapsa o '%%' escapado do
        # módulo em dedupe_doubled_scan() de volta a '%' antes de ir ao
        # Postgres — sem isso, "%%" chega literal e vira erro de sintaxe.
        cursor.execute(sql, [])
        rows = dictfetchall(cursor)

        # Carriers gerenciados sem nenhum registro após o baseline
        # (recém-substituídos): entram com 0 ciclos.
        seen = {row["carrier"] for row in rows}
        cursor.execute(f"SELECT carrier, cycle_limit, baseline_at, notes FROM {CARRIER_TABLE}")
        for carrier, cycle_limit, baseline_at, notes in cursor.fetchall():
            if carrier not in seen:
                rows.append({
                    "carrier": carrier,
                    "cycles": 0,
                    "total_tests": 0,
                    "first_seen": None,
                    "last_seen": None,
                    "cycle_limit": cycle_limit,
                    "baseline_at": baseline_at,
                    "notes": notes,
                })

    return rows


def reset_carrier(carrier, notes=None):
    """Zera a contagem do carrier (substituído/instalado agora)."""
    ensure_carrier_table()
    sql = f"""
        INSERT INTO {CARRIER_TABLE} (carrier, baseline_at, updated_at, notes)
        VALUES (%s, NOW(), NOW(), %s)
        ON CONFLICT (carrier) DO UPDATE
        SET baseline_at = NOW(),
            updated_at = NOW(),
            notes = COALESCE(EXCLUDED.notes, {CARRIER_TABLE}.notes)
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, [carrier, notes])


def parametric_distribution(filters, step, usl=None, lsl=None, n_bins=30):
    """Distribuição paramétrica (histograma + Cp/Cpk) de um step numérico do
    EEData/SPC — ex.: PACK, DCIR, Temperature. `step` é a chave JSON do valor
    medido; não passa por get_exprs() porque é dinâmica (escolhida pelo
    usuário), diferente das chaves fixas normalizadas ali."""
    import numpy as np
    import pandas as pd

    cols = table_columns()
    json_col = get_json_column(cols)
    if not json_col:
        return {"step": step, "count": 0, "error": "No JSON column found"}

    where_sql, params = build_where(filters)
    sep = "AND" if where_sql else "WHERE"

    # Padrão numérico: inteiro, float, notação científica
    numeric_re = r'^-?[0-9]+\.?[0-9]*([eE][+-]?[0-9]+)?$'

    sql = f"""
        SELECT ({json_col} ->> %s)::float AS value
        FROM {TABLE_NAME}
        {where_sql}
        {sep} ({json_col} ->> %s) IS NOT NULL
          AND ({json_col} ->> %s) ~ %s
        LIMIT 50000
    """

    with connection.cursor() as cursor:
        # A 1ª ocorrência de %s é a do SELECT, ANTES dos params de where_sql
        # (station/model/datas/etc.) — bind na ordem em que aparecem no SQL.
        cursor.execute(sql, [step] + params + [step, step, numeric_re])
        rows = cursor.fetchall()

    if not rows:
        return {
            "step": step, "count": 0,
            "bins": [], "stats": {}, "limits": {}, "capability": {},
        }

    values = pd.Series([row[0] for row in rows], dtype=float).dropna()

    if values.empty:
        return {
            "step": step, "count": 0,
            "bins": [], "stats": {}, "limits": {}, "capability": {},
        }

    n = len(values)
    mean_val = float(values.mean())
    std_val = float(values.std(ddof=1)) if n > 1 else 0.0

    usl_val = float(usl) if usl is not None else mean_val + 3 * std_val
    lsl_val = float(lsl) if lsl is not None else mean_val - 3 * std_val

    counts, edges = np.histogram(values, bins=n_bins)

    cp = cpk = None
    if std_val > 0:
        cp = (usl_val - lsl_val) / (6 * std_val)
        cpu = (usl_val - mean_val) / (3 * std_val)
        cpl = (mean_val - lsl_val) / (3 * std_val)
        cpk = min(cpu, cpl)

    return {
        "step": step,
        "count": n,
        "bins": [
            {
                "x": round(float(edges[i]), 8),
                "x_end": round(float(edges[i + 1]), 8),
                "y": int(counts[i]),
            }
            for i in range(len(counts))
        ],
        "stats": {
            "mean": round(mean_val, 6),
            "std": round(std_val, 6),
            "min": round(float(values.min()), 6),
            "max": round(float(values.max()), 6),
            "p5": round(float(values.quantile(0.05)), 6),
            "p95": round(float(values.quantile(0.95)), 6),
        },
        "limits": {
            "usl": round(usl_val, 6),
            "lsl": round(lsl_val, 6),
            "auto": usl is None and lsl is None,
        },
        "capability": {
            "cp": round(float(cp), 3) if cp is not None else None,
            "cpk": round(float(cpk), 3) if cpk is not None else None,
        },
    }


# ---------------------------------------------------------------------------
# EEData / SPC — visão geral de Cp/Cpk de TODOS os steps paramétricos de uma
# vez (em vez de um por vez via parametric_distribution()).
# ---------------------------------------------------------------------------

# Tabela PRÓPRIA do dashboard para sobrescrever manualmente o LSL/USL de um
# step — mesmo padrão de dashboard_carriers: auto-detectado por padrão,
# editável, "limpar" volta ao automático.
STEP_SPECS_TABLE = "dashboard_step_specs"

# Sentinela de placeholder vindo de alguns schemas (registros de diagnóstico
# sem limite físico real, ex.: chips de gauge) — descarta magnitude absurda.
SPEC_SENTINEL_ABS_MAX = 1_000_000


def ensure_step_specs_table():
    sql = f"""
        CREATE TABLE IF NOT EXISTS {STEP_SPECS_TABLE} (
            step_key      TEXT PRIMARY KEY,
            usl_override  DOUBLE PRECISION NULL,
            lsl_override  DOUBLE PRECISION NULL,
            unit_override TEXT NULL,
            updated_at    TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """
    with connection.cursor() as cursor:
        cursor.execute(sql)


# Chaves do schema que são metadado/identificação, não medida paramétrica
# (já normalizadas em outro lugar via get_exprs(), ou texto/ID/timestamp) —
# excluídas dos candidatos a "step". Qualquer outra chave sobra como
# candidata; a que nunca tiver valor numérico real simplesmente não aparece
# no resultado (auto-filtrada pela query de contagem).
NON_PARAMETRIC_KEYS = {
    "station_id", "Station", "machine_no", "device_name", "Station ID", "Reserve_StationID",
    "Model", "model", "proj_code", "PN", "QualityPn",
    "test_result", "TestResult", "result", "status", "Test PASS/FAIL STATUS",
    "test_time", "TestTime", "datetime", "timestamp", "Test Start Time", "Test Stop Time",
    "error_code", "SC_ERR_CODE", "SC_RESULT_CODE", "error_msg", "SC_ERR_MSG",
    "List of Failing Tests",
    "channel_no", "Channel", "CH", "channel",
    "barcode", "carrier", "Carrier",
    "_line_no", "line_no", "WO", "test_user", "Version", "Other",
    "proc_name", "test_type", "SerialNumber", "Total",
}


def _to_float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _latest_schema_row(model_name=None):
    """Busca o schema (colunas + limites) mais recente de mes_csv_schemas —
    capturado pelo cliente do cabeçalho do CSV (linha 2 do formato PCM/CYG:
    'Nome(unidade)[lsl-usl]', ex.: 'Sleep Power(μA)[0.01-2]'). Usa o schema
    mais recente do model_name informado; sem model, o mais recente entre
    todos. Retorna (columns, upper, lower, units) já desserializados, ou
    None se não houver nenhum schema capturado ainda."""
    with connection.cursor() as cursor:
        if model_name:
            cursor.execute("""
                SELECT columns_json, upper_limits_json, lower_limits_json, units_json
                FROM mes_csv_schemas
                WHERE model_name = %s
                ORDER BY last_seen DESC LIMIT 1
            """, [model_name])
        else:
            cursor.execute("""
                SELECT columns_json, upper_limits_json, lower_limits_json, units_json
                FROM mes_csv_schemas
                ORDER BY last_seen DESC LIMIT 1
            """)
        row = cursor.fetchone()

    if not row:
        return None

    columns, upper, lower, units = row
    columns = json.loads(columns) if isinstance(columns, str) else (columns or [])
    upper = json.loads(upper) if isinstance(upper, str) else (upper or {})
    lower = json.loads(lower) if isinstance(lower, str) else (lower or {})
    units = json.loads(units) if isinstance(units, str) else (units or {})
    return columns, upper, lower, units


def discover_schema_specs(model_name=None):
    """Descobre os steps paramétricos com limite conhecido e seus valores
    (LSL/USL/unidade), a partir do schema mais recente em mes_csv_schemas."""
    schema = _latest_schema_row(model_name)
    if not schema:
        return {}
    _columns, upper, lower, units = schema

    specs = {}
    for key, usl_raw in upper.items():
        usl = _to_float_or_none(usl_raw)
        lsl = _to_float_or_none(lower.get(key))
        if usl is None or lsl is None:
            continue
        if abs(usl) > SPEC_SENTINEL_ABS_MAX or abs(lsl) > SPEC_SENTINEL_ABS_MAX:
            continue
        specs[key] = {"usl": usl, "lsl": lsl, "unit": units.get(key) or ""}

    return specs


def discover_step_candidates(model_name=None):
    """Todas as colunas do schema que podem ser um step paramétrico (para
    achar também steps sem limite definido, ex.: Temperature — entram no
    resultado com limite automático μ±3σ se tiverem dado numérico real)."""
    schema = _latest_schema_row(model_name)
    if not schema:
        return set()
    columns, _upper, _lower, _units = schema
    return {c for c in columns if c and c not in NON_PARAMETRIC_KEYS}


def step_overrides():
    ensure_step_specs_table()
    with connection.cursor() as cursor:
        cursor.execute(f"""
            SELECT step_key, usl_override, lsl_override, unit_override
            FROM {STEP_SPECS_TABLE}
        """)
        return {
            row[0]: {"usl": row[1], "lsl": row[2], "unit": row[3]}
            for row in cursor.fetchall()
        }


def set_step_spec(step_key, usl=None, lsl=None, unit=None):
    """Sobrescreve LSL/USL/unidade de um step. Qualquer valor None mantém o
    automático (detectado de mes_csv_schemas) para aquele campo específico."""
    ensure_step_specs_table()
    sql = f"""
        INSERT INTO {STEP_SPECS_TABLE} (step_key, usl_override, lsl_override, unit_override, updated_at)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (step_key) DO UPDATE
        SET usl_override = EXCLUDED.usl_override,
            lsl_override = EXCLUDED.lsl_override,
            unit_override = EXCLUDED.unit_override,
            updated_at = NOW()
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, [step_key, usl, lsl, unit])


def step_cpk_overview(filters):
    """Cp/Cpk de todos os steps paramétricos conhecidos, calculado numa
    única varredura (jsonb_each_text + GROUP BY) — não uma query por step."""
    cols = table_columns()
    json_col = get_json_column(cols)
    if not json_col:
        return []

    model = filters.get("model") or None
    auto_specs = discover_schema_specs(model)
    overrides = step_overrides()
    candidates = discover_step_candidates(model)

    known_steps = sorted(candidates | set(auto_specs.keys()) | set(overrides.keys()))
    if not known_steps:
        return []

    where_sql, params = build_where(filters)
    numeric_re = r'^-?[0-9]+\.?[0-9]*([eE][+-]?[0-9]+)?$'

    sql = f"""
        WITH base AS (
            SELECT {json_col} AS row_data
            FROM {TABLE_NAME}
            {where_sql}
        ), unpivoted AS (
            SELECT j.key AS step_key, (j.value)::float AS value
            FROM base, jsonb_each_text(base.row_data) j
            WHERE j.key = ANY(%s)
              AND j.value ~ %s
        )
        SELECT step_key, COUNT(*), AVG(value), STDDEV_SAMP(value)
        FROM unpivoted
        GROUP BY step_key
    """

    with connection.cursor() as cursor:
        cursor.execute(sql, params + [known_steps, numeric_re])
        stats_by_step = {row[0]: row[1:] for row in cursor.fetchall()}

    results = []
    for step in known_steps:
        stats = stats_by_step.get(step)
        count = stats[0] if stats else 0
        mean = float(stats[1]) if stats and stats[1] is not None else None
        std = float(stats[2]) if stats and stats[2] is not None else 0.0

        if not count or mean is None:
            continue  # sem amostras neste período/filtro — não exibe

        auto = auto_specs.get(step, {})
        override = overrides.get(step, {})

        usl = override.get("usl") if override.get("usl") is not None else auto.get("usl")
        lsl = override.get("lsl") if override.get("lsl") is not None else auto.get("lsl")
        unit = override.get("unit") if override.get("unit") else auto.get("unit", "")

        limits_auto = usl is None or lsl is None
        if limits_auto:
            usl = mean + 3 * std
            lsl = mean - 3 * std

        limits_valid = usl > lsl
        cp = cpk = None
        if limits_valid and std > 0:
            cp = (usl - lsl) / (6 * std)
            cpu = (usl - mean) / (3 * std)
            cpl = (mean - lsl) / (3 * std)
            cpk = min(cpu, cpl)

        results.append({
            "step": step,
            "unit": unit,
            "count": count,
            "mean": round(mean, 6),
            "std": round(std, 6),
            "usl": round(usl, 6),
            "lsl": round(lsl, 6),
            "usl_is_override": override.get("usl") is not None,
            "lsl_is_override": override.get("lsl") is not None,
            "limits_auto": limits_auto,
            "limits_valid": limits_valid,
            "cp": round(cp, 3) if cp is not None else None,
            "cpk": round(cpk, 3) if cpk is not None else None,
        })

    # Piores Cpk primeiro (mais urgente para engenharia olhar)
    results.sort(key=lambda r: (r["cpk"] is None, r["cpk"] if r["cpk"] is not None else 0))
    return results


def set_carrier_limit(carrier, limit):
    """Define limite de ciclos individual (None volta ao limite global)."""
    ensure_carrier_table()
    sql = f"""
        INSERT INTO {CARRIER_TABLE} (carrier, cycle_limit, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (carrier) DO UPDATE
        SET cycle_limit = EXCLUDED.cycle_limit,
            updated_at = NOW()
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, [carrier, limit])


def channel_hourly(filters):
    """Yield por canal por hora — base da detecção de canal intermitente."""
    exprs = get_exprs()
    where_sql, params = build_where(filters)

    sql = f"""
        WITH base AS (
            SELECT
                {exprs["channel_no"]} AS channel_no,
                date_trunc('hour', {exprs["event_time"]}) AS hour_ts,
                UPPER(COALESCE({exprs["result"]}::text, '')) AS result_text
            FROM {TABLE_NAME}
            {where_sql}
        )
        SELECT
            channel_no,
            TO_CHAR(hour_ts, 'YYYY-MM-DD HH24:00') AS hour_label,
            COUNT(*) FILTER (WHERE result_text IN %s) AS pass_count,
            COUNT(*) FILTER (WHERE result_text IN %s) AS fail_count,
            COUNT(*) AS total
        FROM base
        WHERE channel_no BETWEEN 1 AND 20
        GROUP BY channel_no, hour_ts
        ORDER BY channel_no, hour_ts
    """

    with connection.cursor() as cursor:
        cursor.execute(sql, params + [PASS_RESULTS, FAIL_RESULTS])
        return dictfetchall(cursor)


def failure_channel_matrix(filters):
    exprs = get_exprs()
    where_sql, params = build_where(filters)

    sql = f"""
        WITH base AS (
            SELECT
                {exprs["failure"]} AS failure,
                {exprs["channel_no"]} AS channel_no,
                UPPER(COALESCE({exprs["result"]}::text, '')) AS result_text
            FROM {TABLE_NAME}
            {where_sql}
        ), fail_rows AS (
            SELECT
                failure,
                channel_no,
                COUNT(*) AS total
            FROM base
            WHERE result_text IN %s
              AND failure IS NOT NULL
              AND failure <> ''
              AND failure <> 'UNKNOWN'
              AND channel_no BETWEEN 1 AND 20
            GROUP BY failure, channel_no
        ), totals AS (
            SELECT failure, SUM(total) AS row_total
            FROM fail_rows
            GROUP BY failure
        )
        SELECT f.failure, f.channel_no, f.total, t.row_total
        FROM fail_rows f
        JOIN totals t ON t.failure = f.failure
        ORDER BY t.row_total DESC, f.failure, f.channel_no
    """

    with connection.cursor() as cursor:
        cursor.execute(sql, params + [FAIL_RESULTS])
        rows = dictfetchall(cursor)

    matrix = {}
    totals = {}

    for row in rows:
        failure = row.get("failure") or "UNKNOWN"
        channel = str(int(row.get("channel_no") or 0))
        total = int(row.get("total") or 0)

        if failure not in matrix:
            matrix[failure] = {str(ch): 0 for ch in CHANNELS}
            totals[failure] = 0

        if channel in matrix[failure]:
            matrix[failure][channel] += total
            totals[failure] += total

    ordered_failures = sorted(totals.keys(), key=lambda x: totals[x], reverse=True)[:20]

    return {
        "channels": CHANNELS,
        "hot_limit": HOT_LIMIT,
        "rows": [
            {
                "name": failure,
                "total": totals[failure],
                "values": matrix[failure],
            }
            for failure in ordered_failures
        ],
    }


def carrier_channel_matrix(filters):
    exprs = get_exprs()
    where_sql, params = build_where(filters)

    sql = f"""
        WITH base AS (
            SELECT
                {exprs["carrier"]} AS carrier,
                {exprs["channel_no"]} AS channel_no,
                UPPER(COALESCE({exprs["result"]}::text, '')) AS result_text
            FROM {TABLE_NAME}
            {where_sql}
        ), fail_rows AS (
            SELECT
                carrier,
                channel_no,
                COUNT(*) AS total
            FROM base
            WHERE result_text IN %s
              AND carrier IS NOT NULL
              AND carrier <> ''
              AND carrier <> 'N/A'
              AND channel_no BETWEEN 1 AND 20
            GROUP BY carrier, channel_no
        ), totals AS (
            SELECT carrier, SUM(total) AS row_total
            FROM fail_rows
            GROUP BY carrier
        )
        SELECT f.carrier, f.channel_no, f.total, t.row_total
        FROM fail_rows f
        JOIN totals t ON t.carrier = f.carrier
        ORDER BY t.row_total DESC, f.carrier, f.channel_no
        LIMIT 2000
    """

    with connection.cursor() as cursor:
        cursor.execute(sql, params + [FAIL_RESULTS])
        rows = dictfetchall(cursor)

    matrix = {}
    totals = {}

    for row in rows:
        carrier = row.get("carrier") or "N/A"
        channel = str(int(row.get("channel_no") or 0))
        total = int(row.get("total") or 0)

        if carrier not in matrix:
            matrix[carrier] = {str(ch): 0 for ch in CHANNELS}
            totals[carrier] = 0

        if channel in matrix[carrier]:
            matrix[carrier][channel] += total
            totals[carrier] += total

    ordered_carriers = sorted(totals.keys(), key=lambda x: totals[x], reverse=True)[:25]

    return {
        "channels": CHANNELS,
        "hot_limit": HOT_LIMIT,
        "rows": [
            {
                "name": carrier,
                "total": totals[carrier],
                "values": matrix[carrier],
            }
            for carrier in ordered_carriers
        ],
    }


def debug_info():
    exprs = get_exprs()

    sql = f"""
        SELECT
            COUNT(*) AS total_rows,
            COUNT(DISTINCT {exprs["station"]}) AS stations,
            COUNT(DISTINCT {exprs["model"]}) AS models,
            MIN({exprs["created"]}) AS first_created_at,
            MAX({exprs["created"]}) AS last_created_at,
            MAX({exprs["event_time"]}) AS last_event_time
        FROM {TABLE_NAME}
    """

    with connection.cursor() as cursor:
        cursor.execute(sql)
        return dictfetchall(cursor)[0]


def distinct_filters():
    exprs = get_exprs()

    station_sql = f"""
        SELECT DISTINCT {exprs["station"]} AS value
        FROM {TABLE_NAME}
        WHERE {exprs["station"]} IS NOT NULL
          AND {exprs["station"]} <> ''
          AND {exprs["station"]} ~ %s
          AND LENGTH({exprs["station"]}) <= 64
        ORDER BY value
    """

    model_sql = f"""
        SELECT DISTINCT {exprs["model"]} AS value
        FROM {TABLE_NAME}
        WHERE {exprs["model"]} IS NOT NULL
          AND {exprs["model"]} <> ''
          AND {exprs["model"]} ~ %s
          AND LENGTH({exprs["model"]}) <= 64
        ORDER BY value
    """

    carrier_sql = f"""
        SELECT DISTINCT {exprs["carrier"]} AS value
        FROM {TABLE_NAME}
        WHERE {exprs["carrier"]} IS NOT NULL
          AND {exprs["carrier"]} <> ''
          AND {exprs["carrier"]} <> 'N/A'
          AND {exprs["carrier"]} ~ %s
          AND LENGTH({exprs["carrier"]}) <= 64
        ORDER BY value
        LIMIT 500
    """

    failure_sql = f"""
        SELECT DISTINCT {exprs["failure"]} AS value
        FROM {TABLE_NAME}
        WHERE {exprs["failure"]} IS NOT NULL
          AND {exprs["failure"]} <> ''
          AND {exprs["failure"]} <> 'UNKNOWN'
          AND {exprs["failure"]} ~ %s
          AND LENGTH({exprs["failure"]}) <= 64
        ORDER BY value
        LIMIT 500
    """

    with connection.cursor() as cursor:
        cursor.execute(station_sql, [SANE_FILTER_VALUE_RE])
        stations = [row[0] for row in cursor.fetchall() if row[0]]

        cursor.execute(model_sql, [SANE_FILTER_VALUE_RE])
        models = [row[0] for row in cursor.fetchall() if row[0]]

        cursor.execute(carrier_sql, [SANE_FILTER_VALUE_RE])
        carriers = [row[0] for row in cursor.fetchall() if row[0]]

        cursor.execute(failure_sql, [SANE_FILTER_VALUE_RE])
        failures = [row[0] for row in cursor.fetchall() if row[0]]

    return {
        "stations": stations,
        "models": models,
        "carriers": carriers,
        "failures": failures,
    }
