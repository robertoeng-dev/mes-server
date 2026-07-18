import io
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


# d2 do gráfico de amplitude móvel (n=2, pares consecutivos) — constante
# padrão de CEP para estimar o desvio-padrão de curto prazo ("Within") a
# partir de dados individuais (sem subgrupos racionais explícitos).
MOVING_RANGE_D2 = 1.128


def _capability_indices(usl, lsl, mean, sigma):
    """Cp/CPL/CPU/Cpk (ou Pp/PPL/PPU/Ppk, dependendo de qual sigma entra) —
    mesma fórmula, o rótulo muda conforme o sigma usado (overall vs. within).
    None se o sigma for inválido ou os limites estiverem invertidos."""
    if sigma is None or sigma <= 0 or usl is None or lsl is None or usl <= lsl:
        return {"cp": None, "cpl": None, "cpu": None, "cpk": None}
    cp = (usl - lsl) / (6 * sigma)
    cpu = (usl - mean) / (3 * sigma)
    cpl = (mean - lsl) / (3 * sigma)
    return {
        "cp": round(cp, 3),
        "cpl": round(cpl, 3),
        "cpu": round(cpu, 3),
        "cpk": round(min(cpu, cpl), 3),
    }


def _expected_ppm(usl, lsl, mean, sigma):
    """PPM esperado (< LSL, > USL, total) assumindo distribuição normal —
    é o que o Minitab chama de 'Expected Overall'/'Expected Within'."""
    from scipy.stats import norm

    if sigma is None or sigma <= 0 or usl is None or lsl is None:
        return {"below": None, "above": None, "total": None}
    below = float(norm.cdf((lsl - mean) / sigma)) * 1_000_000
    above = float((1 - norm.cdf((usl - mean) / sigma))) * 1_000_000
    return {
        "below": round(below, 2),
        "above": round(above, 2),
        "total": round(below + above, 2),
    }


# Nº máximo de pontos desenhados no Probability Plot/Boxplot — as
# estatísticas (Anderson-Darling, quartis) sempre usam a amostra COMPLETA;
# isso só limita quantos pontos vão pro navegador (até 50.000 pontos num
# scatter travaria o Chart.js sem ganho nenhum de leitura visual).
MAX_PLOT_POINTS = 2000


def _anderson_darling_normal(sorted_values, mean, std):
    """Estatística de Anderson-Darling ajustada (AD*) e p-value para adesão
    à normal, via D'Agostino & Stephens (1986) — mesma referência que o
    Minitab usa no Probability Plot. `sorted_values` deve vir ordenado
    ascendente. None/None se a amostra for pequena demais (< 8) ou sem
    variação (std<=0) para o teste fazer sentido."""
    import numpy as np
    from scipy.stats import norm

    n = len(sorted_values)
    if n < 8 or std is None or std <= 0:
        return None, None

    z = (np.asarray(sorted_values, dtype=float) - mean) / std
    i = np.arange(1, n + 1)
    # logsf(z[::-1])[k] = ln(1-Φ(z_{n+1-i})) para i=k+1 — ver nota de
    # indexação no histórico desta função (services.py, busca "AD*").
    log_cdf = norm.logcdf(z)
    log_sf_rev = norm.logsf(z[::-1])
    s = float(np.sum((2 * i - 1) * (log_cdf + log_sf_rev)))
    a2 = -n - s / n
    a2_star = a2 * (1 + 0.75 / n + 2.25 / n ** 2)

    # A fórmula polinomial abaixo (D'Agostino & Stephens) só é válida para
    # AD* na faixa em que costuma cair dado real (até ~poucas unidades).
    # Dado fortemente não-normal (ex.: bimodal por outliers de medição)
    # pode gerar AD* na casa dos milhares — a branch >= 0.6 tem um termo
    # QUADRÁTICO (+0.0186·AD*²) que passa a DOMINAR o termo linear negativo
    # a partir de AD*≈153, fazendo o expoente virar positivo e o np.exp()
    # estourar pra +inf; esse +inf então era silenciosamente grampeado para
    # 1.0 pelo np.clip(), reportando "perfeitamente normal" exatamente no
    # caso mais não-normal possível. Corte de segurança bem antes disso:
    # em AD*=20 a fórmula já dá p≈3e-46 (indistinguível de 0 na prática).
    if a2_star > 20:
        p = 0.0
    elif a2_star >= 0.6:
        p = np.exp(1.2937 - 5.709 * a2_star + 0.0186 * a2_star ** 2)
    elif a2_star > 0.34:
        p = np.exp(0.9177 - 4.279 * a2_star - 1.38 * a2_star ** 2)
    elif a2_star > 0.2:
        p = 1 - np.exp(-8.318 + 42.796 * a2_star - 59.938 * a2_star ** 2)
    else:
        p = 1 - np.exp(-13.436 + 101.14 * a2_star - 223.73 * a2_star ** 2)

    return round(float(a2_star), 4), round(float(np.clip(p, 0.0, 1.0)), 4)


def _probability_plot_data(sorted_values, mean, std, lsl, usl):
    """Normal Probability Plot (estilo Minitab): posição de plotagem de
    Benard (median rank) por ponto, reta teórica da normal ajustada, e
    banda de confiança de 95% EXATA via distribuição Beta dos order
    statistics (F(x_(i)) ~ Beta(i, n+1-i) — resultado livre de distribuição,
    não uma aproximação normal do erro-padrão). Até MAX_PLOT_POINTS pontos
    são amostrados uniformemente pelo rank para o navegador não ter que
    desenhar até 50.000 pontos; a amostra usada nas contas (AD, quartis)
    continua sendo a completa."""
    import numpy as np
    from scipy.stats import beta as beta_dist
    from scipy.stats import norm

    n = len(sorted_values)
    if n < 2 or std is None or std <= 0:
        return {"points": [], "fit_line": [], "ci_lower": [], "ci_upper": []}

    if n > MAX_PLOT_POINTS:
        idx = np.unique(np.linspace(0, n - 1, MAX_PLOT_POINTS).astype(int))
    else:
        idx = np.arange(n)

    ranks = idx + 1  # posição 1-based do order statistic
    plot_pos = (ranks - 0.3) / (n + 0.4)
    z = norm.ppf(plot_pos)

    beta_lower = beta_dist.ppf(0.025, ranks, n + 1 - ranks)
    beta_upper = beta_dist.ppf(0.975, ranks, n + 1 - ranks)
    z_lower = norm.ppf(beta_lower)
    z_upper = norm.ppf(beta_upper)

    values_arr = np.asarray(sorted_values, dtype=float)
    is_oos = (values_arr[idx] < lsl) | (values_arr[idx] > usl)

    return {
        "points": [
            {"x": round(float(values_arr[idx[k]]), 6), "z": round(float(z[k]), 4),
             "out_of_spec": bool(is_oos[k])}
            for k in range(len(idx))
        ],
        "fit_line": [
            {"x": round(float(mean + std * z[k]), 6), "z": round(float(z[k]), 4)}
            for k in range(len(idx))
        ],
        "ci_lower": [
            {"x": round(float(mean + std * z_lower[k]), 6), "z": round(float(z[k]), 4)}
            for k in range(len(idx))
        ],
        "ci_upper": [
            {"x": round(float(mean + std * z_upper[k]), 6), "z": round(float(z[k]), 4)}
            for k in range(len(idx))
        ],
    }


def _boxplot_data(values, lsl, usl):
    """Boxplot Tukey padrão: Q1/mediana/Q3, hastes até o ponto real mais
    extremo dentro de Q1-1.5·IQR / Q3+1.5·IQR, outliers além disso —
    cada outlier marcado "out_of_spec" (fora de LSL/USL, problema de
    qualidade real) ou "statistical" (atípico mas dentro da spec), para o
    frontend colorir os dois casos de forma diferente."""
    q1, median, q3 = (float(v) for v in values.quantile([0.25, 0.5, 0.75]))
    iqr = q3 - q1
    fence_low, fence_high = q1 - 1.5 * iqr, q3 + 1.5 * iqr

    within = values[(values >= fence_low) & (values <= fence_high)]
    whisker_low = float(within.min()) if len(within) else q1
    whisker_high = float(within.max()) if len(within) else q3

    outliers = values[(values < fence_low) | (values > fence_high)]

    return {
        "q1": round(q1, 6),
        "median": round(median, 6),
        "q3": round(q3, 6),
        "whisker_low": round(whisker_low, 6),
        "whisker_high": round(whisker_high, 6),
        "outliers": [
            {"value": round(float(v), 6), "out_of_spec": bool(v < lsl or v > usl)}
            for v in outliers
        ],
    }


def parametric_distribution(filters, step, usl=None, lsl=None, target=None, n_bins=30):
    """Relatório de Capacidade de Processo (estilo Minitab) de um step
    numérico do EEData/SPC — ex.: PACK, DCIR, Temperature. `step` é a chave
    JSON do valor medido; não passa por get_exprs() porque é dinâmica
    (escolhida pelo usuário), diferente das chaves fixas normalizadas ali.

    Distingue dois desvios-padrão, como o Minitab: "Overall" (desvio-padrão
    amostral simples — variação de longo prazo) e "Within" (via amplitude
    móvel entre observações consecutivas na ordem temporal — variação de
    curto prazo/processo). Overall alimenta Pp/Ppk; Within alimenta Cp/Cpk —
    essa troca de nomes É a convenção do Minitab, não um erro de digitação."""
    import numpy as np
    import pandas as pd

    cols = table_columns()
    json_col = get_json_column(cols)
    if not json_col:
        return {"step": step, "count": 0, "error": "No JSON column found"}

    exprs = get_exprs()
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
        ORDER BY {exprs["event_time"]}, id
        LIMIT 50000
    """
    # ", id" é essencial, não cosmético: muitas linhas compartilham o mesmo
    # event_time (mesmo segundo) e, sem um desempate estável, o Postgres
    # pode devolver esse grupo em ordem diferente a cada execução — o que
    # tornava std_within (baseado em amplitude móvel = depende da ordem)
    # não-determinístico entre chamadas idênticas (Cpk variando a cada
    # request pro mesmo filtro). "id" é a PK, ordem de inserção — não é a
    # ordem real do teste físico, mas é ESTÁVEL, o que é o que importa aqui.

    with connection.cursor() as cursor:
        # A 1ª ocorrência de %s é a do SELECT, ANTES dos params de where_sql
        # (station/model/datas/etc.) — bind na ordem em que aparecem no SQL.
        cursor.execute(sql, [step] + params + [step, step, numeric_re])
        rows = cursor.fetchall()

    empty = {
        "step": step, "count": 0,
        "bins": [], "clipped_below": 0, "clipped_above": 0,
        "curve_x": [], "curve_overall": [], "curve_within": [],
        "process_data": {}, "overall_capability": {}, "potential_capability": {},
        "performance": {},
        "normality": {"ad_stat": None, "ad_pvalue": None},
        "probability_plot": {"points": [], "fit_line": [], "ci_lower": [], "ci_upper": []},
        "boxplot": {},
    }

    if not rows:
        return empty

    # Preserva a ordem temporal (ORDER BY event_time acima) — necessário
    # para a amplitude móvel representar variação real de curto prazo, não
    # uma ordem arbitrária de retorno do banco.
    values = pd.Series([row[0] for row in rows], dtype=float).dropna()

    if values.empty:
        return empty

    n = len(values)
    mean_val = float(values.mean())
    std_overall = float(values.std(ddof=1)) if n > 1 else 0.0

    moving_ranges = values.diff().abs().dropna()
    std_within = float(moving_ranges.mean()) / MOVING_RANGE_D2 if len(moving_ranges) > 0 else 0.0

    usl_val = float(usl) if usl is not None else mean_val + 3 * std_overall
    lsl_val = float(lsl) if lsl is not None else mean_val - 3 * std_overall
    target_val = float(target) if target is not None else None

    # Faixa do histograma: NÃO usa values.min()/values.max() cru — um único
    # outlier distante do resto (ex.: uma leitura zerada por falha de
    # medição) estica o eixo X até lá, espremendo a distribuição real numa
    # fatia estreita perto de uma borda (o problema relatado: eixo indo até
    # 0 escondia o agrupamento real perto de 2.3). Usa a faixa mais ESTREITA
    # entre percentis (1%-99%, robusto a outliers) e μ±4σ, sempre alargada
    # para incluir LSL/USL/target (é um gráfico de capacidade — os limites
    # de especificação não podem sumir do eixo). Pontos fora da faixa
    # exibida continuam contando nas estatísticas/PPM acima — só não
    # determinam mais a escala visual; ficam visíveis de verdade no
    # Boxplot/Probability Plot (que mostram todo ponto, inclusive
    # outliers), não neste histograma.
    p_low, p_high = float(values.quantile(0.01)), float(values.quantile(0.99))
    if std_overall > 0:
        range_low = max(p_low, mean_val - 4 * std_overall)
        range_high = min(p_high, mean_val + 4 * std_overall)
    else:
        range_low, range_high = p_low, p_high
    if range_low >= range_high:
        range_low, range_high = float(values.min()), float(values.max())

    range_low = min(range_low, lsl_val, target_val) if target_val is not None else min(range_low, lsl_val)
    range_high = max(range_high, usl_val, target_val) if target_val is not None else max(range_high, usl_val)

    pad = (range_high - range_low) * 0.03 or (abs(range_high) * 0.03 or 1.0)
    range_low -= pad
    range_high += pad

    clipped_below = int((values < range_low).sum())
    clipped_above = int((values > range_high).sum())

    counts, edges = np.histogram(values, bins=n_bins, range=(range_low, range_high))
    bin_width = float(edges[1] - edges[0]) if len(edges) > 1 else 1.0
    bin_centers = [(edges[i] + edges[i + 1]) / 2 for i in range(len(counts))]

    from scipy.stats import norm

    def fitted_curve(sigma):
        if sigma is None or sigma <= 0:
            return [0.0] * len(bin_centers)
        return [
            round(float(n * bin_width * norm.pdf(x, mean_val, sigma)), 4)
            for x in bin_centers
        ]

    overall_idx = _capability_indices(usl_val, lsl_val, mean_val, std_overall)
    within_idx = _capability_indices(usl_val, lsl_val, mean_val, std_within)

    cpm = None
    if target_val is not None and std_overall > 0 and usl_val > lsl_val:
        denom = (std_overall ** 2 + (mean_val - target_val) ** 2) ** 0.5
        if denom > 0:
            cpm = round((usl_val - lsl_val) / (6 * denom), 3)

    observed_below = int((values < lsl_val).sum())
    observed_above = int((values > usl_val).sum())
    observed_total = observed_below + observed_above

    sorted_values = np.sort(values.to_numpy())
    ad_stat, ad_pvalue = _anderson_darling_normal(sorted_values, mean_val, std_overall)
    probability_plot = _probability_plot_data(sorted_values, mean_val, std_overall, lsl_val, usl_val)
    boxplot = _boxplot_data(values, lsl_val, usl_val)

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
        # Pontos com valor fora da faixa exibida no histograma (ver comentário
        # acima do cálculo de range_low/range_high) — não estão perdidos, só
        # não aparecem nesta faixa; o frontend mostra um aviso quando > 0.
        "clipped_below": clipped_below,
        "clipped_above": clipped_above,
        "curve_x": [round(float(x), 6) for x in bin_centers],
        "curve_overall": fitted_curve(std_overall),
        "curve_within": fitted_curve(std_within),
        "process_data": {
            "lsl": round(lsl_val, 6),
            "usl": round(usl_val, 6),
            "target": round(target_val, 6) if target_val is not None else None,
            "limits_auto": usl is None and lsl is None,
            "mean": round(mean_val, 6),
            "n": n,
            "std_overall": round(std_overall, 6),
            "std_within": round(std_within, 6),
            "min": round(float(values.min()), 6),
            "max": round(float(values.max()), 6),
        },
        "overall_capability": {
            "pp": overall_idx["cp"],
            "ppl": overall_idx["cpl"],
            "ppu": overall_idx["cpu"],
            "ppk": overall_idx["cpk"],
            "cpm": cpm,
        },
        "potential_capability": within_idx,
        "performance": {
            "observed": {
                "below": round(1_000_000 * observed_below / n, 2),
                "above": round(1_000_000 * observed_above / n, 2),
                "total": round(1_000_000 * observed_total / n, 2),
            },
            "expected_overall": _expected_ppm(usl_val, lsl_val, mean_val, std_overall),
            "expected_within": _expected_ppm(usl_val, lsl_val, mean_val, std_within),
        },
        "normality": {"ad_stat": ad_stat, "ad_pvalue": ad_pvalue},
        "probability_plot": probability_plot,
        "boxplot": boxplot,
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
            comment       TEXT NULL,
            updated_at    TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """
    with connection.cursor() as cursor:
        cursor.execute(sql)
        # ADD COLUMN IF NOT EXISTS cobre bancos onde a tabela já existia
        # antes do campo "comment" ser introduzido (CREATE TABLE IF NOT
        # EXISTS não altera uma tabela já criada).
        cursor.execute(f"ALTER TABLE {STEP_SPECS_TABLE} ADD COLUMN IF NOT EXISTS comment TEXT NULL")


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
            SELECT step_key, usl_override, lsl_override, unit_override, comment
            FROM {STEP_SPECS_TABLE}
        """)
        return {
            row[0]: {"usl": row[1], "lsl": row[2], "unit": row[3], "comment": row[4]}
            for row in cursor.fetchall()
        }


# Nome do campo (kwarg de set_step_spec) -> coluna real na tabela
_STEP_SPEC_FIELDS = {
    "usl": "usl_override",
    "lsl": "lsl_override",
    "unit": "unit_override",
    "comment": "comment",
}


def set_step_spec(step_key, **fields):
    """Atualiza só os campos passados (usl/lsl/unit/comment) de um step,
    preservando os demais — editar o LSL não deve apagar um comentário já
    salvo, e vice-versa. Passar um campo com valor None o limpa de volta ao
    automático (ou string vazia, no caso de comment); campos OMITIDOS (não
    presentes em `fields`) simplesmente não são tocados."""
    ensure_step_specs_table()
    columns = [_STEP_SPEC_FIELDS[k] for k in fields if k in _STEP_SPEC_FIELDS]
    if not columns:
        return

    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {STEP_SPECS_TABLE} (step_key) VALUES (%s) ON CONFLICT (step_key) DO NOTHING",
            [step_key],
        )
        set_clause = ", ".join(f"{col} = %s" for col in columns) + ", updated_at = NOW()"
        values = [fields[k] for k in fields if k in _STEP_SPEC_FIELDS]
        cursor.execute(
            f"UPDATE {STEP_SPECS_TABLE} SET {set_clause} WHERE step_key = %s",
            values + [step_key],
        )


def _quote_ident(name):
    return '"' + name.replace('"', '""') + '"'


def step_cpk_overview(filters):
    """Cp/Cpk (convenção Minitab: baseado em Within/amplitude móvel — mesma
    base do relatório de detalhe de um step, ver parametric_distribution())
    de todos os steps paramétricos conhecidos, numa única varredura da
    tabela — não uma query por step.
    Sem isso, um step podia aparecer "INCAPAZ" aqui e "ACEITÁVEL" ao abrir
    o detalhe do mesmo step/filtro — os dois cálculos precisam bater.

    HISTÓRICO DE PERFORMANCE (37.685 linhas locais, todas testadas antes de
    chegar nesta versão):
    - jsonb_each_text (desmembra as ~197 chaves/linha, filtra depois por
      ANY(known_steps)) + LAG() window: 18,5s. O unpivot em si custava só
      ~1,9s (confirmado via EXPLAIN ANALYZE isolado); o real gargalo era o
      LAG() OVER (PARTITION BY step_key ORDER BY event_time, id) precisar
      ordenar ~1,3 milhão de linhas (o resultado do unpivot) — um sort que
      estoura work_mem e vai para disco (~24s medidos nessa variante).
    - UNION ALL (uma branch de SELECT por step): 103s — o planner faz um
      sequential scan da tabela POR BRANCH (176 scans em vez de 1).
    - LATERAL VALUES (uma passada só, projeta as chaves como linhas): 58s —
      melhor que UNION ALL mas ainda sofre do mesmo sort de ~1,3M linhas.
    - CTE "base" MATERIALIZED (evita recomputar a expressão cara de
      event_time 1,3M vezes): só 18,5s → 17s — a expressão de event_time
      NÃO era o gargalo dominante, o sort do LAG() sim.
    - SET LOCAL work_mem maior: não ajudou (~16s) — o sort de 1,3M linhas
      com 3 chaves compostas é caro mesmo em memória.
    - Buscar TODAS as colunas do JSON e processar em pandas (Python-side):
      ~14s — melhor, mas json.loads()/DataFrame de ~280 colunas por linha
      desperdiça tempo em chaves irrelevantes.
    - ESCOLHIDA: jsonb_to_record(row_data) pedindo só as ~176 chaves
      conhecidas como colunas tipadas — o Postgres extrai só o que
      interessa em C, sem gerar linhas extras (sem CROSS JOIN, sem sort de
      milhões de linhas: só ORDER BY event_time/id nas 37.685 linhas
      originais). O resto (moving range, desvio) vira contas vetorizadas em
      pandas por coluna. ~10,7s — não chega no ideal, mas quase a metade do
      original e sem a explosão combinatória das outras tentativas."""
    import pandas as pd

    cols = table_columns()
    json_col = get_json_column(cols)
    if not json_col:
        return []

    exprs = get_exprs()
    model = filters.get("model") or None
    auto_specs = discover_schema_specs(model)
    overrides = step_overrides()
    candidates = discover_step_candidates(model)

    known_steps = sorted(candidates | set(auto_specs.keys()) | set(overrides.keys()))
    if not known_steps:
        return []

    where_sql, params = build_where(filters)

    record_def = ", ".join(f"{_quote_ident(s)} text" for s in known_steps)
    select_cols = ", ".join(f"x.{_quote_ident(s)}" for s in known_steps)

    sql = f"""
        SELECT {select_cols}
        FROM (
            SELECT {json_col} AS row_data, {exprs["event_time"]} AS event_time, id
            FROM {TABLE_NAME}
            {where_sql}
        ) base,
        jsonb_to_record(base.row_data) AS x({record_def})
        ORDER BY base.event_time, base.id
    """
    # "id" desempata event_time repetido (mesmo segundo) de forma ESTÁVEL —
    # sem isso a amplitude móvel (depende da ordem) fica não-determinística
    # entre chamadas idênticas. Mesmo motivo do ORDER BY em
    # parametric_distribution().

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    df = pd.DataFrame(rows, columns=known_steps).apply(pd.to_numeric, errors="coerce")

    results = []
    for step in known_steps:
        col = df[step].dropna()
        count = len(col)
        mean = float(col.mean()) if count else None
        std_overall = float(col.std(ddof=1)) if count > 1 else 0.0
        moving_ranges = col.diff().abs().dropna()
        std_within = float(moving_ranges.mean()) / MOVING_RANGE_D2 if len(moving_ranges) else 0.0

        if not count or mean is None:
            continue  # sem amostras neste período/filtro — não exibe

        auto = auto_specs.get(step, {})
        override = overrides.get(step, {})

        usl = override.get("usl") if override.get("usl") is not None else auto.get("usl")
        lsl = override.get("lsl") if override.get("lsl") is not None else auto.get("lsl")
        unit = override.get("unit") if override.get("unit") else auto.get("unit", "")

        limits_auto = usl is None or lsl is None
        if limits_auto:
            # Auto-limite usa o desvio OVERALL, igual ao relatório de
            # detalhe (parametric_distribution) quando usl/lsl não vêm.
            usl = mean + 3 * std_overall
            lsl = mean - 3 * std_overall

        limits_valid = usl > lsl
        idx = _capability_indices(usl, lsl, mean, std_within)

        results.append({
            "step": step,
            "unit": unit,
            "count": count,
            "mean": round(mean, 6),
            "std": round(std_within, 6),
            "usl": round(usl, 6),
            "lsl": round(lsl, 6),
            "usl_is_override": override.get("usl") is not None,
            "lsl_is_override": override.get("lsl") is not None,
            "limits_auto": limits_auto,
            "limits_valid": limits_valid,
            "cp": idx["cp"],
            "cpk": idx["cpk"],
            "comment": override.get("comment") or "",
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
    # Só last_created_at/last_event_time são lidos pelo frontend (banner
    # online/offline) — total_rows/stations/models foram removidos: exigiam
    # um COUNT(*) e 2 COUNT(DISTINCT ...) na tabela inteira a cada chamada
    # (rodava a cada 60s de auto-refresh) sem nenhum consumidor.
    exprs = get_exprs()

    sql = f"""
        SELECT
            MAX({exprs["created"]}) AS last_created_at,
            MAX({exprs["event_time"]}) AS last_event_time
        FROM {TABLE_NAME}
    """

    with connection.cursor() as cursor:
        cursor.execute(sql)
        return dictfetchall(cursor)[0]


# Cache em memória (por processo) do resultado de distinct_filters() — as
# 4 consultas DISTINCT (station/model/carrier/failure) chegaram a medir
# 12,4s somadas (o "carrier" sozinho, com dedupe_doubled_scan() + COALESCE
# de várias chaves JSON, custava 6,5s para só 4 valores distintos), e essa
# função é chamada a CADA carga de página (loadFilterOptions(), a 1ª coisa
# que initDashboard() espera) — travava a tela por 12s antes de qualquer
# outra coisa carregar. Estação/modelo/carrier/falha mudam devagar (não é
# incomum passar dias sem um carrier novo aparecer), então um TTL curto é
# seguro e reduz isso a "12s uma vez a cada 5 minutos" em vez de "toda
# carga de página".
_DISTINCT_FILTERS_CACHE = {"data": None, "expires_at": 0.0}
DISTINCT_FILTERS_TTL_SECONDS = 300


def distinct_filters():
    import time

    now = time.time()
    cached = _DISTINCT_FILTERS_CACHE["data"]
    if cached is not None and now < _DISTINCT_FILTERS_CACHE["expires_at"]:
        return cached

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

    # Metadado de schema (mes_csv_schemas), não varre mes_test_results — fonte
    # barata para popular o dropdown de steps na carga da página; a lista
    # completa/real (só steps com dado numérico no filtro atual) é recarregada
    # via /api/spc/overview/ quando o usuário abre o relatório de Cp/Cpk.
    step_candidates = sorted(discover_step_candidates())

    result = {
        "stations": stations,
        "models": models,
        "carriers": carriers,
        "failures": failures,
        "step_candidates": step_candidates,
    }
    _DISTINCT_FILTERS_CACHE["data"] = result
    _DISTINCT_FILTERS_CACHE["expires_at"] = now + DISTINCT_FILTERS_TTL_SECONDS
    return result


# ---------------------------------------------------------------------------
# Exportação para Excel (.xlsx) — dataframe bruto do filtro atual (data/hora
# + demais filtros da tela ANÁLISE), para analisar em Minitab ou no próprio
# Excel. Não recria os gráficos do dashboard no arquivo — só entrega dado
# limpo, 1 linha por teste, para análise externa.
# ---------------------------------------------------------------------------

# Limite de linhas: acima disso, gerar o arquivo arriscaria travar o
# navegador/servidor. Filtro precisa ser estreitado.
EXPORT_ROW_CAP = 100_000


def export_dataset_xlsx(filters):
    """Gera o .xlsx (bytes) do dataset filtrado: metadado normalizado
    (data/hora, estação, modelo, carrier, canal, resultado) + todos os
    steps paramétricos conhecidos como colunas, 1 linha por teste — reusa
    o mesmo padrão jsonb_to_record de step_cpk_overview() (extração
    direta das chaves conhecidas, sem o unpivot caro de jsonb_each_text).

    Retorna {"error": ..., "count": N} SEM gerar o arquivo se não houver
    linha ou se o filtro exceder EXPORT_ROW_CAP — quem chama deve pedir
    para o usuário estreitar o filtro nesse caso.

    Nota sobre rastreabilidade: não há serial_number confiável no estágio
    PCM_TESTER (o "barcode" é o carrier — reutilizado entre várias placas
    — e "device_name" é o serial da FIXTURE do canal, não da peça testada).
    Por isso a coluna "Unit_ID" exportada é uma composição sintética
    (carrier + canal + id da linha), suficiente para cruzar com o restante
    da análise no Minitab/Excel, mas deliberadamente rotulada como
    sintética — não finge ser um serial real de unidade."""
    import pandas as pd
    import xlsxwriter

    cols = table_columns()
    json_col = get_json_column(cols)
    if not json_col:
        return {"error": "Tabela sem coluna JSON reconhecida.", "count": 0}

    exprs = get_exprs()
    where_sql, params = build_where(filters)

    with connection.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) FROM {TABLE_NAME} {where_sql}", params)
        total = cursor.fetchone()[0]

    if total == 0:
        return {"error": "Nenhuma linha encontrada no filtro atual.", "count": 0}
    if total > EXPORT_ROW_CAP:
        return {
            "error": (
                f"O filtro atual tem {total:,} linhas — acima do limite de "
                f"{EXPORT_ROW_CAP:,} para exportação. Estreite o período ou "
                f"outros filtros e tente novamente."
            ),
            "count": total,
        }

    model = filters.get("model") or None
    known_steps = sorted(discover_step_candidates(model))

    step_select, step_join = "", ""
    if known_steps:
        record_def = ", ".join(f"{_quote_ident(s)} text" for s in known_steps)
        select_cols = ", ".join(f"x.{_quote_ident(s)}" for s in known_steps)
        step_select = f", {select_cols}"
        step_join = f", jsonb_to_record(base.row_data) AS x({record_def})"

    sql = f"""
        SELECT
            base.id, base.event_time, base.station, base.model,
            base.carrier, base.channel_no, base.result
            {step_select}
        FROM (
            SELECT
                id,
                {exprs["event_time"]} AS event_time,
                {exprs["station"]} AS station,
                {exprs["model"]} AS model,
                {exprs["carrier"]} AS carrier,
                {exprs["channel_no"]} AS channel_no,
                {exprs["result"]} AS result,
                {json_col} AS row_data
            FROM {TABLE_NAME}
            {where_sql}
        ) base
        {step_join}
        ORDER BY base.event_time, base.id
    """

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        col_names = [d[0] for d in cursor.description]

    df = pd.DataFrame(rows, columns=col_names)
    if known_steps:
        numeric_cols = df[known_steps].apply(pd.to_numeric, errors="coerce")
    else:
        numeric_cols = pd.DataFrame(index=df.index)

    unit_id = (
        df["carrier"].fillna("N/A").astype(str) + "_ch" +
        df["channel_no"].fillna(0).astype(int).astype(str) + "_" +
        df["id"].astype(str)
    )
    # event_time como texto formatado, não datetime cru: escrevendo direto
    # via worksheet.write_row() (ver comentário abaixo) sem um num_format
    # de data, o xlsxwriter grava só o número de série do Excel
    # (ex.: 46105.362708) em vez de uma data legível — pandas.to_excel()
    # cuida disso sozinho, mas aqui foi trocado por escrita direta por
    # performance (ver nota mais abaixo).
    event_time_text = pd.to_datetime(df["event_time"]).dt.strftime("%Y-%m-%d %H:%M:%S")
    meta = df[["id", "station", "model", "carrier", "channel_no", "result"]].rename(columns={
        "id": "id_linha", "station": "estacao",
        "model": "modelo", "carrier": "carrier", "channel_no": "canal",
        "result": "resultado",
    })
    # pd.concat de uma vez em vez de df.insert()/atribuição de colunas
    # repetida — evita o "DataFrame is highly fragmented" que deixava a
    # geração do arquivo bem mais lenta num dataframe desta largura
    # (~180 colunas).
    df = pd.concat([
        pd.Series(unit_id, name="Unit_ID (sintetico: carrier+canal+id)"),
        meta[["id_linha"]],
        pd.Series(event_time_text, name="data_hora"),
        meta[["estacao", "modelo", "carrier", "canal", "resultado"]],
        numeric_cols,
    ], axis=1)

    # Escreve via API direta do xlsxwriter (constant_memory), NÃO via
    # df.to_excel() do pandas — testado e medido: numa planilha desta
    # largura (~180 colunas), o próprio df.to_excel() (independente do
    # engine — openpyxl OU xlsxwriter por baixo dele) itera célula a célula
    # em Python puro e levava ~112s para 37.685 linhas. Escrever direto via
    # worksheet.write_row() linha a linha caiu para ~22s (5x) — o gargalo
    # era a camada de formatação do pandas, não o motor de arquivo em si.
    buffer = io.BytesIO()
    workbook = xlsxwriter.Workbook(buffer, {"in_memory": True, "constant_memory": True})
    worksheet = workbook.add_worksheet("Dados")
    worksheet.write_row(0, 0, df.columns.tolist())
    # astype(object) ANTES do .where(): atribuir None a uma coluna float64
    # vira NaN de novo (arrays NumPy float não guardam None) — sem isso o
    # xlsxwriter rejeita NaN com TypeError ("NAN/INF not supported").
    rows_data = df.astype(object).where(pd.notna(df), None).values.tolist()
    for row_idx, row in enumerate(rows_data, start=1):
        worksheet.write_row(row_idx, 0, row)
    workbook.close()

    return {"file_bytes": buffer.getvalue(), "count": total}
