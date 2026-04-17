const form = document.getElementById("query-form");
const queryInput = document.getElementById("query");
const chatThread = document.getElementById("chat-thread");
const statusBadge = document.getElementById("status-badge");
const currentStep = document.getElementById("current-step");
const taskIdEl = document.getElementById("task-id");
const progressLog = document.getElementById("progress-log");
const errorBox = document.getElementById("error-box");
const preview = document.getElementById("browser-preview");
const previewEmpty = document.getElementById("preview-empty");
const previewStrip = document.getElementById("preview-strip");
const answerBox = document.getElementById("answer-box");
const sourcesList = document.getElementById("sources-list");
const evidenceList = document.getElementById("evidence-list");
const videoCard = document.getElementById("video-card");

let activeTaskId = null;
let pollHandle = null;
let previewFrames = [];
let previewFrameIndex = 0;
let previewTimer = null;

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = queryInput.value.trim();
  if (!query) return;

  appendUserMessage(query);

  const response = await fetch("/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
  });
  const payload = await response.json();
  activeTaskId = payload.task_id;
  taskIdEl.textContent = activeTaskId;
  statusBadge.textContent = "queued";
  currentStep.textContent = "Task created";
  progressLog.innerHTML = "";
  clearPanels();
  appendAssistantMessage("Research task accepted. Planning and browsing started.");
  startPolling();
});

function startPolling() {
  if (pollHandle) clearInterval(pollHandle);
  fetchTask();
  pollHandle = setInterval(fetchTask, 2500);
}

async function fetchTask() {
  if (!activeTaskId) return;
  const response = await fetch(`/api/tasks/${activeTaskId}`);
  if (!response.ok) return;
  const task = await response.json();
  renderTask(task);
  if (task.status === "completed" || task.status === "failed") {
    clearInterval(pollHandle);
  }
}

function renderTask(task) {
  statusBadge.textContent = task.status;
  currentStep.textContent = task.current_step;
  renderActions(task.actions || []);
  renderErrors(task.errors || []);
  renderPreview(task.actions || [], task.latest_screenshot);
  renderAnswer(task.answer);
  renderSources(task.sources || []);
  renderEvidence(task.evidence || []);
  renderVideo(task.answer?.best_video || null);

  if (task.status === "completed" && task.answer) {
    appendAssistantMessage("Research complete. Final answer and evidence are available below.");
  } else if (task.status === "failed") {
    appendAssistantMessage("Task failed. Check Error Details for context.");
  }
}

function renderActions(actions) {
  progressLog.innerHTML = actions
    .slice()
    .reverse()
    .map(
      (action) => `
        <div class="feed-item">
          <p><strong>${escapeHtml(action.action_type)}</strong> - ${escapeHtml(action.description)}</p>
          ${action.url ? `<p><a class="source-link" href="${action.url}" target="_blank">${escapeHtml(action.url)}</a></p>` : ""}
        </div>
      `
    )
    .join("");
}

function renderPreview(actions, latestScreenshotPath) {
  const screenshots = Array.from(
    new Set(
      actions
        .map((action) => action.screenshot_path)
        .filter(Boolean)
        .map(normalizePath)
    )
  );

  previewFrames = screenshots;
  renderPreviewStrip();

  if (!screenshots.length && !latestScreenshotPath) {
    stopPreviewPlayback();
    preview.style.display = "none";
    previewEmpty.style.display = "block";
    return;
  }

  const latest = screenshots.at(-1) || normalizePath(latestScreenshotPath);
  preview.src = `${latest}?t=${Date.now()}`;
  preview.style.display = "block";
  previewEmpty.style.display = "none";
  startPreviewPlayback();
}

function renderErrors(errors) {
  if (!errors.length) {
    errorBox.hidden = true;
    errorBox.innerHTML = "";
    return;
  }
  const latest = errors[errors.length - 1];
  errorBox.hidden = false;
  errorBox.innerHTML = `
    <p><strong>Error Details</strong></p>
    <p>${escapeHtml(latest.message)}</p>
  `;
}

function renderAnswer(answer) {
  if (!answer) {
    answerBox.innerHTML = "<p>The agent's answer will appear here.</p>";
    return;
  }
  const points = (answer.supporting_points || []).slice(0, 6);
  const citations = (answer.citations || []).slice(0, 8);
  answerBox.innerHTML = `
    <p><strong>${escapeHtml(answer.direct_answer)}</strong></p>
    ${
      points.length
        ? `<ul>${points.map((point) => `<li>${escapeHtml(point)}</li>`).join("")}</ul>`
        : "<p>No supporting points extracted.</p>"
    }
    <p><strong>Confidence:</strong> ${Math.round((answer.confidence.overall || 0) * 100)}%</p>
    ${
      citations.length
        ? `<p>${citations
            .map((citation) => `<a class="source-link" href="${citation}" target="_blank">${escapeHtml(citation)}</a>`)
            .join("<br/>")}</p>`
        : ""
    }
  `;
}

function renderSources(sources) {
  sourcesList.innerHTML = sources.length
    ? sources
        .map(
          (source) => `
            <div class="stack-item">
              <p><strong>${escapeHtml(source.title)}</strong></p>
              <p>${escapeHtml(source.domain)} | score ${Number(source.rank_score).toFixed(2)}</p>
              <p>${escapeHtml(source.snippet || "")}</p>
              <a class="source-link" href="${source.url}" target="_blank">${escapeHtml(source.url)}</a>
            </div>
          `
        )
        .join("")
    : "<p>No sources collected yet.</p>";
}

function renderEvidence(evidence) {
  evidenceList.innerHTML = evidence.length
    ? evidence
        .slice(0, 10)
        .map(
          (item) => `
            <div class="stack-item">
              <p><strong>${escapeHtml(item.evidence_type)}</strong> | confidence ${Math.round((item.confidence || 0) * 100)}%</p>
              <p>${escapeHtml(item.excerpt || item.content.slice(0, 300))}</p>
            </div>
          `
        )
        .join("")
    : "<p>No evidence extracted yet.</p>";
}

function renderVideo(video) {
  if (!video) {
    videoCard.innerHTML = "<p>No video recommendation yet.</p>";
    return;
  }
  videoCard.innerHTML = `
    <p><strong>${escapeHtml(video.title)}</strong></p>
    <p>${escapeHtml((video.reasons || []).join(" | "))}</p>
    ${video.transcript_excerpt ? `<p>${escapeHtml(video.transcript_excerpt.slice(0, 320))}</p>` : ""}
    <a class="source-link" href="${video.url}" target="_blank">${escapeHtml(video.url)}</a>
  `;
}

function clearPanels() {
  answerBox.innerHTML = "<p>Research in progress.</p>";
  sourcesList.innerHTML = "<p>Waiting for ranked sources.</p>";
  evidenceList.innerHTML = "<p>Waiting for evidence.</p>";
  videoCard.innerHTML = "<p>No video recommendation yet.</p>";
  errorBox.hidden = true;
  errorBox.innerHTML = "";
  preview.style.display = "none";
  previewEmpty.style.display = "block";
  previewStrip.innerHTML = "";
  previewFrames = [];
  previewFrameIndex = 0;
  stopPreviewPlayback();
}

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
  chatThread.insertAdjacentHTML("beforeend", markup);
  chatThread.scrollTop = chatThread.scrollHeight;
}

function appendAssistantMessage(text) {
  const lastMessage = chatThread.lastElementChild;
  const isDuplicate = lastMessage?.textContent?.trim() === text;
  if (isDuplicate) return;
  const markup = `<article class="message assistant-message"><p>${escapeHtml(text)}</p></article>`;
  chatThread.insertAdjacentHTML("beforeend", markup);
  chatThread.scrollTop = chatThread.scrollHeight;
}

function renderPreviewStrip() {
  previewStrip.innerHTML = previewFrames
    .map((path, index) => {
      const activeClass = index === previewFrameIndex ? "active" : "";
      return `<img class="preview-thumb ${activeClass}" src="${path}" data-index="${index}" alt="Browser frame ${index + 1}" />`;
    })
    .join("");

  previewStrip.querySelectorAll(".preview-thumb").forEach((thumb) => {
    thumb.addEventListener("click", () => {
      previewFrameIndex = Number(thumb.dataset.index);
      setPreviewFrame(previewFrameIndex, true);
    });
  });
}

function startPreviewPlayback() {
  if (previewFrames.length <= 1) return;
  if (previewTimer) return;
  previewTimer = setInterval(() => {
    previewFrameIndex = (previewFrameIndex + 1) % previewFrames.length;
    setPreviewFrame(previewFrameIndex, false);
  }, 1600);
}

function stopPreviewPlayback() {
  if (!previewTimer) return;
  clearInterval(previewTimer);
  previewTimer = null;
}

function setPreviewFrame(index, resetTimer) {
  const frame = previewFrames[index];
  if (!frame) return;
  preview.src = `${frame}?t=${Date.now()}`;
  previewStrip.querySelectorAll(".preview-thumb").forEach((thumb, thumbIndex) => {
    thumb.classList.toggle("active", thumbIndex === index);
  });
  if (resetTimer) {
    stopPreviewPlayback();
    startPreviewPlayback();
  }
}

function normalizePath(rawPath) {
  if (!rawPath) return "";
  return rawPath.startsWith("/") ? rawPath : `/${rawPath.replaceAll("\\", "/")}`;
}
