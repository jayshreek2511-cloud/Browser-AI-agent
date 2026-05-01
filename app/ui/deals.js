/* ===================================================================
   Deals & Price Tracker Agent — deals.js
   Search, compare, history, alerts, library integration
   =================================================================== */

const dealsForm = document.getElementById("deals-form");
const dealsInput = document.getElementById("deals-query");
const dealsThread = document.getElementById("deals-thread");

// ── Utilities ─────────────────────────────────────────────────────────

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function showToast(msg) {
  const el = document.createElement("div");
  el.className = "deals-toast";
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

function formatPrice(p) {
  if (p == null) return "";
  return "₹" + Number(p).toLocaleString("en-IN");
}

function badgeClass(status) {
  switch ((status || "").toUpperCase()) {
    case "GOOD": return "good";
    case "OVERPRICED": return "overpriced";
    case "BEST_DEAL": return "good";
    default: return "average";
  }
}

// ── User message ──────────────────────────────────────────────────────

function appendUserMsg(text) {
  dealsThread.insertAdjacentHTML("beforeend",
    `<article class="message user-message"><p>${esc(text)}</p></article>`);
  dealsThread.scrollTop = dealsThread.scrollHeight;
}

function appendAssistantHtml(html) {
  dealsThread.insertAdjacentHTML("beforeend",
    `<article class="message assistant-message">${html}</article>`);
  dealsThread.scrollTop = dealsThread.scrollHeight;
}

// ── Render a single product card ──────────────────────────────────────

function renderProductCard(r, highlight = false) {
  const badge = `<span class="deal-badge ${badgeClass(r.deal_status)}">${esc(r.deal_status)}</span>`;
  const ratingHtml = r.rating
    ? `<span><span class="rating-star">★</span> ${esc(r.rating)}</span>`
    : "";
  const highlightClass = highlight ? " deal-card--main" : "";
  return `
    <div class="deal-card${highlightClass}">
      <div class="deal-card-header">
        <span class="deal-card-name">${esc(r.name)}</span>
        <span class="deal-card-price">${formatPrice(r.price)}</span>
      </div>
      <div class="deal-card-meta">
        ${badge}
        ${ratingHtml}
        <span>${esc(r.source)}</span>
      </div>
      <div class="deal-card-actions">
        ${r.link ? `<a class="btn-view" href="${esc(r.link)}" target="_blank">
          <span class="material-symbols-outlined" style="font-size:16px;">open_in_new</span> View Product
        </a>` : ""}
        <button class="btn-track" onclick="showHistoryAndAlert('${esc(r.product_id)}', '${esc(r.name)}')">
          <span class="material-symbols-outlined" style="font-size:16px;">notifications</span> Track Price
        </button>
      </div>
    </div>`;
}

// ── Render: URL Flow (main + related + comparison + verdict) ──────────

function renderUrlFlow(payload, query) {
  const main = payload.main_product;
  if (!main) {
    appendAssistantHtml(`<div class="deals-empty">
      <span class="material-symbols-outlined">shopping_bag</span>
      <p>Unable to extract product details from this URL. The page may be blocked or use an unsupported layout.</p>
    </div>`);
    return;
  }

  // 1. Main Product (highlighted)
  appendAssistantHtml(`
    <div class="deals-section-label">
      <span class="material-symbols-outlined" style="font-size:18px;">shopping_cart</span>
      Product Found
    </div>
    <div class="deals-results">${renderProductCard(main, true)}</div>
  `);

  // 2. Related Products
  const related = payload.related || [];
  if (related.length > 0) {
    const relatedCards = related.map(r => renderProductCard(r)).join("");
    appendAssistantHtml(`
      <div class="deals-section-label">
        <span class="material-symbols-outlined" style="font-size:18px;">compare_arrows</span>
        Related Products from Other Stores
      </div>
      <div class="deals-results">${relatedCards}</div>
    `);
  }

  // 3. Price Comparison Table
  const comparison = payload.comparison || [];
  if (comparison.length > 0) {
    const rows = comparison.map(c => {
      const isBest = c.is_best;
      return `<div class="comparison-row ${isBest ? 'comparison-row--best' : ''}">
        <span class="comparison-source">
          <span class="material-symbols-outlined" style="font-size:16px;">storefront</span>
          ${esc(c.source)}
        </span>
        <span class="comparison-price ${isBest ? 'comparison-lowest' : ''}">
          ${formatPrice(c.price)} ${isBest ? "← BEST PRICE" : ""}
        </span>
      </div>`;
    }).join("");

    appendAssistantHtml(`
      <div class="comparison-section">
        <h3><span class="material-symbols-outlined" style="font-size:20px;vertical-align:middle;">analytics</span> Price Comparison</h3>
        ${rows}
      </div>
    `);
  }

  // 4. Final Verdict
  const verdict = payload.verdict;
  if (verdict) {
    const isBest = verdict.status === "BEST_DEAL";
    const icon = isBest ? "verified" : "trending_down";
    const cls = isBest ? "verdict--best" : "verdict--better";
    let verdictBody = `<p>${esc(verdict.message)}</p>`;
    if (!isBest && verdict.savings) {
      verdictBody += `<p class="verdict-savings">You could save <strong>${formatPrice(verdict.savings)}</strong></p>`;
    }
    if (!isBest && verdict.best_link) {
      verdictBody += `<a class="btn-view verdict-link" href="${esc(verdict.best_link)}" target="_blank">
        <span class="material-symbols-outlined" style="font-size:16px;">open_in_new</span> Go to Best Deal
      </a>`;
    }
    appendAssistantHtml(`
      <div class="verdict-section ${cls}">
        <div class="verdict-header">
          <span class="material-symbols-outlined">${icon}</span>
          <h3>Final Verdict</h3>
        </div>
        ${verdictBody}
      </div>
    `);
  }
}

// ── Render: Search Flow (filtered product cards) ──────────────────────

function renderSearchFlow(payload, query) {
  const results = payload.results || [];
  if (!results.length) {
    appendAssistantHtml(`<div class="deals-empty">
      <span class="material-symbols-outlined">shopping_bag</span>
      <p>No products found. Try a different search.</p>
    </div>`);
    return;
  }

  // Product Cards
  const cardsHtml = results.map(r => renderProductCard(r)).join("");
  appendAssistantHtml(`<div class="deals-results">${cardsHtml}</div>`);

  // Price Comparison across sources
  const sources = {};
  let lowestPrice = Infinity;
  results.forEach(r => {
    if (r.price != null) {
      const key = r.source || "Unknown";
      if (!sources[key] || r.price < sources[key].price) {
        sources[key] = { price: r.price, link: r.link };
      }
      if (r.price < lowestPrice) lowestPrice = r.price;
    }
  });

  const sourceKeys = Object.keys(sources);
  if (sourceKeys.length > 1) {
    const rows = sourceKeys.map(s => {
      const isLowest = sources[s].price === lowestPrice;
      return `<div class="comparison-row">
        <span class="comparison-source">
          <span class="material-symbols-outlined" style="font-size:16px;">storefront</span>
          ${esc(s)}
        </span>
        <span class="comparison-price ${isLowest ? "comparison-lowest" : ""}">
          ${formatPrice(sources[s].price)} ${isLowest ? "← Lowest" : ""}
        </span>
      </div>`;
    }).join("");

    appendAssistantHtml(`
      <div class="comparison-section">
        <h3>Price Comparison</h3>
        ${rows}
      </div>
    `);
  }
}

// ── Main render dispatcher ────────────────────────────────────────────

function renderDealsResponse(payload, query) {
  if (payload.mode === "url") {
    renderUrlFlow(payload, query);
  } else {
    renderSearchFlow(payload, query);
  }
}

// ── History & Alert Panel ─────────────────────────────────────────────

window.showHistoryAndAlert = async function(productId, productName) {
  try {
    const resp = await fetch(`/api/deals/history/${productId}`);
    const data = await resp.json();
    const history = data.history || [];

    let historyHtml = "";
    if (history.length) {
      const rows = history.slice(0, 15).map(h => `
        <div class="history-row">
          <span class="history-date">${h.checked_at ? new Date(h.checked_at).toLocaleDateString() : "—"}</span>
          <span class="history-price">${formatPrice(h.price)}</span>
          <span class="history-source">${esc(h.source)}</span>
        </div>`).join("");
      historyHtml = `<div class="history-section">
        <h3>Price History — ${esc(productName)}</h3>
        ${rows}
      </div>`;
    } else {
      historyHtml = `<div class="history-section">
        <h3>Price History</h3>
        <p style="color:var(--muted);font-size:.85rem;">No history recorded yet for this product.</p>
      </div>`;
    }

    const alertHtml = `<div class="alert-section">
      <h3>Set Price Alert</h3>
      <div class="alert-form">
        <input type="number" id="alert-price-${productId}" placeholder="Notify me below ₹..." min="1" />
        <button onclick="setAlert('${productId}')">
          <span class="material-symbols-outlined" style="font-size:16px;">add_alert</span> Set Alert
        </button>
      </div>
    </div>`;

    appendAssistantHtml(historyHtml + alertHtml);
  } catch (e) {
    showToast("Failed to load price history");
  }
};

window.setAlert = async function(productId) {
  const input = document.getElementById(`alert-price-${productId}`);
  const val = parseFloat(input?.value);
  if (!val || val <= 0) {
    showToast("Enter a valid target price");
    return;
  }
  try {
    const resp = await fetch("/api/deals/track", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ product_id: productId, target_price: val }),
    });
    if (resp.ok) {
      showToast(`Alert set! We'll notify you below ${formatPrice(val)}`);
    } else {
      showToast("Failed to set alert");
    }
  } catch (e) {
    showToast("Network error");
  }
};

// ── Form Submit ───────────────────────────────────────────────────────

dealsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = dealsInput.value.trim();
  if (!query) return;

  appendUserMsg(query);
  dealsInput.value = "";
  dealsInput.style.height = "auto";

  const isUrl = query.startsWith("http://") || query.startsWith("https://");

  appendAssistantHtml(`<div class="deals-loading">
    <div class="loading-spinner"></div>
    <span>${isUrl ? "Analyzing product and finding best deals…" : "Searching for the best deals…"}</span>
  </div>`);

  try {
    const resp = await fetch("/api/deals/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(isUrl ? { url: query } : { query }),
    });
    const payload = await resp.json();
    if (!resp.ok) {
      appendAssistantHtml(`<div class="deals-empty">
        <span class="material-symbols-outlined">error</span>
        <p>${esc(payload?.detail || "Search failed")}</p>
      </div>`);
      return;
    }
    renderDealsResponse(payload, query);
    saveDealsHistory(query, payload);
  } catch (e) {
    appendAssistantHtml(`<div class="deals-empty">
      <span class="material-symbols-outlined">wifi_off</span>
      <p>Network error. Please try again.</p>
    </div>`);
  }
});

// ── History persistence for Library ───────────────────────────────────

function saveDealsHistory(query, payload) {
  const history = JSON.parse(localStorage.getItem("deals_history") || "[]");
  history.unshift({
    id: "deal-" + Date.now(),
    type: "deals",
    query: query,
    timestamp: new Date().toISOString(),
    payload: payload,
  });
  localStorage.setItem("deals_history", JSON.stringify(history.slice(0, 50)));
}

// ── Load history from URL params ──────────────────────────────────────

function handleDealsHistoryLoad() {
  const params = new URLSearchParams(window.location.search);
  const hid = params.get("historyId");
  if (!hid) return;
  const history = JSON.parse(localStorage.getItem("deals_history") || "[]");
  const entry = history.find(h => h.id === hid);
  if (entry) {
    appendUserMsg(entry.query);
    renderDealsResponse(entry.payload || {}, entry.query);
  }
}
handleDealsHistoryLoad();

// ── New Chat ──────────────────────────────────────────────────────────

document.getElementById("new-deals-chat").addEventListener("click", () => {
  dealsThread.innerHTML = `
    <article class="message assistant-message">
      <p>Search for any product to compare prices, track history, and find the best deals across top stores.</p>
    </article>
  `;
  dealsInput.value = "";
  dealsInput.focus();
  window.history.replaceState({}, "", window.location.pathname);
});
