const form = document.getElementById("query-form");
const queryInput = document.getElementById("query");
const chatThread = document.getElementById("chat-thread");

let activeTaskId = null;
let pollHandle = null;
let previewFrames = [];
let previewFrameIndex = 0;
let previewTimer = null;
let activeWebSockets = new Map(); // taskId -> WebSocket
let liveTasks = new Set(); // taskId set
let manualTabSelections = new Map(); // taskId -> pageId or null for 'auto'
let seenPages = new Map(); // taskId -> Set of pageIds

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

  const taskHtml = `
    <article class="message assistant-message" id="task-msg-${activeTaskId}">
      <div class="status-container" style="margin-bottom: 12px; display: flex; align-items: center; gap: 10px;">
        <span class="badge" id="status-badge-${activeTaskId}">queued</span>
        <span id="current-step-${activeTaskId}" style="font-size: 0.9em; color: var(--on-surface-variant);">Task created</span>
        <span id="task-id-${activeTaskId}" style="font-size: 0.8em; color: var(--muted);">ID: ${activeTaskId}</span>
      </div>
      
      <div id="progress-log-${activeTaskId}" class="feed" style="margin-bottom: 12px; font-size: 0.85em;"></div>
      <div id="error-box-${activeTaskId}" class="error-box" hidden></div>
      
      <div id="browser-container-${activeTaskId}" style="display:none; margin-bottom: 16px;">
        <div id="live-tab-strip-${activeTaskId}" class="live-tab-strip" style="display:none;"></div>
        <div id="preview-grid-${activeTaskId}" class="preview-grid">
          <div id="preview-empty-${activeTaskId}" class="empty">No live browser frame yet.</div>
        </div>
        <div id="preview-strip-${activeTaskId}" class="preview-strip"></div>
      </div>

      <div id="sources-list-${activeTaskId}" class="stack" style="margin-bottom: 20px;"></div>
      <div id="answer-box-${activeTaskId}" class="answer-box" style="margin-bottom: 20px;"></div>
      <div id="video-card-${activeTaskId}" class="answer-box" style="margin-bottom: 20px;"></div>
      <div id="evidence-list-${activeTaskId}" class="stack" style="margin-bottom: 20px;"></div>
    </article>
  `;
  chatThread.insertAdjacentHTML("beforeend", taskHtml);

  // Clear input and reset height
  queryInput.value = "";
  queryInput.style.height = 'auto';

  clearPanels(activeTaskId);
  startPolling();
  startScreencast(activeTaskId);
});

function startScreencast(taskId) {
  console.log("Starting screencast for task:", taskId);
  
  // Close existing socket for this task if any
  if (activeWebSockets.has(taskId)) {
    const oldWs = activeWebSockets.get(taskId);
    oldWs.onclose = null;
    oldWs.close();
  }
  
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${protocol}//${window.location.host}/api/tasks/${taskId}/screencast`;
  console.log("Connecting to WebSocket:", wsUrl);
  const ws = new WebSocket(wsUrl);
  activeWebSockets.set(taskId, ws);

  ws.onopen = () => {
    console.log("WebSocket connected for task:", taskId);
  };

  ws.onmessage = (event) => {
    try {
      const { page_id, data } = JSON.parse(event.data);
      if (!liveTasks.has(taskId)) {
        console.log("First live frame received for task:", taskId);
        liveTasks.add(taskId);
      }
      
      const grid = document.getElementById(`preview-grid-${taskId}`);
      const tabStrip = document.getElementById(`live-tab-strip-${taskId}`);
      if (!grid) return;

      // Track seen pages and update tabs
      if (!seenPages.has(taskId)) seenPages.set(taskId, new Set());
      const pages = seenPages.get(taskId);
      
      if (!pages.has(page_id)) {
        pages.add(page_id);
        if (tabStrip) {
          tabStrip.style.display = "flex";
          // If this is the first page (not counting Auto), add Auto tab
          if (pages.size === 1) {
             const autoBtn = document.createElement("button");
             autoBtn.id = `tab-auto-${taskId}`;
             autoBtn.className = "live-tab active";
             autoBtn.textContent = "Auto Switch";
             autoBtn.onclick = () => {
               manualTabSelections.set(taskId, null);
               updateLiveTabs(taskId);
             };
             tabStrip.appendChild(autoBtn);
          }
          
          const tab = document.createElement("button");
          tab.id = `tab-${page_id}`;
          tab.className = "live-tab";
          tab.textContent = `Tab ${pages.size}`;
          tab.onclick = () => {
            manualTabSelections.set(taskId, page_id);
            updateLiveTabs(taskId);
          };
          tabStrip.appendChild(tab);
        }
      }

      // Switching logic: Use manual selection if set, otherwise follow latest
      const manualId = manualTabSelections.get(taskId);
      const activeId = manualId || page_id;

      let img = document.getElementById(`stream-${page_id}`);
      if (!img) {
        // Remove fallback if exists
        const fallback = grid.querySelector(".screenshot-fallback");
        if (fallback) fallback.remove();

        img = document.createElement("img");
        img.id = `stream-${page_id}`;
        img.className = "stream-frame";
        img.alt = "Live Stream";
        grid.appendChild(img);
        
        const empty = document.getElementById(`preview-empty-${taskId}`);
        if (empty) empty.style.display = "none";
      }
      
      img.src = data;
      img.classList.remove("screenshot-fallback");
      
      // Update visibility based on active selection
      grid.querySelectorAll(".stream-frame").forEach(el => {
        if (el.id === `stream-${activeId}`) {
          el.style.display = "block";
        } else {
          el.style.display = "none";
        }
      });
      
      // Ensure grid and container are visible
      grid.style.display = "grid";
      const container = document.getElementById(`browser-container-${taskId}`);
      if (container) container.style.display = "block";
    } catch (e) {
      console.error("Screencast error:", e);
    }
  };

  ws.onclose = () => {
    console.log("WebSocket closed for task:", taskId);
    activeWebSockets.delete(taskId);
    // We don't remove from liveTasks immediately to keep the last frame shown as "live" 
    // until the next polling cycle or task completion.
  };
}

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
  renderTask(task.id, task);
  if (task.status === "completed" || task.status === "failed") {
    clearInterval(pollHandle);
    liveTasks.delete(task.id);
    // Refresh one last time to show historical strip
    renderTask(task.id, task);
  }
}

function renderTask(taskId, task) {
  const statusBadge = document.getElementById(`status-badge-${taskId}`);
  const currentStep = document.getElementById(`current-step-${taskId}`);
  if (statusBadge) statusBadge.textContent = task.status;
  if (currentStep) currentStep.textContent = task.current_step;
  
  renderActions(taskId, task.actions || []);
  renderErrors(taskId, task.errors || []);
  renderPreview(taskId, task.actions || [], task.latest_screenshot);
  renderAnswer(taskId, task.answer);
  renderSources(taskId, task.sources || []);
  renderEvidence(taskId, task.evidence || []);
  renderVideo(taskId, task.answer?.videos || []);
}

function renderActions(taskId, actions) {
  const progressLog = document.getElementById(`progress-log-${taskId}`);
  if (!progressLog) return;
  progressLog.innerHTML = actions
    .slice()
    .reverse()
    .map(
      (action) => `
        <div class="feed-item" style="padding: 8px 12px; margin-bottom: 4px;">
          <p><strong>${escapeHtml(action.action_type)}</strong> - ${escapeHtml(action.description)}</p>
        </div>
      `
    )
    .join("");
}

function renderPreview(taskId, actions, latestScreenshotPath) {
  const screenshots = Array.from(
    new Set(
      actions
        .map((action) => action.screenshot_path)
        .filter(Boolean)
        .map(normalizePath)
    )
  );

  previewFrames = screenshots;
  renderPreviewStrip(taskId);

  const grid = document.getElementById(`preview-grid-${taskId}`);
  const container = document.getElementById(`browser-container-${taskId}`);
  const previewEmpty = document.getElementById(`preview-empty-${taskId}`);
  const previewStrip = document.getElementById(`preview-strip-${taskId}`);

  if (!screenshots.length && !latestScreenshotPath) {
    stopPreviewPlayback();
    if (grid) grid.style.display = "none";
    if (previewEmpty) previewEmpty.style.display = "block";
    if (container) container.style.display = "block";
    return;
  }

  const isLive = liveTasks.has(taskId);
  const hasLiveFrames = grid && grid.querySelector(".stream-frame");

  if (isLive && hasLiveFrames) {
    if (previewStrip) previewStrip.style.display = "none";
    if (grid) grid.style.display = "grid";
    if (container) container.style.display = "block";
    return;
  }

  if (previewStrip) previewStrip.style.display = "flex";

  if (grid) {
    grid.style.display = "grid";
    const latest = screenshots.at(-1) || normalizePath(latestScreenshotPath);
    if (latest) {
      let img = grid.querySelector(".stream-frame");
      if (!img) {
        img = document.createElement("img");
        img.className = "stream-frame screenshot-fallback";
        grid.appendChild(img);
      }
      if (!isLive || img.classList.contains("screenshot-fallback")) {
        img.src = `${latest}?t=${Date.now()}`;
        img.classList.add("screenshot-fallback");
        img.style.display = "block";
      }
      if (previewEmpty) previewEmpty.style.display = "none";
    }
  }
  if (container) container.style.display = "block";
  startPreviewPlayback(taskId);
}

function renderErrors(taskId, errors) {
  const errorBox = document.getElementById(`error-box-${taskId}`);
  if (!errorBox) return;
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

function renderAnswer(taskId, answer) {
  const answerBox = document.getElementById(`answer-box-${taskId}`);
  if (!answerBox) return;
  if (!answer) {
    answerBox.innerHTML = "";
    return;
  }
  const points = (answer.supporting_points || []).slice(0, 6);
  const citations = (answer.citations || []).slice(0, 8);
  answerBox.innerHTML = `
    <div class="direct-answer-html" style="font-size: 1.05em; margin-bottom: 16px;">${answer.direct_answer}</div>
    ${points.length
      ? `<ul style="margin-bottom: 16px;">${points.map((point) => `<li>${escapeHtml(point)}</li>`).join("")}</ul>`
      : ""
    }
    <p style="margin-bottom: 12px;"><strong>Confidence:</strong> ${Math.round((answer.confidence.overall || 0) * 100)}%</p>
    ${citations.length
      ? `<p style="font-size: 0.9em; line-height: 1.4;">${citations
        .map((citation) => `<a class="source-link" href="${citation}" target="_blank">${escapeHtml(citation)}</a>`)
        .join("<br/>")}</p>`
      : ""
    }
  `;
}

function renderSources(taskId, sources) {
  const sourcesList = document.getElementById(`sources-list-${taskId}`);
  if (!sourcesList) return;
  if (!sources.length) {
    sourcesList.innerHTML = "";
    return;
  }
  sourcesList.innerHTML = `
    <h3 style="margin-bottom: 8px; font-size: 1.1em; color: var(--primary);">Ranked Sources</h3>
    ` + sources
      .map(
        (source) => `
            <div class="stack-item" style="border-bottom: 1px solid var(--outline-variant); padding-bottom: 12px; margin-bottom: 12px; background: transparent; border-radius: 0;">
              <p><strong>${escapeHtml(source.title)}</strong></p>
              <p style="font-size: 0.85em;">${escapeHtml(source.domain)} | score ${Number(source.rank_score).toFixed(2)}</p>
              <p style="font-size: 0.9em; margin-top: 6px;">${escapeHtml(source.snippet || "")}</p>
              <a class="source-link" href="${source.url}" target="_blank">${escapeHtml(source.url)}</a>
            </div>
          `
      )
      .join("");
}

function renderEvidence(taskId, evidence) {
  const evidenceList = document.getElementById(`evidence-list-${taskId}`);
  if (!evidenceList) return;
  if (!evidence.length) {
    evidenceList.innerHTML = "";
    return;
  }
  evidenceList.innerHTML = `
    <h3 style="margin-bottom: 8px; font-size: 1.1em; color: var(--primary);">Evidence</h3>
    ` + evidence
      .slice(0, 10)
      .map(
        (item) => `
            <div class="stack-item" style="border-bottom: 1px solid var(--outline-variant); padding-bottom: 12px; margin-bottom: 12px; background: transparent; border-radius: 0;">
              <p style="font-size: 0.85em;"><strong>${escapeHtml(item.evidence_type)}</strong> | confidence ${Math.round((item.confidence || 0) * 100)}%</p>
              <p style="font-size: 0.9em; margin-top: 6px;">${escapeHtml(item.excerpt || item.content.slice(0, 300))}</p>
            </div>
          `
      )
      .join("");
}

function renderVideo(taskId, videos) {
  const videoCard = document.getElementById(`video-card-${taskId}`);
  if (!videoCard) return;
  if (!videos || !videos.length) {
    videoCard.innerHTML = "";
    return;
  }
  videoCard.innerHTML = `
    <h3 style="margin-bottom: 8px; font-size: 1.1em; color: var(--primary);">YouTube Recommendations</h3>
    ` + videos.map(video => {
    let videoId = "";
    try {
      const urlObj = new URL(video.url);
      if (urlObj.hostname.includes("youtube.com")) {
        videoId = urlObj.searchParams.get("v");
      } else if (urlObj.hostname.includes("youtu.be")) {
        videoId = urlObj.pathname.slice(1);
      }
    } catch (e) { }

    const imgTag = videoId ? `<img src="https://img.youtube.com/vi/${videoId}/hqdefault.jpg" alt="Video thumbnail" style="width: 120px; height: 90px; object-fit: cover; border-radius: 8px; margin-right: 12px; flex-shrink: 0;">` : '';

    return `
      <div class="stack-item" style="border-bottom: 1px solid var(--outline-variant); padding-bottom: 12px; margin-bottom: 12px; background: transparent; border-radius: 0; display: flex; align-items: flex-start;">
        ${imgTag}
        <div>
          <p><strong><a class="source-link" href="${video.url}" target="_blank" style="text-decoration: none;">${escapeHtml(video.title)}</a></strong></p>
          <p style="font-size: 0.85em;">${escapeHtml((video.reasons || []).join(" | "))}</p>
          ${video.transcript_excerpt ? `<p style="font-size: 0.9em; margin-top: 6px;">${escapeHtml(video.transcript_excerpt.slice(0, 320))}</p>` : ""}
        </div>
      </div>
      `;
  }).join("");
}

function clearPanels(taskId) {
  const answerBox = document.getElementById(`answer-box-${taskId}`);
  const sourcesList = document.getElementById(`sources-list-${taskId}`);
  const evidenceList = document.getElementById(`evidence-list-${taskId}`);
  const videoCard = document.getElementById(`video-card-${taskId}`);
  const errorBox = document.getElementById(`error-box-${taskId}`);
  const grid = document.getElementById(`preview-grid-${taskId}`);
  const previewStrip = document.getElementById(`preview-strip-${taskId}`);
  const tabStrip = document.getElementById(`live-tab-strip-${taskId}`);

  if (answerBox) answerBox.innerHTML = "";
  if (sourcesList) sourcesList.innerHTML = "";
  if (evidenceList) evidenceList.innerHTML = "";
  if (videoCard) videoCard.innerHTML = "";
  if (errorBox) {
    errorBox.hidden = true;
    errorBox.innerHTML = "";
  }

  if (grid) {
    grid.innerHTML = `<div id="preview-empty-${taskId}" class="empty">No live browser frame yet.</div>`;
  }

  if (previewStrip) previewStrip.innerHTML = "";
  if (tabStrip) {
    tabStrip.innerHTML = "";
    tabStrip.style.display = "none";
  }

  previewFrames = [];
  previewFrameIndex = 0;
  manualTabSelections.delete(taskId);
  seenPages.delete(taskId);
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

function renderPreviewStrip(taskId) {
  const previewStrip = document.getElementById(`preview-strip-${taskId}`);
  if (!previewStrip) return;
  previewStrip.innerHTML = previewFrames
    .map((path, index) => {
      const activeClass = index === previewFrameIndex ? "active" : "";
      return `<img class="preview-thumb ${activeClass}" src="${path}" data-index="${index}" alt="Browser frame ${index + 1}" />`;
    })
    .join("");

  previewStrip.querySelectorAll(".preview-thumb").forEach((thumb) => {
    thumb.addEventListener("click", () => {
      previewFrameIndex = Number(thumb.dataset.index);
      setPreviewFrame(taskId, previewFrameIndex, true);
    });
  });
}

function startPreviewPlayback(taskId) {
  if (previewFrames.length <= 1) return;
  if (previewTimer) return;
  previewTimer = setInterval(() => {
    previewFrameIndex = (previewFrameIndex + 1) % previewFrames.length;
    setPreviewFrame(taskId, previewFrameIndex, false);
  }, 1600);
}

function stopPreviewPlayback() {
  if (!previewTimer) return;
  clearInterval(previewTimer);
  previewTimer = null;
}

function setPreviewFrame(taskId, index, resetTimer) {
  const frame = previewFrames[index];
  if (!frame) return;

  const grid = document.getElementById(`preview-grid-${taskId}`);
  const previewStrip = document.getElementById(`preview-strip-${taskId}`);

  if (grid) {
    let img = grid.querySelector(".stream-frame");
    if (!img) {
      img = document.createElement("img");
      img.className = "stream-frame";
      grid.appendChild(img);
    }
    img.src = `${frame}?t=${Date.now()}`;
  }

  if (previewStrip) {
    previewStrip.querySelectorAll(".preview-thumb").forEach((thumb, thumbIndex) => {
      thumb.classList.toggle("active", thumbIndex === index);
    });
  }
  if (resetTimer) {
    stopPreviewPlayback();
    startPreviewPlayback(taskId);
  }
}

function normalizePath(rawPath) {
  if (!rawPath) return "";
  return rawPath.startsWith("/") ? rawPath : `/${rawPath.replaceAll("\\", "/")}`;
}

function updateLiveTabs(taskId) {
  const tabStrip = document.getElementById(`live-tab-strip-${taskId}`);
  if (!tabStrip) return;
  const manualId = manualTabSelections.get(taskId);
  
  tabStrip.querySelectorAll(".live-tab").forEach(tab => {
    if (tab.id === `tab-auto-${taskId}`) {
      tab.classList.toggle("active", !manualId);
    } else {
      tab.classList.toggle("active", tab.id === `tab-${manualId}`);
    }
  });

  // Force re-render of frame visibility
  const grid = document.getElementById(`preview-grid-${taskId}`);
  if (grid) {
    // Note: visibility is also handled in onmessage, 
    // but here we force it for the frames already in DOM
    const frames = grid.querySelectorAll(".stream-frame");
    if (manualId) {
        frames.forEach(f => f.style.display = (f.id === `stream-${manualId}`) ? "block" : "none");
    }
  }
}
