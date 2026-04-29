const form = document.getElementById("automation-form");
const queryInput = document.getElementById("automation-query");
const thread = document.getElementById("automation-thread");

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function appendUserMessage(text) {
  const markup = `<article class="message user-message"><p>${escapeHtml(text)}</p></article>`;
  thread.insertAdjacentHTML("beforeend", markup);
  thread.scrollTop = thread.scrollHeight;
}

function appendAssistantHtml(html) {
  const markup = `<article class="message assistant-message">${html}</article>`;
  thread.insertAdjacentHTML("beforeend", markup);
  thread.scrollTop = thread.scrollHeight;
}

function renderResult(payload) {
  const plan = payload?.plan;
  const output = payload?.output;
  const logs = payload?.execution?.logs || [];
  const results = output?.results || [];

  const planHtml = plan
    ? `<div class="automation-card" style="margin-bottom:12px;">
         <h3>Generated Action Plan</h3>
         <div class="code-like">${escapeHtml(JSON.stringify(plan, null, 2))}</div>
       </div>`
    : "";

  const logsHtml = logs.length
    ? `<div class="automation-card" style="margin-bottom:12px;">
         <h3>Execution Log</h3>
         <div class="code-like">${escapeHtml(
           logs
             .map((l) => `#${l.step} ${l.action} ${l.ok ? "OK" : "FAIL"} — ${l.message}`)
             .join("\n")
         )}</div>
       </div>`
    : "";

  const resultsHtml = results.length
    ? `<div class="automation-card">
         <h3>Top Results</h3>
         <div class="stack">
           ${results
             .map(
               (r) => `
             <div class="stack-item" style="border-bottom: 1px solid var(--outline-variant); padding-bottom: 12px; margin-bottom: 12px;">
               <p><strong>${escapeHtml(r.name || "Item")}</strong></p>
               <p style="font-size:0.85em;">
                 ${r.price != null ? `Price: ${escapeHtml(r.price)}` : "Price: n/a"}
                 ${r.rating != null ? ` | Rating: ${escapeHtml(r.rating)}` : ""}
               </p>
               ${r.link ? `<a class="source-link" href="${escapeHtml(r.link)}" target="_blank">${escapeHtml(r.link)}</a>` : ""}
             </div>`
             )
             .join("")}
         </div>
       </div>`
    : `<div class="automation-card"><h3>Top Results</h3><p>No structured items extracted (site may block automation or require login).</p></div>`;

  const summaryHtml = output?.summary
    ? `<div class="automation-card" style="margin-bottom:12px;">
         <h3>Summary</h3>
         <p>${escapeHtml(output.summary)}</p>
         <p style="font-size:0.9em;color:var(--on-surface-variant)">${escapeHtml(output.reasoning || "")}</p>
       </div>`
    : "";

  appendAssistantHtml(summaryHtml + planHtml + logsHtml + resultsHtml);
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = queryInput.value.trim();
  if (!query) return;

  appendUserMessage(query);
  queryInput.value = "";
  queryInput.style.height = "auto";

  appendAssistantHtml(`<div class="automation-card"><p>Running task automation…</p></div>`);

  try {
    const response = await fetch("/api/automation/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });
    const payload = await response.json();
    if (!response.ok) {
      appendAssistantHtml(
        `<div class="automation-card"><h3>Error</h3><p>${escapeHtml(payload?.detail || "Request failed")}</p></div>`
      );
      return;
    }
    renderResult(payload);
  } catch (e) {
    appendAssistantHtml(`<div class="automation-card"><h3>Error</h3><p>Network error.</p></div>`);
  }
});

