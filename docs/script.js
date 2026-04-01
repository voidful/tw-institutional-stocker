let ratioChart = null;
let useLogScale = false;
let marketFilter = "ALL";
let currentWindow = 20;

async function fetchJson(url) {
  const resp = await fetch(url);
  if (!resp.ok) {
    throw new Error(`HTTP ${resp.status}`);
  }
  return await resp.json();
}

function formatPct(x) {
  const v = Number.isFinite(x) ? x : 0;
  return v.toFixed(2);
}

function formatNumber(x) {
  const v = Number.isFinite(x) ? x : 0;
  return v.toLocaleString();
}

// ========== Stock Chart ==========

async function loadStock(code) {
  const status = document.getElementById("statusText");
  const title = document.getElementById("chartTitle");
  const btn = document.getElementById("loadBtn");

  code = (code || "").trim();
  if (!code) return;

  btn.disabled = true;
  status.textContent = `載入 ${code}...`;

  const showForeign = document.getElementById("showForeign").checked;
  const showTrust = document.getElementById("showTrust").checked;
  const showDealer = document.getElementById("showDealer").checked;
  const showTotal = document.getElementById("showTotal").checked;

  try {
    const data = await fetchJson(`data/timeseries/${code}.json`);
    if (!data.length) {
      status.textContent = `找不到 ${code} 資料`;
      btn.disabled = false;
      return;
    }

    const name = data[0].name || "";
    const market = data[0].market || "";
    title.textContent = `${code} ${name}（${market}）`;

    const labels = data.map((d) => d.date);
    const foreignRatio = data.map((d) => d.foreign_ratio);
    const trustRatio = data.map((d) => d.trust_ratio);
    const dealerRatio = data.map((d) => d.dealer_ratio);
    const totalRatio = data.map((d) => d.three_inst_ratio);

    const datasets = [];
    if (showForeign) {
      datasets.push({
        label: "外資%",
        data: foreignRatio,
        borderColor: "#ff6b6b",
        backgroundColor: "rgba(255, 107, 107, 0.1)",
        borderWidth: 2,
        tension: 0.3,
        fill: true,
      });
    }
    if (showTrust) {
      datasets.push({
        label: "投信%",
        data: trustRatio,
        borderColor: "#4ecdc4",
        borderWidth: 2,
        borderDash: [5, 3],
        tension: 0.3,
      });
    }
    if (showDealer) {
      datasets.push({
        label: "自營商%",
        data: dealerRatio,
        borderColor: "#ffe66d",
        borderWidth: 2,
        borderDash: [2, 2],
        tension: 0.3,
      });
    }
    if (showTotal) {
      datasets.push({
        label: "三法人合計%",
        data: totalRatio,
        borderColor: "#a55eea",
        borderWidth: 3,
        pointRadius: 0,
        tension: 0.3,
      });
    }

    const ctx = document.getElementById("ratioChart").getContext("2d");
    if (ratioChart) {
      ratioChart.destroy();
    }

    ratioChart = new Chart(ctx, {
      type: "line",
      data: { labels, datasets },
      options: {
        responsive: true,
        interaction: { mode: "index", intersect: false },
        scales: {
          x: {
            ticks: { maxTicksLimit: 8, color: "#8b8b9e" },
            grid: { color: "rgba(255,255,255,0.05)" },
          },
          y: {
            type: useLogScale ? "logarithmic" : "linear",
            title: { display: true, text: "持股比重 (%)", color: "#8b8b9e" },
            ticks: { color: "#8b8b9e" },
            grid: { color: "rgba(255,255,255,0.05)" },
            min: 0,
          },
        },
        plugins: {
          legend: { position: "bottom", labels: { color: "#eaeaea" } },
        },
      },
    });

    const last = data[data.length - 1];
    status.textContent = `${last.date} | 三大法人 ${formatPct(last.three_inst_ratio)}%`;
  } catch (err) {
    console.error(err);
    status.textContent = `載入失敗：${err.message}`;
  } finally {
    btn.disabled = false;
  }
}

// ========== Institutional Ranking ==========

async function loadRanking() {
  const tbody = document.querySelector("#rankTable tbody");
  tbody.innerHTML = "<tr><td colspan='5'>載入中...</td></tr>";

  try {
    const up = await fetchJson(`data/top_three_inst_change_${currentWindow}_up.json`);
    tbody.innerHTML = "";

    const filtered = up.filter((row) => {
      if (marketFilter === "ALL") return true;
      return row.market === marketFilter;
    });

    filtered.slice(0, 50).forEach((row, idx) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${idx + 1}</td>
        <td><span class="badge">${row.code}</span>${row.name || ""}</td>
        <td>${row.market || ""}</td>
        <td>${formatPct(row.three_inst_ratio)}</td>
        <td class="${row.change >= 0 ? 'net-positive' : 'net-negative'}">${row.change >= 0 ? '+' : ''}${formatPct(row.change)}</td>
      `;
      tr.addEventListener("click", () => {
        document.getElementById("stockInput").value = row.code;
        loadStock(row.code);
      });
      tbody.appendChild(tr);
    });
  } catch (err) {
    console.error(err);
    tbody.innerHTML = `<tr><td colspan='5'>載入失敗：${err.message}</td></tr>`;
  }
}

// ========== Broker Functions ==========

async function loadBrokerRanking() {
  const tbody = document.querySelector("#brokerRankTable tbody");
  const updateTime = document.getElementById("brokerUpdateTime");

  if (!tbody) return;
  tbody.innerHTML = "<tr><td colspan='6'>載入中...</td></tr>";

  try {
    const data = await fetchJson("data/broker_ranking.json");
    tbody.innerHTML = "";

    if (updateTime && data.updated) {
      updateTime.textContent = `更新：${new Date(data.updated).toLocaleString("zh-TW")}`;
    }

    if (!data.data || data.data.length === 0) {
      tbody.innerHTML = "<tr><td colspan='6'>尚無券商數據</td></tr>";
      return;
    }

    data.data.slice(0, 50).forEach((row, idx) => {
      const netVol = row.total_net_vol || 0;
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${idx + 1}</td>
        <td>${row.broker_name || ""}</td>
        <td class="${netVol > 0 ? 'net-positive' : 'net-negative'}">${formatNumber(netVol)}</td>
        <td>${row.buy_count || 0}</td>
        <td>${row.sell_count || 0}</td>
        <td>${row.stocks_traded || 0}</td>
      `;
      tbody.appendChild(tr);
    });
  } catch (err) {
    console.error(err);
    tbody.innerHTML = `<tr><td colspan='6'>載入失敗：${err.message}</td></tr>`;
  }
}

async function loadBrokerTrades() {
  const tbody = document.querySelector("#brokerTradesTable tbody");
  const status = document.getElementById("brokerTradesStatus");

  if (!tbody) return;
  tbody.innerHTML = "";
  status.textContent = "載入中...";

  try {
    const data = await fetchJson("data/broker_trades_latest.json");

    if (!data.data || data.data.length === 0) {
      status.textContent = "尚無交易數據";
      return;
    }

    status.textContent = `共 ${data.count || 0} 筆交易`;

    data.data.slice(0, 100).forEach((row) => {
      const netVol = row.net_vol || 0;
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${row.date || ""}</td>
        <td><span class="badge">${row.stock_code}</span></td>
        <td>${row.broker_name || ""}</td>
        <td>${formatNumber(row.buy_vol || 0)}</td>
        <td>${formatNumber(row.sell_vol || 0)}</td>
        <td class="${netVol > 0 ? 'net-positive' : 'net-negative'}">${formatNumber(netVol)}</td>
        <td>${formatPct(row.pct || 0)}%</td>
      `;
      tbody.appendChild(tr);
    });
  } catch (err) {
    console.error(err);
    status.textContent = `載入失敗：${err.message}`;
  }
}

async function loadTargetBrokers() {
  const container = document.getElementById("targetBrokersContent");
  if (!container) return;

  container.innerHTML = "<p>載入中...</p>";

  try {
    const data = await fetchJson("data/target_broker_trades.json");

    if (!data.brokers || Object.keys(data.brokers).length === 0) {
      container.innerHTML = "<p>尚無目標券商數據</p>";
      return;
    }

    container.innerHTML = "";

    Object.entries(data.brokers).forEach(([brokerName, trades]) => {
      const totalNet = trades.reduce((sum, t) => sum + (t.net_vol || 0), 0);
      const netClass = totalNet > 0 ? "net-positive" : "net-negative";

      const card = document.createElement("div");
      card.className = "broker-card";
      card.innerHTML = `
        <h4>
          ${brokerName}
          <span class="${netClass}">${formatNumber(totalNet)} 張</span>
        </h4>
        <div class="trades-list">
          ${trades.slice(0, 8).map(t => {
        const sideClass = t.net_vol > 0 ? "buy-text" : "sell-text";
        return `<span class="badge">${t.stock_code}</span><span class="${sideClass}">${formatNumber(t.net_vol)}</span> `;
      }).join("")}
          ${trades.length > 8 ? `<br><small style="color:#8b8b9e">+${trades.length - 8} 筆</small>` : ""}
        </div>
      `;
      container.appendChild(card);
    });
  } catch (err) {
    console.error(err);
    container.innerHTML = `<p>載入失敗：${err.message}</p>`;
  }
}

// ========== Broker Trend Chart ==========

let brokerTrendChart = null;
let brokerTrendsData = null;

async function loadBrokerTrends() {
  const select = document.getElementById("brokerSelect");
  if (!select) return;

  try {
    brokerTrendsData = await fetchJson("data/broker_trends.json");

    if (!brokerTrendsData.brokers || Object.keys(brokerTrendsData.brokers).length === 0) {
      return;
    }

    // Populate broker select
    select.innerHTML = '<option value="ALL">全部目標券商</option>';
    Object.keys(brokerTrendsData.brokers).forEach(broker => {
      const option = document.createElement("option");
      option.value = broker;
      option.textContent = broker;
      select.appendChild(option);
    });

    // Add event listener
    select.addEventListener("change", () => {
      renderBrokerTrendChart(select.value);
    });

    // Initial render
    renderBrokerTrendChart("ALL");
  } catch (err) {
    console.error("Failed to load broker trends:", err);
  }
}

function renderBrokerTrendChart(selectedBroker) {
  const ctx = document.getElementById("brokerTrendChart");
  if (!ctx || !brokerTrendsData) return;

  // Destroy existing chart
  if (brokerTrendChart) {
    brokerTrendChart.destroy();
  }

  const brokers = brokerTrendsData.brokers;
  const datasets = [];

  // Define colors for different brokers
  const colors = [
    "#ff6b6b", "#4ecdc4", "#ffe66d", "#a55eea", "#45aaf2",
    "#fed330", "#26de81", "#fd9644", "#eb3b5a", "#2bcbba"
  ];

  let colorIndex = 0;
  let allDates = new Set();

  // Collect all dates
  Object.values(brokers).forEach(data => {
    data.forEach(d => allDates.add(d.date));
  });
  const sortedDates = Array.from(allDates).sort();

  // Build datasets
  Object.entries(brokers).forEach(([brokerName, data]) => {
    if (selectedBroker !== "ALL" && brokerName !== selectedBroker) {
      return;
    }

    // Create date -> cumulative map
    const dateMap = {};
    data.forEach(d => {
      dateMap[d.date] = d.cumulative;
    });

    // Fill in missing dates with last known value
    const values = [];
    let lastValue = 0;
    sortedDates.forEach(date => {
      if (dateMap[date] !== undefined) {
        lastValue = dateMap[date];
      }
      values.push(lastValue);
    });

    datasets.push({
      label: brokerName,
      data: values,
      borderColor: colors[colorIndex % colors.length],
      backgroundColor: "transparent",
      borderWidth: 2,
      tension: 0.3,
      pointRadius: selectedBroker === "ALL" ? 0 : 3,
    });

    colorIndex++;
  });

  brokerTrendChart = new Chart(ctx, {
    type: "line",
    data: {
      labels: sortedDates,
      datasets: datasets,
    },
    options: {
      responsive: true,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: {
          ticks: { maxTicksLimit: 10, color: "#8b8b9e" },
          grid: { color: "rgba(255,255,255,0.05)" },
        },
        y: {
          title: { display: true, text: "累計買賣超 (張)", color: "#8b8b9e" },
          ticks: { color: "#8b8b9e" },
          grid: { color: "rgba(255,255,255,0.05)" },
        },
      },
      plugins: {
        legend: {
          position: "bottom",
          labels: { color: "#eaeaea", boxWidth: 12 },
        },
      },
    },
  });
}

// ========== Navigation ==========


function initNavigation() {
  const navBtns = document.querySelectorAll(".nav-btn");
  const sections = document.querySelectorAll(".section");

  navBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      const targetSection = btn.dataset.section;

      // Update nav buttons
      navBtns.forEach(b => b.classList.remove("active"));
      btn.classList.add("active");

      // Update sections
      sections.forEach(section => {
        section.classList.remove("active");
        if (section.id === targetSection) {
          section.classList.add("active");
        }
      });

      // Load data for broker section on first click
      if (targetSection === "broker") {
        loadBrokerRanking();
        loadBrokerTrades();
        loadTargetBrokers();
        loadBrokerTrends();
      }
    });
  });
}

// ========== Initialization ==========

document.addEventListener("DOMContentLoaded", () => {
  const input = document.getElementById("stockInput");
  const btn = document.getElementById("loadBtn");
  const marketSel = document.getElementById("marketFilter");
  const windowSel = document.getElementById("windowFilter");
  const logCb = document.getElementById("logScaleCheckbox");
  const showForeign = document.getElementById("showForeign");
  const showTrust = document.getElementById("showTrust");
  const showDealer = document.getElementById("showDealer");
  const showTotal = document.getElementById("showTotal");

  btn.addEventListener("click", () => loadStock(input.value));
  input.addEventListener("keyup", (e) => {
    if (e.key === "Enter") loadStock(input.value);
  });

  marketSel.addEventListener("change", () => {
    marketFilter = marketSel.value;
    loadRanking();
  });

  windowSel.addEventListener("change", () => {
    currentWindow = parseInt(windowSel.value, 10);
    loadRanking();
  });

  logCb.addEventListener("change", () => {
    useLogScale = logCb.checked;
    loadStock(input.value || "2330");
  });

  [showForeign, showTrust, showDealer, showTotal].forEach((cb) => {
    cb.addEventListener("change", () => loadStock(input.value || "2330"));
  });

  // Initialize navigation
  initNavigation();

  // Load initial data
  input.value = "2330";
  loadStock("2330");
  loadRanking();
});
