let topFailuresChart = null;
let hourlyYieldChart = null;
let channelsChart = null;
let spcChart = null;

if (window.ChartDataLabels) {
    Chart.register(ChartDataLabels);
}

document.body.classList.add("sidebar-collapsed");

// ---------------------------------------------------------------------------
// CONFIGURAÇÕES DO USUÁRIO (persistidas em localStorage do PC do dashboard)
// ---------------------------------------------------------------------------

const CONFIG_KEY = "mesDashboardConfig";

const DEFAULT_CONFIG = {
    productionGoal: 1300,        // meta de produção (linha no gráfico UPH)
    uphYMax: 1800,               // teto padrão do eixo Y do UPH
    hotLimit: 5,                 // matriz: célula > N fica vermelha
    refreshSeconds: 60,          // auto-refresh do dashboard
    onlineThresholdMinutes: 60,  // banner ONLINE/OFFLINE
    paretoTopN: 10,              // nº de falhas no pareto (1-20)
    yieldYellow: 95,             // saúde do canal: amarelo abaixo deste yield %
    yieldRed: 85,                // saúde do canal: vermelho abaixo deste yield %
    carrierCycleLimit: 5000,     // vida útil do carrier em ciclos (passagens)
    tvMode: false                // modo TV: esconde filtros, tela cheia
};

function loadConfig() {
    try {
        const saved = JSON.parse(localStorage.getItem(CONFIG_KEY) || "{}");
        return { ...DEFAULT_CONFIG, ...saved };
    } catch (err) {
        return { ...DEFAULT_CONFIG };
    }
}

let config = loadConfig();
let refreshTimer = null;

// Modo de operação: "online" (tempo real, chão de fábrica) ou "analise"
// (investigação com filtros completos). Persistido por navegador.
let appMode = localStorage.getItem("mesDashboardMode") || "online";

// Janela das tabelas de falhas no modo ONLINE: "hour" (última hora, padrão)
// ou "period" (mesmo período dos gráficos)
let matrixView = "hour";

// Timestamp do último dado recebido no banco (atualizado a cada refresh)
let lastDataTime = null;

// Ciclos acumulados por carrier (vida útil), atualizado a cada refresh
let carrierCyclesList = [];
let carrierCyclesMap = {};

function persistConfig() {
    localStorage.setItem(CONFIG_KEY, JSON.stringify(config));
}

function applyRefreshInterval() {
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = setInterval(loadDashboard, Math.max(5, config.refreshSeconds) * 1000);
}

function applyTvMode() {
    document.body.classList.toggle("tv-mode", !!config.tvMode);
    if (config.tvMode) {
        document.body.classList.add("sidebar-collapsed");
    }
}

function pad2(value) {
    return String(value).padStart(2, "0");
}

function toDatetimeLocal(date) {
    return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}T${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

function setDefaultDates(reference) {
    // Período padrão começa às 05:00 — a produção pode iniciar antes das
    // 06:00 em caso de hora extra.
    const now = reference ? new Date(reference) : new Date();
    const start = new Date(now);
    start.setHours(5, 0, 0, 0);

    if (now < start) {
        start.setDate(start.getDate() - 1);
    }

    document.getElementById("filterDateFrom").value = toDatetimeLocal(start);
    document.getElementById("filterDateTo").value = toDatetimeLocal(now);
}

function onlineAutoDates() {
    // Modo ONLINE: 05:00 → agora. Se o banco está atrasado (sem dados novos
    // além do limiar), ancora a janela no último dado recebido para o painel
    // não ficar vazio.
    const now = new Date();
    const stale = lastDataTime &&
        (now.getTime() - lastDataTime.getTime()) / 60000 > config.onlineThresholdMinutes;
    setDefaultDates(stale ? lastDataTime : now);
}

function parseServerTimestamp(value) {
    if (!value) return null;
    const parsed = new Date(String(value).replace(" ", "T"));
    return isNaN(parsed.getTime()) ? null : parsed;
}

function formatDateTimePtBr(date) {
    return date.toLocaleString("pt-BR", {
        day: "2-digit", month: "2-digit", year: "numeric",
        hour: "2-digit", minute: "2-digit"
    });
}

function formatDurationSince(date) {
    const totalMinutes = Math.max(0, Math.floor((Date.now() - date.getTime()) / 60000));
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    if (hours <= 0) return `${minutes} min`;
    return `${hours}h${String(minutes).padStart(2, "0")}min`;
}

function showStatusBanner(kind, message, autoHideMs) {
    const banner = document.getElementById("statusBanner");
    const text = document.getElementById("statusBannerText");

    banner.classList.remove("status-online", "status-offline");
    banner.classList.add(kind === "online" ? "status-online" : "status-offline");
    text.textContent = message;
    banner.hidden = false;

    if (banner._hideTimer) clearTimeout(banner._hideTimer);
    if (autoHideMs) {
        banner._hideTimer = setTimeout(() => { banner.hidden = true; }, autoHideMs);
    }
}

async function checkOnlineStatus() {
    let data;
    try {
        data = await fetchJson("/api/debug/");
    } catch (err) {
        showStatusBanner("offline", "Não foi possível verificar a conexão com o banco de dados.");
        return;
    }

    const lastSeen = parseServerTimestamp(data.last_created_at) || parseServerTimestamp(data.last_event_time);

    if (!lastSeen) {
        showStatusBanner("offline", "Nenhum dado encontrado na base ainda.");
        return;
    }

    const diffMinutes = (Date.now() - lastSeen.getTime()) / 60000;

    if (diffMinutes <= config.onlineThresholdMinutes) {
        showStatusBanner("online", `ONLINE — recebendo dados em tempo real (último registro às ${formatDateTimePtBr(lastSeen)})`, 12000);
    } else {
        showStatusBanner("offline", `OFFLINE — sem dados novos há ${formatDurationSince(lastSeen)}. Último dado recebido em ${formatDateTimePtBr(lastSeen)} — filtros ajustados para esse período.`);
        setDefaultDates(lastSeen);
    }
}

function toggleSidebar() {
    document.body.classList.toggle("sidebar-collapsed");
}

// ---------------------------------------------------------------------------
// FAIXA DE STATUS + MODOS ONLINE / ANÁLISE
// ---------------------------------------------------------------------------

async function refreshDebugStatus() {
    let data = null;
    try {
        data = await fetchJson("/api/debug/");
    } catch (err) {
        // banco inacessível — tratado abaixo
    }

    const stripState = document.getElementById("stripState");
    const stripUpdated = document.getElementById("stripUpdated");
    const lastSeen = data
        ? (parseServerTimestamp(data.last_created_at) || parseServerTimestamp(data.last_event_time))
        : null;

    lastDataTime = lastSeen;

    if (!lastSeen) {
        stripState.className = "strip-state state-offline";
        stripState.textContent = "● SEM CONEXÃO";
        stripUpdated.textContent = "Banco de dados inacessível ou sem registros";
        return;
    }

    const diffMinutes = (Date.now() - lastSeen.getTime()) / 60000;
    const online = diffMinutes <= config.onlineThresholdMinutes;

    stripState.className = `strip-state ${online ? "state-online" : "state-offline"}`;
    stripState.textContent = online ? "● RECEBENDO DADOS" : "● SEM DADOS NOVOS";
    stripUpdated.textContent =
        `Última atualização do banco: ${formatDateTimePtBr(lastSeen)}` +
        (online ? "" : ` — há ${formatDurationSince(lastSeen)}`);
}

function updateModeUI() {
    document.body.classList.toggle("mode-online", appMode === "online");
    document.body.classList.toggle("mode-analise", appMode === "analise");
    document.getElementById("modeOnline").classList.toggle("active", appMode === "online");
    document.getElementById("modeAnalise").classList.toggle("active", appMode === "analise");

    if (appMode === "analise") {
        document.body.classList.remove("sidebar-collapsed");
    } else {
        document.body.classList.add("sidebar-collapsed");
    }
}

async function switchMode(mode) {
    appMode = mode;
    localStorage.setItem("mesDashboardMode", mode);

    if (mode === "online") {
        // Filtros de investigação não valem no modo tempo real
        ["filterChannel", "filterCarrier", "filterStep"].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.value = "";
        });
    }

    updateModeUI();
    await loadDashboard();
}

// ---------------------------------------------------------------------------
// CICLOS DOS CARRIERS (vida útil) + AVISO AUTOMÁTICO DE LIMITE
// ---------------------------------------------------------------------------

async function postJson(url, body) {
    const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {})
    });
    if (!response.ok) {
        throw new Error(`HTTP ${response.status} - ${url}`);
    }
    return await response.json();
}

// Limite efetivo: o individual do carrier (se definido) ou o global
function effectiveCycleLimit(item) {
    return Number(item && item.cycle_limit ? item.cycle_limit : config.carrierCycleLimit) || 0;
}

async function loadCarrierCycles() {
    try {
        carrierCyclesList = await fetchJson("/api/carrier-cycles/");
    } catch (err) {
        carrierCyclesList = [];
    }

    carrierCyclesMap = {};
    carrierCyclesList.forEach(c => { carrierCyclesMap[c.carrier] = c; });

    renderCarrierAlert();
}

function renderCarrierAlert() {
    const el = document.getElementById("carrierAlert");
    if (!el) return;

    const over = carrierCyclesList.filter(c => {
        const limit = effectiveCycleLimit(c);
        return limit > 0 && Number(c.cycles) >= limit;
    });
    const near = carrierCyclesList.filter(c => {
        const limit = effectiveCycleLimit(c);
        return limit > 0 && Number(c.cycles) >= limit * 0.9 && Number(c.cycles) < limit;
    });

    if (over.length === 0 && near.length === 0) {
        el.hidden = true;
        return;
    }

    const parts = [];
    if (over.length > 0) {
        const names = over.map(c =>
            `${c.carrier} (${Number(c.cycles).toLocaleString("pt-BR")}/${effectiveCycleLimit(c).toLocaleString("pt-BR")} ciclos)`).join(", ");
        parts.push(`⚠ LIMITE DE CICLOS ATINGIDO — substituir carrier: ${names}`);
    }
    if (near.length > 0) {
        const names = near.map(c =>
            `${c.carrier} (${Number(c.cycles).toLocaleString("pt-BR")} — ${Math.round(100 * Number(c.cycles) / effectiveCycleLimit(c))}%)`).join(", ");
        parts.push(`Aproximando do limite: ${names}`);
    }

    el.textContent = parts.join("  •  ") + "  —  clique para gerenciar";
    el.classList.toggle("carrier-alert-over", over.length > 0);
    el.classList.toggle("carrier-alert-near", over.length === 0);
    el.hidden = false;
}

function cycleBadgeHtml(carrierName) {
    const item = carrierCyclesMap[carrierName];
    if (!item) return "";

    const cycles = Number(item.cycles || 0);
    const limit = effectiveCycleLimit(item);
    let cls = "cycle-ok";
    if (limit > 0 && cycles >= limit) cls = "cycle-over";
    else if (limit > 0 && cycles >= limit * 0.9) cls = "cycle-near";

    return ` <span class="cycle-badge ${cls}" title="Ciclos acumulados do carrier (limite ${limit.toLocaleString("pt-BR")})">${cycles.toLocaleString("pt-BR")} cic.</span>`;
}

// ---------------------------------------------------------------------------
// TELA DE GESTÃO DOS CARRIERS (zerar ao substituir, limite individual)
// ---------------------------------------------------------------------------

function formatBaseline(item) {
    const baseline = parseServerTimestamp(item.baseline_at);
    if (baseline && baseline.getFullYear() > 1971) {
        return `desde ${formatDateTimePtBr(baseline)}`;
    }
    return "desde o 1º registro no banco";
}

function renderCarrierManager() {
    const tbody = document.querySelector("#carrierManagerTable tbody");
    tbody.innerHTML = "";

    if (carrierCyclesList.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="6" class="cm-empty">Nenhum carrier registrado no banco ainda.</td>`;
        tbody.appendChild(tr);
        return;
    }

    carrierCyclesList.forEach(item => {
        const cycles = Number(item.cycles || 0);
        const limit = effectiveCycleLimit(item);
        const pct = limit > 0 ? Math.round((100 * cycles) / limit) : 0;

        let statusCls = "cm-ok", statusTxt = `${pct}%`;
        if (limit > 0 && cycles >= limit) { statusCls = "cm-over"; statusTxt = "SUBSTITUIR"; }
        else if (limit > 0 && cycles >= limit * 0.9) { statusCls = "cm-near"; statusTxt = `${pct}% — atenção`; }

        const tr = document.createElement("tr");
        tr.innerHTML = `
            <td class="cm-name">${item.carrier}</td>
            <td class="cm-cycles">${cycles.toLocaleString("pt-BR")}</td>
            <td class="cm-baseline">${formatBaseline(item)}</td>
            <td class="cm-limit">
                <input type="number" min="100" step="100" class="cm-limit-input"
                       value="${item.cycle_limit || ""}"
                       placeholder="${Number(config.carrierCycleLimit).toLocaleString("pt-BR")} (global)">
            </td>
            <td><span class="cm-status ${statusCls}">${statusTxt}</span></td>
            <td><button type="button" class="cm-reset-btn">Zerar</button></td>
        `;

        tr.querySelector(".cm-reset-btn").addEventListener("click", async () => {
            const ok = confirm(
                `Zerar a contagem de ciclos do carrier ${item.carrier}?\n\n` +
                `Use ao SUBSTITUIR o carrier físico. A contagem atual ` +
                `(${cycles.toLocaleString("pt-BR")} ciclos) recomeça do zero a partir de agora.`
            );
            if (!ok) return;
            try {
                await postJson("/api/carriers/reset/", { carrier: item.carrier, notes: "Zerado pelo dashboard" });
                await loadCarrierCycles();
                renderCarrierManager();
                await loadCarrierChannelMatrix();
            } catch (err) {
                alert(`Erro ao zerar: ${err.message}`);
            }
        });

        tr.querySelector(".cm-limit-input").addEventListener("change", async event => {
            const raw = event.target.value.trim();
            const newLimit = raw === "" ? null : Math.max(100, parseInt(raw, 10) || 0);
            try {
                await postJson("/api/carriers/limit/", { carrier: item.carrier, limit: newLimit });
                await loadCarrierCycles();
                renderCarrierManager();
                await loadCarrierChannelMatrix();
            } catch (err) {
                alert(`Erro ao salvar limite: ${err.message}`);
            }
        });

        tbody.appendChild(tr);
    });
}

// ---------------------------------------------------------------------------
// EEData / SPC — distribuição paramétrica (histograma + Cp/Cpk) de um step
// ---------------------------------------------------------------------------

// Classificação de Cpk compartilhada entre o modal de detalhe (um step) e a
// visão geral (todos os steps) — mesmos limiares em um único lugar.
function cpkClassify(cpk) {
    if (cpk == null) return { cls: "cpk-poor", label: "--" };
    if (cpk >= 1.67) return { cls: "cpk-excellent", label: "EXCELENTE" };
    if (cpk >= 1.33) return { cls: "cpk-good", label: "BOM" };
    if (cpk >= 1.0) return { cls: "cpk-acceptable", label: "ACEITÁVEL" };
    return { cls: "cpk-poor", label: "INCAPAZ" };
}

function valueToIndex(value, bins) {
    if (!bins || !bins.length) return 0;
    const total = bins[bins.length - 1].x_end - bins[0].x;
    if (total === 0) return 0;
    return ((value - bins[0].x) / total) * bins.length - 0.5;
}

async function loadSpcDistribution() {
    const step = document.getElementById("stepSelect").value;
    if (!step) return;

    const usl = document.getElementById("spcUsl").value;
    const lsl = document.getElementById("spcLsl").value;

    const params = new URLSearchParams(getFiltersQuery());
    params.set("step", step);
    if (usl) params.set("usl", usl);
    if (lsl) params.set("lsl", lsl);

    try {
        const data = await fetchJson(`/api/spc/distribution/?${params.toString()}`);
        renderSpcPanel(data);
    } catch (e) {
        console.error("SPC load error:", e);
    }
}

function renderSpcPanel(data) {
    document.getElementById("spcStepLabel").textContent = data.step || "--";

    const stats = data.stats || {};
    const limits = data.limits || {};
    const capability = data.capability || {};
    const n = data.count || 0;

    document.getElementById("statCount").textContent = n.toLocaleString("pt-BR");
    document.getElementById("statMean").textContent = stats.mean != null ? Number(stats.mean).toFixed(6) : "--";
    document.getElementById("statStd").textContent = stats.std != null ? Number(stats.std).toFixed(6) : "--";
    document.getElementById("statMin").textContent = stats.min != null ? Number(stats.min).toFixed(6) : "--";
    document.getElementById("statMax").textContent = stats.max != null ? Number(stats.max).toFixed(6) : "--";
    document.getElementById("statP5").textContent = stats.p5 != null ? Number(stats.p5).toFixed(6) : "--";
    document.getElementById("statP95").textContent = stats.p95 != null ? Number(stats.p95).toFixed(6) : "--";

    const auto = limits.auto ? " (auto)" : "";
    document.getElementById("statUsl").textContent = limits.usl != null ? `${Number(limits.usl).toFixed(6)}${auto}` : "--";
    document.getElementById("statLsl").textContent = limits.lsl != null ? `${Number(limits.lsl).toFixed(6)}${auto}` : "--";
    document.getElementById("statCp").textContent = capability.cp != null ? Number(capability.cp).toFixed(3) : "--";
    document.getElementById("statCpk").textContent = capability.cpk != null ? Number(capability.cpk).toFixed(3) : "--";

    const badge = document.getElementById("cpkBadge");
    if (capability.cpk != null) {
        const cpk = Number(capability.cpk);
        const info = cpkClassify(cpk);
        badge.style.display = "";
        badge.textContent = `Cpk ${cpk.toFixed(3)} — ${info.label}`;
        badge.className = `cpk-badge ${info.cls}`;
    } else {
        badge.style.display = "none";
    }

    if (spcChart) { spcChart.destroy(); spcChart = null; }

    if (n === 0) return;

    const bins = data.bins || [];
    const labels = bins.map(b => Number(b.x).toPrecision(4));
    const values = bins.map(b => b.y);

    const annotations = {};
    if (limits.usl != null) {
        const pos = valueToIndex(limits.usl, bins);
        annotations.uslLine = {
            type: "line",
            xMin: pos, xMax: pos,
            borderColor: "#ef4444",
            borderWidth: 2,
            borderDash: [6, 3],
            label: {
                display: true,
                content: `USL ${Number(limits.usl).toFixed(4)}`,
                position: "start",
                backgroundColor: "rgba(239,68,68,0.85)",
                color: "#fff",
                font: { weight: "bold", size: 11 }
            }
        };
    }
    if (limits.lsl != null) {
        const pos = valueToIndex(limits.lsl, bins);
        annotations.lslLine = {
            type: "line",
            xMin: pos, xMax: pos,
            borderColor: "#3b82f6",
            borderWidth: 2,
            borderDash: [6, 3],
            label: {
                display: true,
                content: `LSL ${Number(limits.lsl).toFixed(4)}`,
                position: "start",
                backgroundColor: "rgba(59,130,246,0.85)",
                color: "#fff",
                font: { weight: "bold", size: 11 }
            }
        };
    }
    if (stats.mean != null) {
        const pos = valueToIndex(stats.mean, bins);
        annotations.meanLine = {
            type: "line",
            xMin: pos, xMax: pos,
            borderColor: "#f59e0b",
            borderWidth: 2,
            label: {
                display: true,
                content: `μ ${Number(stats.mean).toFixed(4)}`,
                position: "end",
                backgroundColor: "rgba(245,158,11,0.85)",
                color: "#111827",
                font: { weight: "bold", size: 11 }
            }
        };
    }

    const ctx = document.getElementById("spcChart");
    spcChart = new Chart(ctx, {
        type: "bar",
        data: {
            labels: labels,
            datasets: [{
                label: data.step,
                data: values,
                backgroundColor: "rgba(99, 102, 241, 0.75)",
                borderColor: "#818cf8",
                borderWidth: 1,
                barPercentage: 1.0,
                categoryPercentage: 1.0
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                datalabels: { display: false },
                annotation: { annotations }
            },
            scales: {
                x: {
                    title: { display: true, text: data.step, color: "#94a3b8" },
                    ticks: { maxTicksLimit: 12, maxRotation: 45, color: "#94a3b8" },
                    grid: { color: "rgba(148, 163, 184, 0.08)" }
                },
                y: {
                    title: { display: true, text: "Frequência", color: "#94a3b8" },
                    beginAtZero: true,
                    grid: { color: "rgba(148, 163, 184, 0.08)" }
                }
            }
        }
    });
}

async function openSpcPanel() {
    const step = document.getElementById("stepSelect").value;
    if (!step) return;
    document.getElementById("spcOverlay").hidden = false;
    await loadSpcDistribution();
}

function closeSpcPanel() {
    document.getElementById("spcOverlay").hidden = true;
    if (spcChart) { spcChart.destroy(); spcChart = null; }
}

// ---------------------------------------------------------------------------
// EEData / SPC — visão geral de Cp/Cpk de TODOS os steps paramétricos
// ---------------------------------------------------------------------------

// Popula o <select id="stepSelect"> com os steps que realmente têm dado
// numérico (vindo de /api/spc/overview/) — substitui a lista fixa antiga do
// HTML, que não cobria todos os steps do CSV (ex.: R1T, STC, DOCD2 etc.).
function fillStepSelect(steps) {
    const select = document.getElementById("stepSelect");
    const currentValue = select.value;
    select.innerHTML = "";

    steps.forEach(step => {
        const opt = document.createElement("option");
        opt.value = step;
        opt.textContent = step;
        select.appendChild(opt);
    });

    if (currentValue && steps.includes(currentValue)) {
        select.value = currentValue;
    }
}

async function refreshStepSpecsOverview() {
    const query = getFiltersQuery();
    let rows;
    try {
        rows = await fetchJson(`/api/spc/overview/?${query}`);
    } catch (err) {
        rows = [];
    }
    fillStepSelect(rows.map(r => r.step));
    return rows;
}

function renderStepSpecsTable(rows) {
    const tbody = document.querySelector("#stepSpecsTable tbody");
    tbody.innerHTML = "";

    if (!rows.length) {
        tbody.innerHTML = `<tr><td colspan="7" class="cm-empty">Nenhum step paramétrico com dado numérico no filtro atual.</td></tr>`;
        return;
    }

    rows.forEach(row => {
        const tr = document.createElement("tr");
        const autoTag = row.limits_auto ? " (auto)" : "";

        let statusCls, statusTxt;
        if (row.limits_valid === false) {
            statusCls = "cm-over";
            statusTxt = "LIMITES INVERTIDOS NO SCHEMA";
        } else if (row.cpk == null) {
            statusCls = "cm-ok";
            statusTxt = "--";
        } else {
            const info = cpkClassify(row.cpk);
            statusCls = info.cls === "cpk-poor" ? "cm-over"
                : info.cls === "cpk-acceptable" ? "cm-near" : "cm-ok";
            statusTxt = `${Number(row.cpk).toFixed(3)} — ${info.label}`;
        }

        tr.innerHTML = `
            <td class="cm-name">${row.step}</td>
            <td>${row.unit || ""}</td>
            <td class="cm-cycles">${Number(row.count).toLocaleString("pt-BR")}</td>
            <td><input type="number" step="any" class="cm-limit-input step-lsl-input"
                       value="${row.lsl_is_override ? row.lsl : ""}"
                       placeholder="${Number(row.lsl).toFixed(4)}${autoTag}"></td>
            <td><input type="number" step="any" class="cm-limit-input step-usl-input"
                       value="${row.usl_is_override ? row.usl : ""}"
                       placeholder="${Number(row.usl).toFixed(4)}${autoTag}"></td>
            <td><span class="cm-status ${statusCls}">${statusTxt}</span></td>
            <td><button type="button" class="cm-reset-btn step-detail-btn">Detalhar</button></td>
        `;

        const saveOverride = async () => {
            const lslRaw = tr.querySelector(".step-lsl-input").value;
            const uslRaw = tr.querySelector(".step-usl-input").value;
            try {
                await postJson("/api/spc/specs/set/", {
                    step: row.step,
                    lsl: lslRaw === "" ? null : parseFloat(lslRaw),
                    usl: uslRaw === "" ? null : parseFloat(uslRaw)
                });
                const rows2 = await refreshStepSpecsOverview();
                renderStepSpecsTable(rows2);
            } catch (err) {
                alert(`Erro ao salvar limite: ${err.message}`);
            }
        };

        tr.querySelector(".step-lsl-input").addEventListener("change", saveOverride);
        tr.querySelector(".step-usl-input").addEventListener("change", saveOverride);

        tr.querySelector(".step-detail-btn").addEventListener("click", async () => {
            document.getElementById("stepSelect").value = row.step;
            // Carrega o MESMO limite que a visão geral usou (schema ou
            // override) no detalhe — sem isso, o detalhe recalcularia com
            // seu próprio μ±3σ e mostraria um Cpk diferente do que acabou
            // de aparecer na tabela, o que confundiria o usuário.
            document.getElementById("spcUsl").value = row.limits_auto ? "" : row.usl;
            document.getElementById("spcLsl").value = row.limits_auto ? "" : row.lsl;
            closeStepSpecsManager();
            await openSpcPanel();
        });

        tbody.appendChild(tr);
    });
}

async function openStepSpecsManager() {
    document.getElementById("stepSpecsOverlay").hidden = false;
    const rows = await refreshStepSpecsOverview();
    renderStepSpecsTable(rows);
}

function closeStepSpecsManager() {
    document.getElementById("stepSpecsOverlay").hidden = true;
}

async function openCarrierManager() {
    await loadCarrierCycles();
    renderCarrierManager();
    document.getElementById("carrierManagerOverlay").hidden = false;
}

function closeCarrierManager() {
    document.getElementById("carrierManagerOverlay").hidden = true;
}

function setMatrixView(view) {
    matrixView = view;
    document.getElementById("mtHour").classList.toggle("active", view === "hour");
    document.getElementById("mtPeriod").classList.toggle("active", view === "period");
    Promise.all([loadFailureChannelMatrix(), loadCarrierChannelMatrix()]);
}

function getFiltersQuery() {
    const params = new URLSearchParams();

    const station = document.getElementById("filterStation").value;
    const model = document.getElementById("filterModel").value;
    const channel = document.getElementById("filterChannel").value;
    const carrier = document.getElementById("filterCarrier").value;
    const step = document.getElementById("filterStep").value;
    const dateFrom = document.getElementById("filterDateFrom").value;
    const dateTo = document.getElementById("filterDateTo").value;

    if (station) params.append("station", station);
    if (model) params.append("model", model);
    if (channel) params.append("channel", channel);
    if (carrier) params.append("carrier", carrier);
    if (step) params.append("failure", step);
    if (dateFrom) params.append("date_from", dateFrom.replace("T", " ") + ":00");
    if (dateTo) params.append("date_to", dateTo.replace("T", " ") + ":00");

    return params.toString();
}

function getMatrixQuery() {
    // Modo ONLINE + "Última hora": tabelas mostram a última hora de dados
    // recebidos (ancorada no último registro do banco, não no relógio).
    if (appMode === "online" && matrixView === "hour" && lastDataTime) {
        const params = new URLSearchParams();

        const station = document.getElementById("filterStation").value;
        const model = document.getElementById("filterModel").value;
        if (station) params.append("station", station);
        if (model) params.append("model", model);

        const end = new Date(lastDataTime);
        const start = new Date(end.getTime() - 60 * 60000);
        params.append("date_from", toDatetimeLocal(start).replace("T", " ") + ":00");
        params.append("date_to", toDatetimeLocal(end).replace("T", " ") + ":59");

        return params.toString();
    }

    return getFiltersQuery();
}

async function fetchJson(url) {
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) {
        throw new Error(`HTTP ${response.status} - ${url}`);
    }
    return await response.json();
}

function fillSelect(selectElement, defaultText, values) {
    const currentValue = selectElement.value;
    selectElement.innerHTML = "";

    const defaultOption = document.createElement("option");
    defaultOption.value = "";
    defaultOption.textContent = defaultText;
    selectElement.appendChild(defaultOption);

    (values || []).forEach(value => {
        const opt = document.createElement("option");
        opt.value = value;
        opt.textContent = value;
        selectElement.appendChild(opt);
    });

    if (currentValue && (values || []).includes(currentValue)) {
        selectElement.value = currentValue;
    }
}

async function loadFilterOptions() {
    const data = await fetchJson("/api/filters/");
    fillSelect(document.getElementById("filterStation"), "Todas as estações", data.stations || []);
    fillSelect(document.getElementById("filterModel"), "Todos os modelos", data.models || []);
    fillSelect(document.getElementById("filterCarrier"), "Todos os carriers", data.carriers || []);
    fillSelect(document.getElementById("filterStep"), "Todos os steps", data.failures || []);
}

function fillChannelSelect() {
    const select = document.getElementById("filterChannel");
    for (let ch = 1; ch <= 20; ch++) {
        const opt = document.createElement("option");
        opt.value = String(ch);
        opt.textContent = `Canal ${ch}`;
        select.appendChild(opt);
    }
}

function updateTitle() {
    const model = document.getElementById("filterModel").value;
    const station = document.getElementById("filterStation").value;
    const title = document.getElementById("dashboardTitle");

    if (model && station) {
        title.textContent = `DASHBOARD FT ${model} - ${station} MONITORAMENTO EM TEMPO REAL`;
    } else if (model) {
        title.textContent = `DASHBOARD FT ${model} - MONITORAMENTO EM TEMPO REAL`;
    } else {
        title.textContent = "DASHBOARD FT - MONITORAMENTO EM TEMPO REAL";
    }
}

async function loadSummary() {
    const query = getFiltersQuery();
    const data = await fetchJson(`/api/summary/?${query}`);

    const yieldValue = Number(data.yield_percent || 0);
    document.getElementById("yieldValue").innerText = `${yieldValue.toFixed(2)}%`;
    document.getElementById("passValue").innerText = Number(data.pass_count || 0).toLocaleString("pt-BR");
    document.getElementById("failValue").innerText = Number(data.fail_count || 0).toLocaleString("pt-BR");
}

async function loadTopFailures() {
    const query = getFiltersQuery();
    const data = await fetchJson(`/api/top-failures/?${query}&limit=${config.paretoTopN}`);

    const labels = data.map(x => x.failure);
    const values = data.map(x => Number(x.total || 0));
    const ctx = document.getElementById("topFailuresChart");

    if (topFailuresChart) topFailuresChart.destroy();

    topFailuresChart = new Chart(ctx, {
        type: "bar",
        data: {
            labels: labels,
            datasets: [{
                label: "Falhas",
                data: values,
                borderWidth: 1
            }]
        },
        options: {
            indexAxis: "y",
            responsive: true,
            maintainAspectRatio: false,
            layout: { padding: { right: 46 } },
            plugins: {
                legend: { display: false },
                datalabels: {
                    anchor: "end",
                    align: "right",
                    color: "#e5e7eb",
                    font: { weight: "bold", size: 10 },
                    formatter: value => Number(value || 0).toLocaleString("pt-BR")
                }
            },
            scales: {
                x: { beginAtZero: true, grid: { color: "rgba(148, 163, 184, 0.08)" }, ticks: { font: { size: 9 } } },
                y: {
                    grid: { display: false },
                    ticks: { autoSkip: false, font: { size: 9 } }
                }
            }
        }
    });
}

async function loadHourlyYield() {
    const query = getFiltersQuery();
    const data = await fetchJson(`/api/hourly-yield/?${query}`);

    const labels = data.map(x => x.hour_short || x.hour_label);
    const pass = data.map(x => Number(x.pass_count || 0));
    const fail = data.map(x => Number(x.fail_count || 0));
    // Yield só nas horas com produção — sem produção não há yield a mostrar
    const yieldPct = data.map(x => Number(x.total || 0) > 0 ? Number(x.yield_percent || 0) : null);
    const goalLine = labels.map(() => config.productionGoal);
    const ctx = document.getElementById("hourlyYieldChart");

    if (hourlyYieldChart) hourlyYieldChart.destroy();

    hourlyYieldChart = new Chart(ctx, {
        type: "line",
        data: {
            labels: labels,
            datasets: [
                {
                    label: "PASS",
                    data: pass,
                    yAxisID: "y",
                    borderColor: "#3987e5",
                    backgroundColor: "#3987e5",
                    tension: 0.25,
                    borderWidth: 2,
                    pointRadius: 4,
                    pointBackgroundColor: "#3987e5",
                    datalabels: {
                        anchor: "end",
                        align: "top",
                        color: "#3987e5",
                        font: { weight: "bold", size: 9 },
                        formatter: value => value > 0 ? value.toLocaleString("pt-BR") : "0"
                    }
                },
                {
                    label: "FAIL",
                    data: fail,
                    yAxisID: "y",
                    borderColor: "#e34948",
                    backgroundColor: "#e34948",
                    tension: 0.25,
                    borderWidth: 2,
                    pointRadius: 4,
                    pointBackgroundColor: "#e34948",
                    datalabels: {
                        anchor: "end",
                        align: "bottom",
                        color: "#e34948",
                        font: { weight: "bold", size: 9 },
                        formatter: value => value > 0 ? value.toLocaleString("pt-BR") : "0"
                    }
                },
                {
                    label: "YIELD %",
                    data: yieldPct,
                    yAxisID: "y2",
                    borderColor: "#22c55e",
                    backgroundColor: "#22c55e",
                    tension: 0.25,
                    borderWidth: 2,
                    pointRadius: 3,
                    pointBackgroundColor: "#22c55e",
                    spanGaps: false,
                    datalabels: {
                        anchor: "end",
                        align: "top",
                        clamp: true,
                        clip: false,
                        color: "#86efac",
                        font: { weight: "bold", size: 10 },
                        display: ctx2 => ctx2.dataset.data[ctx2.dataIndex] !== null,
                        formatter: value => value !== null ? `${Number(value).toFixed(1)}%` : ""
                    }
                },
                {
                    label: `META ${Number(config.productionGoal).toLocaleString("pt-BR")}`,
                    data: goalLine,
                    yAxisID: "y",
                    borderColor: "#f59e0b",
                    backgroundColor: "#f59e0b",
                    borderDash: [8, 6],
                    borderWidth: 2,
                    pointRadius: 0,
                    pointHitRadius: 0,
                    datalabels: { display: false }
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            // Padding no topo para os rótulos de yield (~100%) não serem cortados
            layout: { padding: { top: 22, left: 6, right: 6 } },
            plugins: { legend: { position: "right" } },
            scales: {
                x: { ticks: { maxRotation: 45, minRotation: 45, font: { size: 9 } }, grid: { display: false } },
                // Eixos ocultos: os valores estão nos rótulos dos pontos
                y: {
                    display: false,
                    beginAtZero: true,
                    suggestedMax: config.uphYMax
                },
                y2: {
                    display: false,
                    min: 0,
                    max: 100
                }
            }
        }
    });
}

// Uma hora só entra na análise de intermitência com amostra mínima de testes
// (evita marcar canal por 1-2 peças isoladas)
const MIN_TESTS_PER_HOUR = 5;

function computeIntermittency(hourlyRows) {
    // Agrupa as horas por canal, na ordem cronológica (o SQL já ordena)
    const byChannel = {};
    (hourlyRows || []).forEach(row => {
        const ch = row.channel_no;
        if (ch === null || ch === undefined) return;
        (byChannel[ch] = byChannel[ch] || []).push(row);
    });

    // Canal intermitente = alterna entre hora normal e hora CRÍTICA (falha e
    // "recupera" sozinho): ≥2 alternâncias em ≥3 horas com produção, com as
    // horas ruins em minoria (≤70% — acima disso é degradação contínua, não
    // intermitência, e o chip vermelho/amarelo pelo yield geral cobre o caso).
    // Hora ruim usa o limiar CRÍTICO (yieldRed): o limiar amarelo marcaria
    // quase toda hora como ruim em dias de yield baixo, gerando alarme falso.
    const result = {};
    Object.entries(byChannel).forEach(([ch, rows]) => {
        const sequence = rows
            .filter(r => Number(r.total || 0) >= MIN_TESTS_PER_HOUR)
            .map(r => {
                const total = Number(r.total || 0);
                const yieldPct = total > 0 ? (100 * Number(r.pass_count || 0)) / total : 100;
                return yieldPct < config.yieldRed ? 1 : 0;   // 1 = hora crítica
            });

        let transitions = 0;
        for (let i = 1; i < sequence.length; i++) {
            if (sequence[i] !== sequence[i - 1]) transitions++;
        }

        const badHours = sequence.filter(v => v === 1).length;

        result[ch] = {
            hours: sequence.length,
            badHours: badHours,
            transitions: transitions,
            intermittent:
                sequence.length >= 3 &&
                transitions >= 2 &&
                badHours >= 1 &&
                badHours <= sequence.length * 0.7
        };
    });

    return result;
}

function renderChannelHealth(channels, intermittency) {
    const wrap = document.getElementById("channelHealth");
    if (!wrap) return;

    wrap.innerHTML = "";

    channels
        .filter(c => c.channel !== null && c.channel !== undefined)
        .forEach(c => {
            const total = Number(c.total || 0);
            const yieldPct = Number(c.yield_percent || 0);
            const info = (intermittency || {})[String(c.channel)] || (intermittency || {})[c.channel];

            let cls = "ch-off";
            let label = "sem produção no período";
            if (total > 0) {
                if (yieldPct < config.yieldRed) {
                    cls = "ch-bad";
                    label = "CRÍTICO — acionar manutenção";
                } else if (info && info.intermittent) {
                    cls = "ch-int";
                    label = `INTERMITENTE — falha e recupera sozinho (${info.badHours} hora(s) ruim(ns) de ${info.hours}, ${info.transitions} alternâncias) — verificar fixture/conexão do canal`;
                } else if (yieldPct < config.yieldYellow) {
                    cls = "ch-warn";
                    label = "atenção — canal degradando";
                } else {
                    cls = "ch-good";
                    label = "saudável";
                }
            }

            const chip = document.createElement("span");
            chip.className = `ch-chip ${cls}`;
            chip.textContent = c.channel;
            chip.title = `Canal ${c.channel}: yield ${yieldPct.toFixed(1)}% (${total.toLocaleString("pt-BR")} testes) — ${label}`;
            wrap.appendChild(chip);
        });
}

async function loadChannelsChart() {
    const query = getFiltersQuery();
    const [data, hourly] = await Promise.all([
        fetchJson(`/api/channels/?${query}`),
        fetchJson(`/api/channel-hourly/?${query}`)
    ]);

    renderChannelHealth(data, computeIntermittency(hourly));

    const labels = data.map(x => x.channel === null || x.channel === undefined ? "N/A" : String(x.channel));
    const pass = data.map(x => Number(x.pass_count || 0));
    const fail = data.map(x => Number(x.fail_count || 0));
    const ctx = document.getElementById("channelsChart");

    if (channelsChart) channelsChart.destroy();

    channelsChart = new Chart(ctx, {
        type: "bar",
        data: {
            labels: labels,
            datasets: [
                {
                    label: "PASSED",
                    data: pass,
                    backgroundColor: "#7c3aed",
                    borderRadius: 4,
                    maxBarThickness: 24,
                    datalabels: {
                        anchor: "end",
                        align: "top",
                        color: "#c4b5fd",
                        font: { weight: "bold", size: 8 },
                        formatter: value => value > 0 ? value.toLocaleString("pt-BR") : "0"
                    }
                },
                {
                    label: "FAILED",
                    data: fail,
                    backgroundColor: "#e34948",
                    borderRadius: 4,
                    maxBarThickness: 24,
                    datalabels: {
                        anchor: "end",
                        align: "top",
                        color: "#f4a3a2",
                        font: { weight: "bold", size: 8 },
                        formatter: value => value > 0 ? value.toLocaleString("pt-BR") : "0"
                    }
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            layout: { padding: { top: 16 } },
            plugins: { legend: { position: "bottom", labels: { boxWidth: 12, font: { size: 10 } } } },
            scales: {
                x: { grid: { display: false }, ticks: { font: { size: 9 } } },
                y: { beginAtZero: true, grid: { color: "rgba(148, 163, 184, 0.08)" } }
            }
        }
    });
}

function buildMatrixTable(tableId, data, firstColumnName) {
    const table = document.getElementById(tableId);
    const thead = table.querySelector("thead");
    const tbody = table.querySelector("tbody");
    let tfoot = table.querySelector("tfoot");

    if (!tfoot) {
        tfoot = document.createElement("tfoot");
        table.appendChild(tfoot);
    }

    thead.innerHTML = "";
    tbody.innerHTML = "";
    tfoot.innerHTML = "";

    const channels = data.channels || [];
    const hotLimit = Number(config.hotLimit ?? data.hot_limit ?? 5);
    const headerRow = document.createElement("tr");
    headerRow.innerHTML = `<th>${firstColumnName}</th>${channels.map(ch => `<th>${ch}</th>`).join("")}<th>Total</th>`;
    thead.appendChild(headerRow);

    const rows = data.rows || [];

    if (rows.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td class="row-title">Sem dados</td><td colspan="${channels.length + 1}" class="empty-cell">Nenhuma falha encontrada no filtro atual</td>`;
        tbody.appendChild(tr);
        return;
    }

    const channelTotals = {};
    channels.forEach(ch => { channelTotals[String(ch)] = 0; });
    let grandTotal = 0;
    const isCarrierTable = tableId === "carrierChannelMatrix";

    rows.forEach(row => {
        const tr = document.createElement("tr");
        const badge = isCarrierTable ? cycleBadgeHtml(row.name) : "";
        let html = `<td class="row-title" title="${row.name}">${row.name}${badge}</td>`;

        channels.forEach(ch => {
            const value = Number((row.values || {})[String(ch)] || 0);
            channelTotals[String(ch)] += value;
            const cls = value > hotLimit ? "hot-cell" : value > 0 ? "warn-cell" : "empty-cell";
            html += `<td class="${cls}">${value || ""}</td>`;
        });

        grandTotal += Number(row.total || 0);
        html += `<td class="total-cell">${Number(row.total || 0).toLocaleString("pt-BR")}</td>`;
        tr.innerHTML = html;
        tbody.appendChild(tr);
    });

    // Linha TOTAL por canal (fixa no rodapé da tabela)
    const totalRow = document.createElement("tr");
    totalRow.className = "total-row";
    let totalHtml = `<td class="row-title">TOTAL</td>`;
    channels.forEach(ch => {
        const value = channelTotals[String(ch)];
        totalHtml += `<td>${value > 0 ? value.toLocaleString("pt-BR") : ""}</td>`;
    });
    totalHtml += `<td>${grandTotal.toLocaleString("pt-BR")}</td>`;
    totalRow.innerHTML = totalHtml;
    tfoot.appendChild(totalRow);
}

async function loadFailureChannelMatrix() {
    const query = getMatrixQuery();
    const data = await fetchJson(`/api/channel-matrix/?${query}`);
    buildMatrixTable("failureChannelMatrix", data, "Falha");
}

async function loadCarrierChannelMatrix() {
    const query = getMatrixQuery();
    const data = await fetchJson(`/api/carrier-matrix/?${query}`);
    buildMatrixTable("carrierChannelMatrix", data, "Carrier");
}

async function loadDashboard() {
    // 1º: o banco está recebendo dados? (alimenta a faixa de status e a
    // âncora de tempo das tabelas "última hora"). Os ciclos dos carriers vêm
    // junto, antes das matrizes, para os badges terem os números prontos.
    await Promise.all([refreshDebugStatus(), loadCarrierCycles()]);

    if (appMode === "online") {
        onlineAutoDates();
    }

    updateTitle();
    await Promise.all([
        loadSummary(),
        loadTopFailures(),
        loadHourlyYield(),
        loadChannelsChart(),
        loadFailureChannelMatrix(),
        loadCarrierChannelMatrix()
    ]);
}

async function resetFilters() {
    document.getElementById("filterStation").value = "";
    document.getElementById("filterModel").value = "";
    setDefaultDates();
    await loadDashboard();
}

// ---------------------------------------------------------------------------
// TELA DE CONFIGURAÇÕES
// ---------------------------------------------------------------------------

function fillSettingsForm() {
    document.getElementById("cfgGoal").value = config.productionGoal;
    document.getElementById("cfgUphMax").value = config.uphYMax;
    document.getElementById("cfgHotLimit").value = config.hotLimit;
    document.getElementById("cfgRefresh").value = config.refreshSeconds;
    document.getElementById("cfgOnlineMin").value = config.onlineThresholdMinutes;
    document.getElementById("cfgParetoN").value = config.paretoTopN;
    document.getElementById("cfgYieldYellow").value = config.yieldYellow;
    document.getElementById("cfgYieldRed").value = config.yieldRed;
    document.getElementById("cfgCycleLimit").value = config.carrierCycleLimit;
    document.getElementById("cfgTvMode").checked = !!config.tvMode;
}

function openSettings() {
    fillSettingsForm();
    document.getElementById("settingsOverlay").hidden = false;
    document.getElementById("cfgGoal").focus();
}

function closeSettings() {
    document.getElementById("settingsOverlay").hidden = true;
}

function readNumber(id, fallback, min, max) {
    const value = Number(document.getElementById(id).value);
    if (isNaN(value)) return fallback;
    return Math.max(min, Math.min(max, value));
}

async function saveSettings() {
    const wasTvMode = !!config.tvMode;

    config.productionGoal = readNumber("cfgGoal", DEFAULT_CONFIG.productionGoal, 0, 1000000);
    config.uphYMax = readNumber("cfgUphMax", DEFAULT_CONFIG.uphYMax, 100, 1000000);
    config.hotLimit = readNumber("cfgHotLimit", DEFAULT_CONFIG.hotLimit, 0, 100000);
    config.refreshSeconds = readNumber("cfgRefresh", DEFAULT_CONFIG.refreshSeconds, 5, 3600);
    config.onlineThresholdMinutes = readNumber("cfgOnlineMin", DEFAULT_CONFIG.onlineThresholdMinutes, 1, 1440);
    config.paretoTopN = readNumber("cfgParetoN", DEFAULT_CONFIG.paretoTopN, 1, 20);
    config.yieldYellow = readNumber("cfgYieldYellow", DEFAULT_CONFIG.yieldYellow, 0, 100);
    config.yieldRed = readNumber("cfgYieldRed", DEFAULT_CONFIG.yieldRed, 0, 100);
    config.carrierCycleLimit = readNumber("cfgCycleLimit", DEFAULT_CONFIG.carrierCycleLimit, 100, 10000000);
    config.tvMode = document.getElementById("cfgTvMode").checked;

    persistConfig();
    applyRefreshInterval();
    applyTvMode();

    // Entrar em tela cheia só funciona a partir de um gesto do usuário —
    // o clique em "Salvar" é um.
    if (config.tvMode && !wasTvMode && document.documentElement.requestFullscreen) {
        document.documentElement.requestFullscreen().catch(() => {});
    }
    if (!config.tvMode && wasTvMode && document.fullscreenElement) {
        document.exitFullscreen().catch(() => {});
    }

    closeSettings();
    await loadDashboard();
}

async function resetSettings() {
    config = { ...DEFAULT_CONFIG };
    persistConfig();
    fillSettingsForm();
    applyRefreshInterval();
    applyTvMode();
    await loadDashboard();
}

// ---------------------------------------------------------------------------
// AMPLIAR PAINÉIS (estilo Streamlit: cada gráfico/tabela expande em tela cheia)
// ---------------------------------------------------------------------------

function togglePanelExpand(panel) {
    const wasExpanded = panel.classList.contains("panel-expanded");

    document.querySelectorAll(".panel-expanded").forEach(p => {
        p.classList.remove("panel-expanded");
        const b = p.querySelector(".panel-expand-btn");
        if (b) { b.textContent = "⤢"; b.title = "Ampliar"; }
    });

    if (!wasExpanded) {
        panel.classList.add("panel-expanded");
        const b = panel.querySelector(".panel-expand-btn");
        if (b) { b.textContent = "✕"; b.title = "Reduzir (Esc)"; }
    }
}

function wirePanelExpand() {
    document.querySelectorAll(".panel").forEach(panel => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "panel-expand-btn";
        btn.title = "Ampliar";
        btn.setAttribute("aria-label", "Ampliar painel");
        btn.textContent = "⤢";
        btn.addEventListener("click", () => togglePanelExpand(panel));
        panel.appendChild(btn);
    });

    document.addEventListener("keydown", event => {
        if (event.key === "Escape") {
            const open = document.querySelector(".panel-expanded");
            if (open) togglePanelExpand(open);
        }
    });
}

function wireSettingsEvents() {
    document.getElementById("settingsToggle").addEventListener("click", openSettings);
    document.getElementById("settingsClose").addEventListener("click", closeSettings);
    document.getElementById("settingsSave").addEventListener("click", saveSettings);
    document.getElementById("settingsReset").addEventListener("click", resetSettings);

    document.getElementById("settingsOverlay").addEventListener("click", event => {
        if (event.target === event.currentTarget) closeSettings();
    });

    document.addEventListener("keydown", event => {
        if (event.key === "Escape") {
            closeSettings();
            closeCarrierManager();
            closeSpcPanel();
            closeStepSpecsManager();
        }
    });
}

async function initDashboard() {
    document.body.classList.add("sidebar-collapsed");
    document.getElementById("sidebarToggle").addEventListener("click", toggleSidebar);
    document.getElementById("statusBannerClose").addEventListener("click", () => {
        document.getElementById("statusBanner").hidden = true;
    });

    document.getElementById("modeOnline").addEventListener("click", () => switchMode("online"));
    document.getElementById("modeAnalise").addEventListener("click", () => switchMode("analise"));
    document.getElementById("mtHour").addEventListener("click", () => setMatrixView("hour"));
    document.getElementById("mtPeriod").addEventListener("click", () => setMatrixView("period"));

    document.getElementById("carrierManagerBtn").addEventListener("click", openCarrierManager);
    document.getElementById("carrierManagerClose").addEventListener("click", closeCarrierManager);
    document.getElementById("spcClose").addEventListener("click", closeSpcPanel);
    document.getElementById("carrierAlert").addEventListener("click", openCarrierManager);
    document.getElementById("carrierManagerOverlay").addEventListener("click", event => {
        if (event.target === event.currentTarget) closeCarrierManager();
    });
    document.getElementById("spcOverlay").addEventListener("click", event => {
        if (event.target === event.currentTarget) closeSpcPanel();
    });

    document.getElementById("stepSpecsBtn").addEventListener("click", openStepSpecsManager);
    document.getElementById("stepSpecsClose").addEventListener("click", closeStepSpecsManager);
    document.getElementById("stepSpecsOverlay").addEventListener("click", event => {
        if (event.target === event.currentTarget) closeStepSpecsManager();
    });

    wireSettingsEvents();
    wirePanelExpand();
    fillChannelSelect();
    applyTvMode();
    updateModeUI();

    setDefaultDates();
    await loadFilterOptions();
    await refreshStepSpecsOverview();
    await checkOnlineStatus();
    await loadDashboard();
    applyRefreshInterval();
}

initDashboard();
