// State Management
let currentChatId = null;
let currentChatData = null;
let appSettings = null;
let availableModels = [];
let currentAgentMode = "solo"; // "solo" (single-agent with subchats) or "squad" (multi-agent team)
let activeEditingFile = null;
let isEditMode = false;

// DOM Elements
const chatListContainer = document.getElementById("chatListContainer");
const messagesContainer = document.getElementById("messagesContainer");
const promptInput = document.getElementById("promptInput");
const btnSendMessage = document.getElementById("btnSendMessage");
const btnNewChat = document.getElementById("btnNewChat");
const currentChatTitle = document.getElementById("currentChatTitle");
const subchatBadge = document.getElementById("subchatBadge");
const breadcrumbsContainer = document.getElementById("breadcrumbsContainer");
const btnSynthesizeSubchat = document.getElementById("btnSynthesizeSubchat");
const modelSelector = document.getElementById("modelSelector");

// Mode Switcher Elements
const btnModeSolo = document.getElementById("btnModeSolo") || document.getElementById("btnModeMuse");
const btnModeSquad = document.getElementById("btnModeSquad") || document.getElementById("btnModeGrok");

// Right Panel Elements
const rightPanel = document.getElementById("rightPanel");
const btnToggleRightPanel = document.getElementById("btnToggleRightPanel");
const tabButtons = document.querySelectorAll(".tab-btn");
const tabFiles = document.getElementById("tab-files");
const tabBrowser = document.getElementById("tab-browser");
const fileListContainer = document.getElementById("fileListContainer");
const fileDropzone = document.getElementById("fileDropzone");
const fileInputHidden = document.getElementById("fileInputHidden");
const btnRefreshFiles = document.getElementById("btnRefreshFiles");
const btnOpenNewFileModal = document.getElementById("btnOpenNewFileModal");

// New File Modal
const newFileModal = document.getElementById("newFileModal");
const btnCloseNewFileModal = document.getElementById("btnCloseNewFileModal");
const btnCancelNewFile = document.getElementById("btnCancelNewFile");
const btnSubmitNewFile = document.getElementById("btnSubmitNewFile");
const newFileNameInput = document.getElementById("newFileNameInput");
const newFileContentInput = document.getElementById("newFileContentInput");

// Settings Modal Elements
const settingsModal = document.getElementById("settingsModal");
const btnOpenSettings = document.getElementById("btnOpenSettings");
const btnCloseSettings = document.getElementById("btnCloseSettings");
const btnCancelSettings = document.getElementById("btnCancelSettings");
const btnSaveSettings = document.getElementById("btnSaveSettings");
const btnTestConnection = document.getElementById("btnTestConnection");
const btnForgetKey = document.getElementById("btnForgetKey");
const cfgApiUrl = document.getElementById("cfgApiUrl");
const cfgApiKey = document.getElementById("cfgApiKey");
const cfgGraphClientId = document.getElementById("cfgGraphClientId");
const cfgGraphTenant = document.getElementById("cfgGraphTenant");
const btnGraphConnect = document.getElementById("btnGraphConnect");
const btnGraphDisconnect = document.getElementById("btnGraphDisconnect");
const graphStatusLine = document.getElementById("graphStatusLine");
const graphCodeBox = document.getElementById("graphCodeBox");
const graphUserCode = document.getElementById("graphUserCode");
const graphVerifyLink = document.getElementById("graphVerifyLink");
const graphPollStatus = document.getElementById("graphPollStatus");
let graphPollTimer = null;
const cfgDefaultModel = document.getElementById("cfgDefaultModel");
const cfgSystemPrompt = document.getElementById("cfgSystemPrompt");
const connectionStatus = document.getElementById("connectionStatus");

// File Viewer / Editor Modal Elements
const fileViewerModal = document.getElementById("fileViewerModal");
const btnCloseViewer = document.getElementById("btnCloseViewer");
const viewerFileName = document.getElementById("viewerFileName");
const viewerFileContent = document.getElementById("viewerFileContent");
const editorFileContent = document.getElementById("editorFileContent");
const viewerImageContainer = document.getElementById("viewerImageContainer");
const viewerImage = document.getElementById("viewerImage");
const btnDownloadFile = document.getElementById("btnDownloadFile");
const btnToggleEditMode = document.getElementById("btnToggleEditMode");
const editModeLabel = document.getElementById("editModeLabel");
const btnSaveFileContent = document.getElementById("btnSaveFileContent");
const fileSaveStatus = document.getElementById("fileSaveStatus");

// Architecture Modal Elements
const roadmapModal = document.getElementById("roadmapModal");
const btnToggleRoadmap = document.getElementById("btnToggleRoadmap");
const btnCloseRoadmap = document.getElementById("btnCloseRoadmap");
const btnCloseRoadmapBtn = document.getElementById("btnCloseRoadmapBtn");

// Quick Actions
const btnQuickUpload = document.getElementById("btnQuickUpload");
const btnQuickSearch = document.getElementById("btnQuickSearch");
const btnQuickSubchat = document.getElementById("btnQuickSubchat");

// Browser Tools
const browserUrlInput = document.getElementById("browserUrlInput");
const btnManualBrowse = document.getElementById("btnManualBrowse");
const btnManualBack = document.getElementById("btnManualBack");
const btnManualReload = document.getElementById("btnManualReload");
const btnManualScreenshot = document.getElementById("btnManualScreenshot");
const browserActivityLog = document.getElementById("browserActivityLog");

// Live Browser (goal runner, live viewport, element map)
const browserGoalInput = document.getElementById("browserGoalInput");
const btnBrowserRunGoal = document.getElementById("btnBrowserRunGoal");
const btnBrowserStopGoal = document.getElementById("btnBrowserStopGoal");
const browserGoalStatus = document.getElementById("browserGoalStatus");
const browserFrameImage = document.getElementById("browserFrameImage");
const browserViewportPlaceholder = document.getElementById("browserViewportPlaceholder");
const browserLiveBadge = document.getElementById("browserLiveBadge");
const browserViewport = document.getElementById("browserViewport");
const btnBrowserTakeover = document.getElementById("btnBrowserTakeover");
const browserTakeoverLabel = document.getElementById("browserTakeoverLabel");
const browserControlHint = document.getElementById("browserControlHint");
const browserUserBanner = document.getElementById("browserUserBanner");
const browserPeteBanner = document.getElementById("browserPeteBanner");
const browserAskCard = document.getElementById("browserAskCard");
const browserAskQuestion = document.getElementById("browserAskQuestion");
const browserAskInput = document.getElementById("browserAskInput");
const btnBrowserAskSend = document.getElementById("btnBrowserAskSend");
const btnBrowserAskResume = document.getElementById("btnBrowserAskResume");
const appContainer = document.querySelector(".app-container");
const btnToggleFocusMode = document.getElementById("btnToggleFocusMode");
const browserElementList = document.getElementById("browserElementList");
const browserElementsCount = document.getElementById("browserElementsCount");

// Initialize Marked.js. Guarded: if the script failed to load for any reason,
// fall back to plain-text rendering instead of crashing the whole app at
// startup (a single failed <script> must never kill Pete).
if (typeof marked !== "undefined") {
  marked.setOptions({
    highlight: function(code, lang) {
      try {
        if (typeof hljs !== "undefined") {
          if (lang && hljs.getLanguage(lang)) {
            return hljs.highlight(code, { language: lang }).value;
          }
          return hljs.highlightAuto(code).value;
        }
      } catch (e) { /* highlighting is decorative; never break rendering */ }
      return code;
    },
    breaks: true
  });
} else {
  window.marked = {
    parse: function(src) {
      return String(src || "")
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/\n/g, "<br>");
    }
  };
  console.warn("marked.js unavailable; using plain-text fallback");
}

// App Startup
document.addEventListener("DOMContentLoaded", async () => {
  await loadSettings();
  await loadModels();
  await loadChatList();
  setupEventListeners();

  // First-run experience: no key saved yet, so open Settings immediately with
  // the key instructions front and center instead of failing later.
  if (appSettings && !appSettings.has_api_key) {
    settingsModal.style.display = "flex";
  }

  if (!currentChatId) {
    await createNewChat("Welcome to Pete AI");
  }
});

// --- API Helpers ---
async function apiGet(endpoint) {
  const res = await fetch(endpoint);
  if (!res.ok) throw new Error(await res.text());
  return await res.json();
}

async function apiPost(endpoint, body) {
  const res = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  if (!res.ok) throw new Error(await res.text());
  return await res.json();
}

async function apiPut(endpoint, body) {
  const res = await fetch(endpoint, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  if (!res.ok) throw new Error(await res.text());
  return await res.json();
}

async function apiDelete(endpoint) {
  const res = await fetch(endpoint, { method: "DELETE" });
  if (!res.ok) throw new Error(await res.text());
  return await res.json();
}

// --- Settings Management ---
async function loadSettings() {
  try {
    appSettings = await apiGet("/api/settings");
    cfgApiUrl.value = appSettings.purdue_api_url;
    // The key itself is never sent to the browser; show only whether one is saved.
    cfgApiKey.value = "";
    cfgApiKey.placeholder = appSettings.has_api_key
      ? "Key saved — leave blank to keep it, or paste a new one"
      : "Paste your GenAI Studio API key";
    cfgSystemPrompt.value = appSettings.system_prompt;
    cfgGraphClientId.value = appSettings.graph_client_id || "";
    cfgGraphTenant.value = appSettings.graph_tenant || "";
    await refreshGraphStatus();
  } catch (e) {
    console.error("Failed to load settings:", e);
  }
}

async function refreshGraphStatus() {
  try {
    const st = await apiGet("/api/graph/status");
    if (st.connected) {
      graphStatusLine.innerHTML = `<i class="fa-solid fa-circle-check" style="color: var(--success, #4caf50);"></i> Microsoft connected.`;
      btnGraphConnect.style.display = "none";
      btnGraphDisconnect.style.display = "";
    } else {
      graphStatusLine.innerHTML = st.has_client_id
        ? `<i class="fa-solid fa-circle" style="color: var(--text-muted);"></i> Not connected yet. Press Connect Microsoft, then enter the code.`
        : `<i class="fa-solid fa-circle" style="color: var(--text-muted);"></i> Not connected. Paste your Azure client ID above, save, then connect.`;
      btnGraphConnect.style.display = "";
      btnGraphDisconnect.style.display = "none";
    }
  } catch (e) {
    graphStatusLine.textContent = "Could not check Microsoft status.";
  }
}

function stopGraphPolling() {
  if (graphPollTimer) { clearTimeout(graphPollTimer); graphPollTimer = null; }
  if (graphCodeBox) graphCodeBox.style.display = "none";
}

async function pollGraphSession(sessionId, intervalMs) {
  try {
    const res = await apiPost("/api/graph/poll", { session_id: sessionId });
    if (res.status === "done") {
      graphPollStatus.textContent = "Connected!";
      stopGraphPolling();
      await refreshGraphStatus();
      return;
    }
  } catch (e) {
    graphPollStatus.textContent = "Sign-in expired or failed. Press Connect to try again.";
    stopGraphPolling();
    return;
  }
  graphPollTimer = setTimeout(() => pollGraphSession(sessionId, intervalMs), intervalMs);
}

function renderModelOptions() {
  modelSelector.innerHTML = "";
  cfgDefaultModel.innerHTML = "";
  availableModels.forEach(m => {
    const opt = document.createElement("option");
    opt.value = m.id;
    opt.textContent = m.name;
    modelSelector.appendChild(opt.cloneNode(true));
    cfgDefaultModel.appendChild(opt);
  });
  if (appSettings && appSettings.default_model) {
    modelSelector.value = appSettings.default_model;
    cfgDefaultModel.value = appSettings.default_model;
  }
}

async function loadModels() {
  try {
    const data = await apiGet("/api/models");
    if (data.error) {
      // No API key (or another model-service problem): say so in the pickers
      // instead of silently showing a stale list.
      availableModels = [];
      const msg = "No API key — open Settings";
      modelSelector.innerHTML = "";
      cfgDefaultModel.innerHTML = "";
      [modelSelector, cfgDefaultModel].forEach(sel => {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = msg;
        sel.appendChild(opt);
      });
      console.warn("Models unavailable:", data.error);
      return;
    }
    availableModels = data.models || [];
    renderModelOptions();
  } catch (e) {
    console.error("Failed to load models:", e);
  }
}

// --- Chat & Subchat Operations ---
async function loadChatList() {
  try {
    const data = await apiGet("/api/chats");
    renderChatTree(data.chats || []);
  } catch (e) {
    console.error("Failed to load chats:", e);
  }
}

function renderChatTree(chats) {
  chatListContainer.innerHTML = "";

  chats.forEach(chat => {
    const itemWrapper = document.createElement("div");
    itemWrapper.className = "chat-item-wrapper";

    const chatEl = document.createElement("div");
    chatEl.className = `chat-item ${currentChatId === chat.id ? "active" : ""}`;
    chatEl.innerHTML = `
      <i class="fa-regular fa-message"></i>
      <span class="chat-item-title" title="${escapeHtml(chat.title)}">${escapeHtml(chat.title)}</span>
      <div class="chat-item-actions">
        <button class="btn-icon btn-subchat-trigger" title="Branch Subchat" style="padding: 2px 4px; font-size: 11px;">
          <i class="fa-solid fa-code-branch"></i>
        </button>
        <button class="btn-icon btn-chat-delete" title="Delete" style="padding: 2px 4px; font-size: 11px;">
          <i class="fa-solid fa-trash"></i>
        </button>
      </div>
    `;

    chatEl.addEventListener("click", (e) => {
      if (e.target.closest(".chat-item-actions")) return;
      selectChat(chat.id);
    });

    chatEl.querySelector(".btn-chat-delete").addEventListener("click", async (e) => {
      e.stopPropagation();
      if (confirm(`Delete "${chat.title}" and any subchats?`)) {
        await fetch(`/api/chats/${chat.id}`, { method: "DELETE" });
        if (currentChatId === chat.id) currentChatId = null;
        await loadChatList();
        if (!currentChatId) await createNewChat();
      }
    });

    chatEl.querySelector(".btn-subchat-trigger").addEventListener("click", async (e) => {
      e.stopPropagation();
      const subTitle = prompt("Enter subchat topic or branch name:", "Sub-investigation");
      if (subTitle) {
        await createSubchat(chat.id, subTitle);
      }
    });

    itemWrapper.appendChild(chatEl);

    // Render nested subchats
    if (chat.subchats && chat.subchats.length > 0) {
      const subTree = document.createElement("div");
      subTree.className = "subchat-tree";

      chat.subchats.forEach(sub => {
        const subEl = document.createElement("div");
        subEl.className = `subchat-item ${currentChatId === sub.id ? "active" : ""}`;
        subEl.innerHTML = `
          <i class="fa-solid fa-code-branch" style="margin-right: 6px; font-size: 11px;"></i>
          <span style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1;" title="${escapeHtml(sub.title)}">${escapeHtml(sub.title)}</span>
          <button class="btn-icon btn-sub-del" title="Delete Subchat" style="padding: 1px 3px; font-size: 10px; border: none;">
            <i class="fa-solid fa-xmark"></i>
          </button>
        `;

        subEl.addEventListener("click", (e) => {
          if (e.target.closest(".btn-sub-del")) return;
          selectChat(sub.id);
        });

        subEl.querySelector(".btn-sub-del").addEventListener("click", async (e) => {
          e.stopPropagation();
          if (confirm(`Delete subchat "${sub.title}"?`)) {
            await fetch(`/api/chats/${sub.id}`, { method: "DELETE" });
            if (currentChatId === sub.id) selectChat(chat.id);
            await loadChatList();
          }
        });

        subTree.appendChild(subEl);
      });

      itemWrapper.appendChild(subTree);
    }

    chatListContainer.appendChild(itemWrapper);
  });
}

function renderBreadcrumbs(crumbs) {
  breadcrumbsContainer.innerHTML = "";
  if (!crumbs || crumbs.length === 0) {
    breadcrumbsContainer.innerHTML = `<span class="breadcrumb-item current">${escapeHtml(currentChatData ? currentChatData.title : "Chat")}</span>`;
    return;
  }

  crumbs.forEach((crumb, index) => {
    const isLast = index === crumbs.length - 1;
    if (isLast) {
      const span = document.createElement("span");
      span.className = "breadcrumb-item current";
      span.textContent = crumb.title;
      breadcrumbsContainer.appendChild(span);
    } else {
      const a = document.createElement("a");
      a.className = "breadcrumb-item";
      a.textContent = crumb.title;
      a.href = "#";
      a.onclick = (e) => {
        e.preventDefault();
        selectChat(crumb.id);
      };
      breadcrumbsContainer.appendChild(a);

      const sep = document.createElement("span");
      sep.innerHTML = `<i class="fa-solid fa-chevron-right" style="font-size: 9px; margin: 0 4px; color: var(--border-color);"></i>`;
      breadcrumbsContainer.appendChild(sep);
    }
  });
}

async function selectChat(chatId) {
  try {
    currentChatId = chatId;
    const chat = await apiGet(`/api/chats/${chatId}`);
    currentChatData = chat;

    currentChatTitle.textContent = chat.title;
    
    renderBreadcrumbs(chat.breadcrumbs || []);

    if (chat.parent_id) {
      subchatBadge.style.display = "inline-block";
      btnSynthesizeSubchat.style.display = "inline-flex";
    } else {
      subchatBadge.style.display = "none";
      btnSynthesizeSubchat.style.display = "none";
    }

    renderMessages(chat.messages || []);
    await loadWorkspaceFiles();
    await loadChatList();
  } catch (e) {
    console.error("Failed to select chat:", e);
  }
}

async function createNewChat(title = "New Chat") {
  const newChat = await apiPost("/api/chats", { title });
  await selectChat(newChat.id);
}

async function createSubchat(parentId, title = "Subchat Investigation") {
  const newSubchat = await apiPost("/api/chats", {
    title,
    parent_id: parentId
  });
  await selectChat(newSubchat.id);
}

// --- Messages & Streaming ---
function renderMessages(messages) {
  messagesContainer.innerHTML = "";

  if (messages.length === 0) {
    messagesContainer.innerHTML = `
      <div class="message-card">
        <div class="avatar avatar-pete">P</div>
        <div class="message-body">
          <div class="message-header">Pete AI</div>
          <div class="message-content">
            <p>I am ready. Ask a research question, drop documents in the workspace, or command me to search or browse the web.</p>
          </div>
        </div>
      </div>
    `;
    return;
  }

  // Tool outputs and internal tool turns are part of the replayable history but are
  // not chat bubbles, so they are filtered out of the transcript view.
  messages
    .filter(msg => (msg.role === "user" || msg.role === "assistant") && !(msg.metadata && msg.metadata.hidden))
    .forEach(msg => {
      appendMessageCard(msg.role, msg.content, msg.id, msg.tool_calls, msg.metadata);
    });

  scrollMessagesToBottom();
}

function appendMessageCard(role, content, msgId = null, toolCalls = null, metadata = {}) {
  const card = document.createElement("div");
  card.className = "message-card";

  const isUser = role === "user";
  let avatarClass = isUser ? "avatar-user" : "avatar-pete";
  let avatarLetter = isUser ? "U" : "P";
  const metaMode = (metadata && metadata.agent_mode) || currentAgentMode;
  let senderName = isUser ? "You" : (metaMode === "squad" ? "Pete Squad (Multi-Agent)" : "Pete AI");

  if (!isUser && metaMode === "squad") {
    avatarClass = "avatar-squad";
    avatarLetter = "S";
  }

  let badgesHtml = "";
  // Past tool calls stay out of the chat transcript: the finished answer is
  // the output, and the working status already said what Pete did. (The
  // subchat-synthesis banner below is context, not tool JSON, so it stays.)

  if (metadata && metadata.type === "subchat_synthesis") {
    badgesHtml += `
      <div style="background: rgba(206, 184, 136, 0.15); border: 1px solid var(--purdue-gold); padding: 6px 12px; border-radius: 6px; font-size: 12px; color: var(--purdue-gold); margin-bottom: 8px;">
        <i class="fa-solid fa-code-branch"></i> Synthesized from Subchat: <strong>${escapeHtml(metadata.subchat_title || "")}</strong>
      </div>
    `;
  }

  const renderedText = isUser ? escapeHtml(content).replace(/\n/g, "<br/>") : marked.parse(content || "");

  card.innerHTML = `
    <div class="avatar ${avatarClass}">${avatarLetter}</div>
    <div class="message-body">
      <div class="message-header">
        <span>${senderName}</span>
      </div>
      <div class="message-badges-area">${badgesHtml}</div>
      <div class="message-content">${renderedText}</div>
      ${!isUser ? `
        <div class="message-actions">
          <button class="btn-branch btn-branch-msg" title="Branch a Subchat from this response">
            <i class="fa-solid fa-code-branch"></i> Branch to Subchat
          </button>
        </div>
      ` : ""}
    </div>
  `;

  if (!isUser) {
    const branchBtn = card.querySelector(".btn-branch-msg");
    if (branchBtn) {
      branchBtn.addEventListener("click", async () => {
        const title = prompt("Branch Subchat Title:", "Deep Dive on Pete Response");
        if (title && currentChatId) {
          await createSubchat(currentChatId, title);
        }
      });
    }
  }

  messagesContainer.appendChild(card);
  scrollMessagesToBottom();
  return card;
}

async function handleSendMessage() {
  const text = promptInput.value.trim();
  if (!text || !currentChatId) return;

  promptInput.value = "";
  promptInput.style.height = "auto";

  appendMessageCard("user", text);

  const assistantCard = appendMessageCard("assistant", "");
  const contentEl = assistantCard.querySelector(".message-content");
  const badgesArea = assistantCard.querySelector(".message-badges-area");
  contentEl.innerHTML = `<span style="color: var(--text-muted);"><i class="fa-solid fa-spinner fa-spin"></i> Initializing...</span>`;

  try {
    const response = await fetch(`/api/chats/${currentChatId}/message`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        content: text,
        model: modelSelector.value,
        agent_mode: currentAgentMode
      })
    });

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let accumulatedContent = "";
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n\n");
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const payload = JSON.parse(line.substring(6));

        if (payload.type === "clear_content") {
          // Erase any raw JSON tool call from screen
          accumulatedContent = "";
          contentEl.innerHTML = `<span class="typing-dots"><span></span><span></span><span></span></span><span class="typing-status">Running tool...</span>`;
        } else if (payload.type === "status") {
          contentEl.innerHTML = `<span class="typing-dots"><span></span><span></span><span></span></span><span class="typing-status">${escapeHtml(payload.data)}</span>`;
        } else if (payload.type === "agent_state") {
          const stepDiv = document.createElement("div");
          stepDiv.className = "agent-step-badge";
          stepDiv.innerHTML = `
              <i class="fa-solid fa-circle-nodes"></i> <strong>${escapeHtml(payload.agent)}</strong>: ${escapeHtml(payload.action)}
          `;
          badgesArea.appendChild(stepDiv);
          logBrowserActivity(`[${payload.agent}] ${payload.action}`);
        } else if (payload.type === "squad_plan") {
          const plan = payload.plan || {};
          const planDiv = document.createElement("div");
          planDiv.className = "tool-call-badge squad-plan-badge";
          const research = (plan.research_tasks || []).map(t => `<li>${escapeHtml(t)}</li>`).join("");
          const workspace = (plan.workspace_tasks || []).map(t => `<li>${escapeHtml(t)}</li>`).join("");
          planDiv.innerHTML = `
            <div class="tool-call-header">
              <i class="fa-solid fa-list-check"></i> Squad Plan: ${escapeHtml(plan.objective || "objective")}
            </div>
            <div class="tool-output-details">
              Research: ${research ? `<ul style="margin-left:16px;">${research}</ul>` : "none"}<br/>
              Workspace: ${workspace ? `<ul style="margin-left:16px;">${workspace}</ul>` : "consolidate findings"}
            </div>
          `;
          badgesArea.appendChild(planDiv);
          logBrowserActivity(`[Orchestrator] plan: ${plan.objective || ""}`);
        } else if (payload.type === "agent_log") {
          logBrowserActivity(`[${payload.agent}] ${payload.text}`);
        } else if (payload.type === "agent_done") {
          const status = payload.status === "warn" ? "fa-triangle-exclamation" : "fa-circle-check";
          logBrowserActivity(`[${payload.agent}] done: ${payload.summary || payload.status}`);
          const doneDiv = document.createElement("div");
          doneDiv.className = "agent-step-badge agent-done-badge";
          doneDiv.innerHTML = `
            <i class="fa-solid ${status}"></i> <strong>${escapeHtml(payload.agent)}</strong> done: ${escapeHtml((payload.summary || "").slice(0, 160))}
          `;
          badgesArea.appendChild(doneDiv);
        } else if (payload.type === "content") {
          accumulatedContent += payload.delta;
          contentEl.innerHTML = marked.parse(accumulatedContent);
          scrollMessagesToBottom();
        } else if (payload.type === "tool_call") {
          logBrowserActivity(`Tool Called: ${payload.name} (${JSON.stringify(payload.arguments)})`);

          // No raw "Used Tool" badge in the chat: the working status line above
          // already says what Pete is doing, and the finished answer is the
          // output. Side effects below stay.
          if ((payload.name || "").includes("file") || payload.name.includes("screenshot")) {
            await loadWorkspaceFiles();
          }
          if (payload.name === "spawn_subchat") {
            await loadChatList();
          }
          // browser_task drove the shared page: bring the live view up to date
          // so the user lands on the page the agent just finished reading.
          if (payload.name === "browser_task") {
            connectLiveView();
            await refreshElements();
          }
        } else if (payload.type === "tool_result") {
          logBrowserActivity(`Tool Result from ${payload.name}: complete`);
          if (payload.name === "take_screenshot" && payload.result && payload.result.filename) {
            const shotPath = `/api/workspaces/${currentChatData.workspace_id}/files/${encodeURIComponent(payload.result.filename)}`;
            const shotDiv = document.createElement("div");
            shotDiv.className = "chat-screenshot-preview";
            shotDiv.innerHTML = `<img src="${shotPath}" alt="Captured Screenshot" title="Click to view full size" onclick="window.open('${shotPath}', '_blank')">`;
            badgesArea.appendChild(shotDiv);
          }

          if (payload.name.includes("file") || payload.name.includes("screenshot")) {
            await loadWorkspaceFiles();
          }
          if (payload.name === "spawn_subchat") {
            await loadChatList();
          }
          // browser_task drove the shared page: bring the live view up to date
          // so the user lands on the page the agent just finished reading.
          if (payload.name === "browser_task") {
            connectLiveView();
            await refreshElements();
          }
        } else if (payload.type === "verification") {
          const v = payload.verification || {};
          const verdictDiv = document.createElement("div");
          verdictDiv.className = "tool-call-badge squad-plan-badge";
          const issues = (v.issues || []).map(i => `<li>${escapeHtml(i)}</li>`).join("");
          verdictDiv.innerHTML = `
            <div class="tool-call-header">
              <i class="fa-solid fa-clipboard-check"></i> Verifier verdict: ${escapeHtml(v.verdict || "unverified")}
              (confidence ${Number(v.confidence || 0).toFixed(2)})
            </div>
            ${issues ? `<div class="tool-output-details"><ul style="margin-left:16px;">${issues}</ul></div>` : ""}
          `;
          badgesArea.appendChild(verdictDiv);
          logBrowserActivity(`[Critic] verdict: ${v.verdict} (${(v.issues || []).length} issue(s))`);
        } else if (payload.type === "error") {
          contentEl.innerHTML += `<div style="color: var(--accent-red); margin-top: 8px;"><strong>Error:</strong> ${escapeHtml(payload.error)}</div>`;
        } else if (payload.type === "done") {
          contentEl.innerHTML = marked.parse(payload.content || accumulatedContent);
          await loadWorkspaceFiles();
          await loadChatList();
        }
      }
    }
  } catch (e) {
    contentEl.innerHTML = `<span style="color: var(--accent-red);">Failed to communicate with agent: ${escapeHtml(e.message)}</span>`;
  }
}

function scrollMessagesToBottom() {
  messagesContainer.scrollTop = messagesContainer.scrollHeight;
}

// --- Workspace File Management & Editor ---
async function loadWorkspaceFiles() {
  if (!currentChatData || !currentChatData.workspace_id) return;
  try {
    const data = await apiGet(`/api/workspaces/${currentChatData.workspace_id}/files`);
    const files = data.files || [];

    if (files.length === 0) {
      fileListContainer.innerHTML = `<p style="font-size: 12px; color: var(--text-muted); text-align: center; margin-top: 20px;">No files in workspace yet.</p>`;
      return;
    }

    fileListContainer.innerHTML = "";
    files.forEach(f => {
      const item = document.createElement("div");
      item.className = "file-item";

      let icon = "fa-file";
      if (f.extension === ".py") icon = "fa-python";
      else if (f.extension === ".md") icon = "fa-markdown";
      else if ([".png", ".jpg", ".jpeg"].includes(f.extension)) icon = "fa-image";
      else if (f.extension === ".csv") icon = "fa-table";

      item.innerHTML = `
        <i class="fa-solid ${icon}" style="color: var(--purdue-gold);"></i>
        <span class="file-item-name" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</span>
        <span style="font-size: 11px; color: var(--text-muted); margin-right: 8px;">${formatBytes(f.size)}</span>
        <button class="btn-icon btn-del-file" title="Delete" style="padding: 2px 4px; font-size: 11px; border: none;">
          <i class="fa-solid fa-trash"></i>
        </button>
      `;

      item.querySelector(".file-item-name").addEventListener("click", () => openFileViewer(f));
      item.querySelector(".btn-del-file").addEventListener("click", async (e) => {
        e.stopPropagation();
        if (confirm(`Delete file "${f.name}"?`)) {
          await fetch(`/api/workspaces/${currentChatData.workspace_id}/files/${encodeURIComponent(f.name)}`, { method: "DELETE" });
          await loadWorkspaceFiles();
        }
      });

      fileListContainer.appendChild(item);
    });
  } catch (e) {
    console.error("Failed to load workspace files:", e);
  }
}

async function uploadFiles(files) {
  if (!currentChatData || !currentChatData.workspace_id) return;
  for (const file of files) {
    const formData = new FormData();
    formData.append("file", file);
    try {
      await fetch(`/api/workspaces/${currentChatData.workspace_id}/upload`, {
        method: "POST",
        body: formData
      });
    } catch (e) {
      console.error("File upload failed:", e);
    }
  }
  await loadWorkspaceFiles();
}

function openFileViewer(file) {
  activeEditingFile = file;
  isEditMode = false;
  viewerFileName.textContent = file.name;
  btnDownloadFile.href = `/api/workspaces/${currentChatData.workspace_id}/files/${encodeURIComponent(file.name)}`;
  
  btnToggleEditMode.style.display = "inline-flex";
  editModeLabel.textContent = "Edit";
  btnSaveFileContent.style.display = "none";
  fileSaveStatus.style.display = "none";

  if ([".png", ".jpg", ".jpeg", ".gif", ".webp"].includes(file.extension)) {
    viewerFileContent.style.display = "none";
    editorFileContent.style.display = "none";
    viewerImageContainer.style.display = "block";
    btnToggleEditMode.style.display = "none";
    viewerImage.src = `/api/workspaces/${currentChatData.workspace_id}/files/${encodeURIComponent(file.name)}`;
  } else {
    viewerImageContainer.style.display = "none";
    editorFileContent.style.display = "none";
    viewerFileContent.style.display = "block";
    viewerFileContent.textContent = "Loading file content...";
    
    fetch(`/api/workspaces/${currentChatData.workspace_id}/files/${encodeURIComponent(file.name)}`)
      .then(res => res.text())
      .then(txt => {
        viewerFileContent.textContent = txt;
        editorFileContent.value = txt;
        hljs.highlightElement(viewerFileContent);
      })
      .catch(err => { viewerFileContent.textContent = "Error loading file: " + err; });
  }

  fileViewerModal.style.display = "flex";
}

function toggleEditMode() {
  if (!activeEditingFile) return;
  isEditMode = !isEditMode;

  if (isEditMode) {
    viewerFileContent.style.display = "none";
    editorFileContent.style.display = "block";
    btnSaveFileContent.style.display = "inline-flex";
    editModeLabel.textContent = "View";
    editorFileContent.focus();
  } else {
    editorFileContent.style.display = "none";
    viewerFileContent.style.display = "block";
    btnSaveFileContent.style.display = "none";
    editModeLabel.textContent = "Edit";
    viewerFileContent.textContent = editorFileContent.value;
    hljs.highlightElement(viewerFileContent);
  }
}

async function saveCurrentFileContent() {
  if (!activeEditingFile || !currentChatData) return;
  const newContent = editorFileContent.value;

  try {
    await apiPut(`/api/workspaces/${currentChatData.workspace_id}/files/${encodeURIComponent(activeEditingFile.name)}`, {
      content: newContent
    });
    fileSaveStatus.style.display = "inline";
    setTimeout(() => { fileSaveStatus.style.display = "none"; }, 2500);
    await loadWorkspaceFiles();
  } catch (e) {
    alert("Save failed: " + e.message);
  }
}

// --- Browser Logs & Tools ---
function logBrowserActivity(text, screenshotUrl = null) {
  const p = document.createElement("div");
  p.style.padding = "6px 8px";
  p.style.marginBottom = "6px";
  p.style.background = "var(--bg-primary)";
  p.style.borderRadius = "4px";
  p.style.border = "1px solid var(--border-color)";
  
  p.innerHTML = `
    <div style="font-size: 11.5px; color: var(--text-main);"><i class="fa-solid fa-circle-info" style="color: var(--purdue-gold);"></i> ${escapeHtml(text)}</div>
    ${screenshotUrl ? `<img src="${screenshotUrl}" style="max-width: 100%; border-radius: 4px; margin-top: 6px; cursor: pointer;" onclick="window.open('${screenshotUrl}', '_blank')">` : ""}
  `;

  browserActivityLog.prepend(p);
}

// --- Event Listeners Setup ---
function setupEventListeners() {
  btnNewChat.addEventListener("click", () => createNewChat());

  // Agent Mode Switcher (Solo vs Squad)
  if (btnModeSolo) {
    btnModeSolo.addEventListener("click", () => {
      currentAgentMode = "solo";
      btnModeSolo.classList.add("active");
      if (btnModeSquad) btnModeSquad.classList.remove("active");
    });
  }

  if (btnModeSquad) {
    btnModeSquad.addEventListener("click", () => {
      currentAgentMode = "squad";
      btnModeSquad.classList.add("active");
      if (btnModeSolo) btnModeSolo.classList.remove("active");
    });
  }

  // Synthesize Subchat
  btnSynthesizeSubchat.addEventListener("click", async () => {
    if (!currentChatId || !currentChatData || !currentChatData.parent_id) return;
    btnSynthesizeSubchat.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Synthesizing...`;
    try {
      const res = await apiPost(`/api/chats/${currentChatId}/synthesize`, {});
      alert("Subchat investigation successfully synthesized into parent thread!");
      await selectChat(currentChatData.parent_id);
    } catch (e) {
      alert("Synthesis failed: " + e.message);
    } finally {
      btnSynthesizeSubchat.innerHTML = `<i class="fa-solid fa-wand-magic-sparkles"></i> Synthesize to Parent`;
    }
  });

  // Send Message
  btnSendMessage.addEventListener("click", handleSendMessage);
  promptInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSendMessage();
    }
  });

  promptInput.addEventListener("input", function() {
    this.style.height = "auto";
    this.style.height = (this.scrollHeight) + "px";
  });

  // Quick Action Buttons
  btnQuickUpload.addEventListener("click", () => fileInputHidden.click());
  btnQuickSearch.addEventListener("click", () => {
    promptInput.value = "Search the web for: " + promptInput.value;
    promptInput.focus();
  });
  btnQuickSubchat.addEventListener("click", async () => {
    if (!currentChatId) return;
    const title = prompt("Enter subchat topic:", "Sub-investigation");
    if (title) await createSubchat(currentChatId, title);
  });

  // Tab switching
  tabButtons.forEach(btn => {
    btn.addEventListener("click", () => {
      tabButtons.forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      const target = btn.getAttribute("data-tab");
      if (target === "tab-files") {
        tabFiles.style.display = "block";
        tabBrowser.style.display = "none";
      } else {
        tabFiles.style.display = "none";
        tabBrowser.style.display = "block";
        // Opening the tab is the moment the user wants to see the page, so the
        // live socket and the element map are only wired up from here.
        connectLiveView();
        refreshElements();
      }
    });
  });

  // Right Panel Toggle
  btnToggleRightPanel.addEventListener("click", () => {
    rightPanel.classList.toggle("hidden");
  });

  // File Upload Drag & Drop
  fileDropzone.addEventListener("click", () => fileInputHidden.click());
  fileInputHidden.addEventListener("change", (e) => {
    if (e.target.files.length > 0) uploadFiles(e.target.files);
  });
  fileDropzone.addEventListener("dragover", (e) => {
    e.preventDefault();
    fileDropzone.style.borderColor = "var(--purdue-gold)";
  });
  fileDropzone.addEventListener("dragleave", () => {
    fileDropzone.style.borderColor = "var(--border-color)";
  });
  fileDropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    fileDropzone.style.borderColor = "var(--border-color)";
    if (e.dataTransfer.files.length > 0) uploadFiles(e.dataTransfer.files);
  });
  btnRefreshFiles.addEventListener("click", () => loadWorkspaceFiles());

  // In-Browser File Editor Actions
  btnToggleEditMode.addEventListener("click", toggleEditMode);
  btnSaveFileContent.addEventListener("click", saveCurrentFileContent);
  btnCloseViewer.addEventListener("click", () => { fileViewerModal.style.display = "none"; });

  editorFileContent.addEventListener("keydown", function(e) {
    if (e.key === "Tab") {
      e.preventDefault();
      const start = this.selectionStart;
      const end = this.selectionEnd;
      this.value = this.value.substring(0, start) + "    " + this.value.substring(end);
      this.selectionStart = this.selectionEnd = start + 4;
    }
  });

  // New File Modal
  btnOpenNewFileModal.addEventListener("click", () => {
    newFileNameInput.value = "";
    newFileContentInput.value = "";
    newFileModal.style.display = "flex";
    newFileNameInput.focus();
  });
  btnCloseNewFileModal.addEventListener("click", () => { newFileModal.style.display = "none"; });
  btnCancelNewFile.addEventListener("click", () => { newFileModal.style.display = "none"; });

  btnSubmitNewFile.addEventListener("click", async () => {
    const fname = newFileNameInput.value.trim();
    if (!fname || !currentChatData) return;
    try {
      await apiPost(`/api/workspaces/${currentChatData.workspace_id}/files`, {
        filename: fname,
        content: newFileContentInput.value
      });
      newFileModal.style.display = "none";
      await loadWorkspaceFiles();
    } catch (e) {
      alert("Failed to create file: " + e.message);
    }
  });

  // Browser Direct Actions -- these drive the SAME page the agent uses, so the
  // user can take over mid-session without losing the agent's place.
  btnManualBrowse.addEventListener("click", async () => {
    const url = browserUrlInput.value.trim();
    if (!url) return;
    logBrowserActivity(`Navigating to ${url}...`);
    connectLiveView();
    await sendBrowserAction({ action: "goto", url }, `Navigated to ${url}`);
  });

  if (btnManualBack) {
    btnManualBack.addEventListener("click", async () => {
      await sendBrowserAction({ action: "back" }, "Went back");
    });
  }

  if (btnManualReload) {
    btnManualReload.addEventListener("click", async () => {
      const url = browserUrlInput.value.trim();
      if (url) await sendBrowserAction({ action: "goto", url }, `Reloaded ${url}`);
    });
  }

  if (btnBrowserRunGoal) {
    btnBrowserRunGoal.addEventListener("click", () => runBrowserGoal());
  }
  if (btnBrowserStopGoal) {
    btnBrowserStopGoal.addEventListener("click", () => stopBrowserGoal());
  }
  if (browserGoalInput) {
    browserGoalInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") runBrowserGoal();
    });
  }

  // -- Human takeover --
  if (btnBrowserTakeover) {
    btnBrowserTakeover.addEventListener("click", () => setBrowserControl(!browserUserControls));
  }
  if (btnBrowserAskSend) {
    btnBrowserAskSend.addEventListener("click", () => answerBrowserAsk());
  }
  if (btnBrowserAskResume) {
    btnBrowserAskResume.addEventListener("click", () => resumeBrowserAsk());
  }
  if (browserAskInput) {
    browserAskInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") answerBrowserAsk();
    });
  }

  // -- Focus mode: browser in the main area, chat as a side rail --
  // The label deliberately never changes. Per the ARIA APG button pattern, a
  // toggle that uses aria-pressed must keep a stable accessible name, and state
  // is carried by aria-pressed + .is-active instead of by renaming the button
  // to a vague "Exit".
  const applyFocusMode = (active) => {
    if (!appContainer) return;
    appContainer.classList.toggle("focus-mode", active);
    if (btnToggleFocusMode) {
      btnToggleFocusMode.classList.toggle("is-active", active);
      btnToggleFocusMode.setAttribute("aria-pressed", active ? "true" : "false");
      btnToggleFocusMode.title = active
        ? "Leave focus mode and return the chat to the main window. Press Escape to undo."
        : "Give the browser the full window and move the chat to a side rail. Press Escape to undo.";
    }
    // The page is bigger now, so the click mapping has to be re-measured.
    refreshBrowserPageSize();
  };
  if (btnToggleFocusMode) {
    btnToggleFocusMode.addEventListener("click", () => {
      applyFocusMode(!appContainer.classList.contains("focus-mode"));
    });
  }

  // Escape leaves focus mode from anywhere, so the mode is never a trap.
  // Skipped while typing: Escape means something else inside a text field, and
  // the prompt box is the one place focus mode does not want to interrupt.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || e.defaultPrevented) return;
    const tag = e.target && e.target.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || (e.target && e.target.isContentEditable)) return;
    if (appContainer && appContainer.classList.contains("focus-mode")) {
      e.preventDefault();
      applyFocusMode(false);
    }
  });

  btnManualScreenshot.addEventListener("click", async () => {
    if (!currentChatData) return;
    // Captures whatever is on the live page, not a fresh visit to the URL, so
    // the screenshot matches what the user is actually looking at.
    logBrowserActivity("Capturing the current page...");
    try {
      // The capture endpoint is keyed on the workspace id, not the chat id.
      const res = await apiPost(
        `/api/browser/capture?workspace_id=${encodeURIComponent(currentChatData.workspace_id)}`, {}
      );
      if (res && res.filename) {
        const shotUrl = `/api/workspaces/${currentChatData.workspace_id}/files/${encodeURIComponent(res.filename)}`;
        logBrowserActivity(`Screenshot saved: ${res.filename}`, shotUrl);
        await loadWorkspaceFiles();
      } else if (res && res.error) {
        logBrowserActivity(`Screenshot notice: ${res.error}`);
      } else {
        logBrowserActivity("Screenshot captured. Open the Workspace tab to see it.");
      }
    } catch (e) {
      logBrowserActivity(`Screenshot error: ${e.message}`);
    }
  });

  // Settings Modal
  btnOpenSettings.addEventListener("click", () => { settingsModal.style.display = "flex"; });
  btnCloseSettings.addEventListener("click", () => { stopGraphPolling(); settingsModal.style.display = "none"; });
  btnCancelSettings.addEventListener("click", () => { stopGraphPolling(); settingsModal.style.display = "none"; });

  btnTestConnection.addEventListener("click", async () => {
    connectionStatus.innerHTML = `<span style="color: var(--purdue-gold);"><i class="fa-solid fa-spinner fa-spin"></i> Testing Purdue RCAC connection...</span>`;
    try {
      // Tests the candidate key WITHOUT saving it; Save Settings persists it.
      const res = await apiPost("/api/settings/test-connection", {
        purdue_api_url: cfgApiUrl.value,
        purdue_api_key: cfgApiKey.value
      });
      if (res.ok) {
        availableModels = res.models || [];
        renderModelOptions();
        const n = availableModels.length;
        connectionStatus.innerHTML = `<span style="color: var(--accent-green);"><i class="fa-solid fa-check"></i> Connected! Fetched ${n} model${n === 1 ? "" : "s"}. Press Save Settings to keep this key.</span>`;
      } else {
        connectionStatus.innerHTML = `<span style="color: var(--accent-red);"><i class="fa-solid fa-xmark"></i> Connection failed: ${escapeHtml(res.error || "unknown error")}</span>`;
      }
    } catch (e) {
      connectionStatus.innerHTML = `<span style="color: var(--accent-red);"><i class="fa-solid fa-xmark"></i> Connection failed: ${escapeHtml(e.message)}</span>`;
    }
  });

  btnForgetKey.addEventListener("click", async () => {
    if (!confirm("Delete the saved API key from this machine?")) return;
    try {
      await apiDelete("/api/settings/api-key");
      cfgApiKey.value = "";
      await loadSettings();
      await loadModels();
      connectionStatus.innerHTML = `<span style="color: var(--accent-green);"><i class="fa-solid fa-check"></i> Saved key removed.</span>`;
    } catch (e) {
      connectionStatus.innerHTML = `<span style="color: var(--accent-red);"><i class="fa-solid fa-xmark"></i> Could not remove key: ${escapeHtml(e.message)}</span>`;
    }
  });

  btnSaveSettings.addEventListener("click", async () => {
    await apiPost("/api/settings", {
      purdue_api_url: cfgApiUrl.value,
      purdue_api_key: cfgApiKey.value,
      default_model: cfgDefaultModel.value,
      system_prompt: cfgSystemPrompt.value,
      graph_client_id: cfgGraphClientId.value.trim(),
      graph_tenant: cfgGraphTenant.value.trim() || "common"
    });
    settingsModal.style.display = "none";
    await loadSettings();
    await loadModels();
  });

  if (btnGraphConnect) btnGraphConnect.addEventListener("click", async () => {
    btnGraphConnect.disabled = true;
    try {
      const flow = await apiPost("/api/graph/connect", {});
      graphUserCode.textContent = flow.user_code;
      if (flow.verification_uri) graphVerifyLink.href = flow.verification_uri;
      graphCodeBox.style.display = "";
      graphPollStatus.textContent = "Waiting for you to approve...";
      stopGraphPolling();
      const intervalMs = Math.max(3, flow.interval || 5) * 1000;
      graphPollTimer = setTimeout(() => pollGraphSession(flow.session_id, intervalMs), intervalMs);
    } catch (e) {
      graphPollStatus.textContent = "";
      alert("Could not start Microsoft sign-in: " + (e.message || e));
      await refreshGraphStatus();
    } finally {
      btnGraphConnect.disabled = false;
    }
  });
  if (btnGraphDisconnect) btnGraphDisconnect.addEventListener("click", async () => {
    await apiPost("/api/graph/disconnect", {});
    await refreshGraphStatus();
  });

  // Architecture Modal
  if (btnToggleRoadmap) btnToggleRoadmap.addEventListener("click", () => { roadmapModal.style.display = "flex"; });
  if (btnCloseRoadmap) btnCloseRoadmap.addEventListener("click", () => { roadmapModal.style.display = "none"; });
  if (btnCloseRoadmapBtn) btnCloseRoadmapBtn.addEventListener("click", () => { roadmapModal.style.display = "none"; });

  // Close modals on backdrop click
  window.addEventListener("click", (e) => {
    if (e.target === settingsModal) { stopGraphPolling(); settingsModal.style.display = "none"; }
    if (e.target === fileViewerModal) fileViewerModal.style.display = "none";
    if (e.target === newFileModal) newFileModal.style.display = "none";
    if (e.target === roadmapModal) roadmapModal.style.display = "none";
  });
}

function toolIcon(name) {
  const n = (name || "").toLowerCase();
  if (n.includes("search")) return "fa-magnifying-glass";
  if (n.includes("browse")) return "fa-globe";
  if (n.includes("screenshot")) return "fa-camera";
  if (n.includes("file")) return "fa-file-lines";
  if (n.includes("subchat")) return "fa-code-branch";
  return "fa-gear";
}

function formatBytes(bytes, decimals = 1) {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const dm = decimals < 0 ? 0 : decimals;
  const sizes = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + " " + sizes[i];
}

function escapeHtml(text) {
  if (!text) return "";
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
