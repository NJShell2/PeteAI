/**
 * Live browser panel: goal runner, live viewport, and clickable element map.
 *
 * The agent and the user share ONE browser page on the server. This file keeps
 * that page visible, and lets the user drive it directly:
 *
 *  - `connectLiveView`  opens a WebSocket that pushes JPEG frames of the page
 *                       and accepts mouse/keyboard events back.
 *  - `runBrowserGoal`   asks the agent to drive the page toward a goal (SSE).
 *  - `refreshElements`  re-reads the ref map so the user can click real targets.
 *  - `setBrowserControl` hands the wheel between Pete and the user.
 *
 * Clicks are mapped from screen space into page pixels here, because the
 * viewport letterboxes a 16:10 frame and a raw clientX/clientY would otherwise
 * land in the wrong place (or off the page entirely).
 *
 * Clicking an element in the ref list issues the same `click <ref>` action the
 * agent uses, so a human and the agent drive the browser through one interface.
 */

let liveSocket = null;
let liveReconnectTimer = null;
let browserGoalAbort = null;
// True while the user holds the wheel; gates all input forwarding.
let browserUserControls = false;
// The page's true pixel size, used to map clicks into page space.
let browserPageSize = { width: 1280, height: 800 };
// Guards against a modal ask box swallowing keys meant for the page.
let browserAskOpen = false;

// Chat changes re-target the live view, since frames are keyed by chat.
function browserChatId() {
  return currentChatData ? currentChatData.id : "default";
}

function setLiveBadge(visible) {
  if (browserLiveBadge) browserLiveBadge.style.display = visible ? "flex" : "none";
}

function setGoalStatus(text, state) {
  if (!browserGoalStatus) return;
  browserGoalStatus.textContent = text || "";
  browserGoalStatus.className = "browser-goal-status" + (state ? " is-" + state : "");
}

function showFrame(dataUrl) {
  if (!browserFrameImage) return;
  browserFrameImage.src = dataUrl;
  browserFrameImage.style.display = "block";
  if (browserViewportPlaceholder) browserViewportPlaceholder.style.display = "none";
}

// ── Screen-to-page coordinate mapping ─────────────────────────────────────────
// The frame is drawn with `object-fit: contain`, so it is letterboxed inside the
// viewport box whenever the box aspect ratio differs from the page's. Mapping
// naively would offset every click by the size of the black bars, so the drawn
// area is recomputed here from the frame's real dimensions.

/** Returns the drawn size and offset of the frame inside the viewport. */
function browserFrameGeometry() {
  if (!browserViewport || !browserFrameImage) return null;
  const box = browserViewport.getBoundingClientRect();
  if (!box.width || !box.height) return null;

  const naturalWidth = browserFrameImage.naturalWidth || browserPageSize.width;
  const naturalHeight = browserFrameImage.naturalHeight || browserPageSize.height;
  if (!naturalWidth || !naturalHeight) return null;

  // `contain` picks the smaller of the two ratios, then centres horizontally
  // and pins to the top (see object-position in the stylesheet).
  const scale = Math.min(box.width / naturalWidth, box.height / naturalHeight);
  const drawnWidth = naturalWidth * scale;
  const drawnHeight = naturalHeight * scale;
  return {
    offsetX: box.left + (box.width - drawnWidth) / 2,
    offsetY: box.top, // object-position: top center
    scale: scale,
    drawnWidth: drawnWidth,
    drawnHeight: drawnHeight,
  };
}

/** Converts a pointer event into page pixels, or null if it missed the frame. */
function browserEventToPagePoint(event) {
  const geometry = browserFrameGeometry();
  if (!geometry) return null;
  const x = (event.clientX - geometry.offsetX) / geometry.scale;
  const y = (event.clientY - geometry.offsetY) / geometry.scale;
  // Clicks on the letterbox are not clicks on the page.
  if (x < 0 || y < 0 || x > browserPageSize.width || y > browserPageSize.height) {
    return null;
  }
  return { x: Math.round(x), y: Math.round(y) };
}

/** True when the event landed on the drawn frame rather than the bars. */
function browserEventHitFrame(event) {
  return browserEventToPagePoint(event) !== null;
}

/**
 * Briefly flashes a marker where a click landed outside the drawn page.
 *
 * The viewport letterboxes the live frame with `object-fit: contain`, so the
 * grey bars around it are part of the app, not the page. A click there is
 * silently discarded, which reads as a broken button. Showing the click makes
 * it clear the app received it and chose to ignore it.
 */
function flashBrowserMiss(event) {
  if (!browserViewport) return;
  const box = browserViewport.getBoundingClientRect();
  const marker = document.createElement("div");
  marker.className = "browser-miss-marker";
  marker.style.left = `${event.clientX - box.left}px`;
  marker.style.top = `${event.clientY - box.top}px`;
  browserViewport.appendChild(marker);
  // Remove on the next frame's timeout so the CSS fade-out can run.
  setTimeout(() => marker.remove(), 600);
}

/** Sends one input event over the live socket, if it is open and we are driving. */
function sendBrowserInput(payload) {
  if (!browserUserControls) return false;
  if (!liveSocket || liveSocket.readyState !== WebSocket.OPEN) return false;
  try {
    liveSocket.send(JSON.stringify(payload));
    return true;
  } catch (e) {
    return false;
  }
}

const BROWSER_MODIFIERS = { altKey: 1, ctrlKey: 2, metaKey: 4, shiftKey: 8 };

/** Packs the pressed modifier keys into the bitmask the server expects. */
function browserModifierMask(event) {
  let mask = 0;
  for (const key of Object.keys(BROWSER_MODIFIERS)) {
    if (event[key]) mask |= BROWSER_MODIFIERS[key];
  }
  return mask;
}

/** Normalizes a mouse button index to the name the server expects. */
function browserButtonName(event) {
  if (event.button === 1) return "middle";
  if (event.button === 2) return "right";
  return "left";
}

function browserPointerPayload(action, event) {
  const point = browserEventToPagePoint(event);
  if (!point) return null;
  return {
    kind: "pointer",
    action: action,
    x: point.x,
    y: point.y,
    button: browserButtonName(event),
    clickCount: event.detail || 1,
    modifiers: browserModifierMask(event),
  };
}

function isModifierKey(key) {
  return ["Alt", "Control", "Meta", "Shift"].includes(key);
}

/**
 * Wires mouse, wheel and keyboard events on the viewport.
 *
 * Only active while the user holds control. `mouseup` is bound to the window as
 * well as the box, because a drag that ends outside the frame would otherwise
 * never reach the page and it would stay stuck mid-press.
 */
function setupBrowserInput() {
  if (!browserViewport) return;
  if (browserViewport.dataset.inputBound === "1") return;
  browserViewport.dataset.inputBound = "1";

  // Escape must release the browser from anywhere, not just while the
  // viewport holds focus. The viewport handler further down stops propagation
  // when it fires, so this document-level fallback only covers the cases it
  // cannot: the user clicked into the prompt box, tabbed to a sidebar button,
  // or focus fell back to the body. Without it those states were a trap -- the
  // UI said "Esc to give Pete back" but nothing happened, which is the "I
  // can't unfocus the browser" report. Skipped while the ask card is open,
  // since Escape there belongs to the question the agent asked.
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (!browserUserControls || browserAskOpen) return;
    if (event.target === browserViewport) return;
    event.preventDefault();
    setBrowserControl(false);
  });

  browserViewport.addEventListener("mousedown", (event) => {
    if (!browserUserControls) return;
    // Show the click even when it missed the page, so a letterbox click reads
    // as "outside the page" rather than as a dead control.
    if (!browserEventHitFrame(event)) {
      flashBrowserMiss(event);
      return;
    }
    event.preventDefault();
    browserViewport.focus();
    const payload = browserPointerPayload("down", event);
    if (payload) sendBrowserInput(payload);
  });

  const releasePointer = (event) => {
    if (!browserUserControls || !(event.buttons & (1 | 2 | 4))) return;
    const payload = browserPointerPayload("up", event);
    if (payload) sendBrowserInput(payload);
  };
  browserViewport.addEventListener("mouseup", releasePointer);
  window.addEventListener("mouseup", releasePointer);

  browserViewport.addEventListener("mousemove", (event) => {
    if (!browserUserControls) return;
    // Only forward movement while a button is held, i.e. during a drag.
    if (!(event.buttons & (1 | 2 | 4))) return;
    const payload = browserPointerPayload("move", event);
    if (payload) sendBrowserInput(payload);
  });

  browserViewport.addEventListener("dblclick", (event) => {
    if (!browserUserControls || !browserEventHitFrame(event)) return;
    const payload = browserPointerPayload("dblclick", event);
    if (payload) sendBrowserInput(payload);
  });

  browserViewport.addEventListener("contextmenu", (event) => {
    // Suppress the native menu only when the page should get the right-click.
    if (browserUserControls) event.preventDefault();
  });

  browserViewport.addEventListener("wheel", (event) => {
    if (!browserUserControls) return;
    if (!browserEventHitFrame(event)) {
      flashBrowserMiss(event);
      return;
    }
    event.preventDefault();
    sendBrowserInput({ kind: "wheel", deltaX: event.deltaX, deltaY: event.deltaY });
  }, { passive: false });

  browserViewport.addEventListener("keydown", (event) => {
    if (!browserUserControls || browserAskOpen) return;
    if (event.target !== browserViewport) return;

    // Escape is the way back out of driving, so it is handled by this app and
    // never forwarded to the page. Previously every key was swallowed here,
    // which is why there was no keyboard escape from the takeover state.
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      setBrowserControl(false);
      return;
    }

    const modifier = event.ctrlKey || event.metaKey;
    const key = event.key.toLowerCase();

    // Copy belongs to the page, so ask the server for the selection.
    if (modifier && key === "c") {
      event.preventDefault();
      requestBrowserSelection();
      return;
    }
    // Paste comes from the user's clipboard and lands as a single insert.
    if (modifier && key === "v") {
      event.preventDefault();
      pasteIntoBrowserPage();
      return;
    }

    event.preventDefault();
    sendBrowserInput({
      kind: "key",
      action: "down",
      key: event.key,
      code: event.code,
      modifiers: browserModifierMask(event),
    });
  });

  // Only modifiers need a matching keyup; ordinary keys fire down+press.
  browserViewport.addEventListener("keyup", (event) => {
    if (!browserUserControls || browserAskOpen) return;
    if (event.target !== browserViewport) return;
    if (!isModifierKey(event.key)) return;
    event.preventDefault();
    sendBrowserInput({
      kind: "key",
      action: "up",
      key: event.key,
      code: event.code,
      modifiers: browserModifierMask(event),
    });
  });
}

/** Asks the server for the page's selection, to copy to the local clipboard. */
function requestBrowserSelection() {
  if (!liveSocket || liveSocket.readyState !== WebSocket.OPEN) return;
  try {
    liveSocket.send(JSON.stringify({ kind: "selection" }));
  } catch (e) {
    /* the socket is on its way out; onclose will reconnect */
  }
}

/** Writes the page's selection to the user's own clipboard. */
async function copyBrowserSelection(text) {
  if (!text) {
    logBrowserActivity("Nothing selected on the page to copy.");
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    logBrowserActivity(`Copied ${text.length} characters from the page.`);
  } catch (e) {
    // Clipboard writes can be refused outside a user gesture; say so plainly
    // rather than silently doing nothing.
    logBrowserActivity("Could not reach the clipboard; the browser blocked it.");
  }
}

/** Reads the user's clipboard and types it into the page as a single insert. */
async function pasteIntoBrowserPage() {
  try {
    const text = await navigator.clipboard.readText();
    if (!text) {
      logBrowserActivity("Your clipboard is empty.");
      return;
    }
    sendBrowserInput({ kind: "text", text: text });
  } catch (e) {
    logBrowserActivity("Could not read your clipboard; allow clipboard access to paste.");
  }
}

/** Refreshes the page dimensions used for click mapping. */
async function refreshBrowserPageSize() {
  try {
    const size = await apiGet("/api/browser/size");
    if (size && size.width && size.height) {
      browserPageSize = { width: size.width, height: size.height };
    }
  } catch (e) {
    /* keep the last known size; the server clamps clicks anyway */
  }
}

// ── Control hand-off ──────────────────────────────────────────────────────────

/** Reflects who is driving in the button, the banner, and the cursor. */
function applyBrowserControlState(state) {
  browserUserControls = !!(state && state.human_control);
  if (btnBrowserTakeover) {
    btnBrowserTakeover.classList.toggle("is-active", browserUserControls);
    // Stable label + aria-pressed, per the ARIA APG toggle pattern.
    btnBrowserTakeover.setAttribute("aria-pressed", browserUserControls ? "true" : "false");
  }
  if (browserTakeoverLabel) {
    browserTakeoverLabel.textContent = browserUserControls ? "Give Pete control" : "Take control";
  }
  if (browserControlHint) {
    browserControlHint.textContent = browserUserControls
      ? "You are driving. Esc hands back, and Pete picks up where it left off."
      : "Pete is driving. Take over to click, type or solve a CAPTCHA.";
  }
  if (browserUserBanner) {
    browserUserBanner.style.display = browserUserControls ? "flex" : "none";
  }
  // The page says so too, so the state is visible where the clicking happens.
  if (browserPeteBanner) {
    browserPeteBanner.style.display = browserUserControls ? "none" : "flex";
  }
  if (browserViewport) {
    browserViewport.classList.toggle("is-interactive", browserUserControls);
    browserViewport.classList.toggle("is-pete", !browserUserControls);
  }
  // Focus the viewport so keys reach the page without an extra click.
  if (browserUserControls && browserViewport) browserViewport.focus();
}

/** Takes the wheel for the user, or hands it back to Pete. */
async function setBrowserControl(active) {
  try {
    const state = await apiPost(
      `/api/browser/takeover?chat_id=${encodeURIComponent(browserChatId())}&active=${active}`, {});
    applyBrowserControlState(state);
    logBrowserActivity(active ? "You took control of the browser." : "Pete has control again.");
  } catch (e) {
    logBrowserActivity(`Could not switch control: ${e.message}`);
  }
}

/** Shows the agent's question and its reply box. */
function showBrowserAsk(question) {
  if (!browserAskCard) return;
  browserAskOpen = true;
  browserAskCard.style.display = "block";
  if (browserAskQuestion) {
    browserAskQuestion.textContent = question || "I need your help to continue.";
  }
  if (browserAskInput) {
    browserAskInput.value = "";
    browserAskInput.focus();
  }
}

function hideBrowserAsk() {
  if (!browserAskCard) return;
  browserAskOpen = false;
  browserAskCard.style.display = "none";
}

/** Sends the user's reply, then hands control back so Pete can carry on. */
async function answerBrowserAsk() {
  const answer = browserAskInput ? browserAskInput.value.trim() : "";
  hideBrowserAsk();
  try {
    await apiPost(`/api/browser/answer?chat_id=${encodeURIComponent(browserChatId())}`,
                  { answer: answer });
    logBrowserActivity("Reply sent to Pete.");
  } catch (e) {
    logBrowserActivity(`Could not send the reply: ${e.message}`);
  }
  // Answering implies Pete may continue, so release the wheel too.
  if (browserUserControls) await setBrowserControl(false);
}

/** Resume with no text: the user did the thing and wants Pete to look. */
async function resumeBrowserAsk() {
  hideBrowserAsk();
  if (browserUserControls) await setBrowserControl(false);
}

/** Handles the socket messages that are not frames. */
function handleBrowserControlMessage(message) {
  if (message.type === "control") {
    applyBrowserControlState(message);
    if (!message.human_control) hideBrowserAsk();
  } else if (message.type === "selection") {
    copyBrowserSelection(message.text || "");
  } else if (message.type === "input-rejected") {
    logBrowserActivity(message.reason || "Take control before interacting with the page.");
  } else if (message.type === "input-error") {
    logBrowserActivity(message.error || "The page rejected that input.");
  }
}

/** Opens (or reuses) the frame WebSocket for the active chat. */
function connectLiveView() {
  if (liveSocket && liveSocket.readyState === WebSocket.OPEN) return;

  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${window.location.host}/api/browser/live/${encodeURIComponent(browserChatId())}`;
  try {
    liveSocket = new WebSocket(url);
  } catch (e) {
    return;
  }

  liveSocket.onopen = () => {
    setLiveBadge(true);
    setupBrowserInput();
    refreshBrowserPageSize();
  };

  liveSocket.onmessage = (event) => {
    let message;
    try { message = JSON.parse(event.data); } catch (e) { return; }
    if (message.type === "frame") {
      showFrame(`data:image/jpeg;base64,${message.data}`);
      return;
    }
    if (message.type === "status" && message.url) {
      if (browserUrlInput) browserUrlInput.value = message.url;
      return;
    }
    handleBrowserControlMessage(message);
  };

  liveSocket.onclose = () => {
    setLiveBadge(false);
    // Reconnect so the panel recovers from a server restart or machine sleep.
    clearTimeout(liveReconnectTimer);
    liveReconnectTimer = setTimeout(connectLiveView, 3000);
  };

  liveSocket.onerror = () => { /* onclose handles recovery */ };
}

function disconnectLiveView() {
  clearTimeout(liveReconnectTimer);
  if (liveSocket) {
    liveSocket.onclose = null;
    try { liveSocket.close(); } catch (e) { /* already closing */ }
    liveSocket = null;
  }
  setLiveBadge(false);
}

/** Re-reads the ref map so the user can see and click the page's targets. */
async function refreshElements() {
  if (!browserElementList) return;
  try {
    const res = await apiGet(`/api/browser/snapshot?chat_id=${encodeURIComponent(browserChatId())}`);
    const elements = res.elements || [];
    if (browserElementsCount) browserElementsCount.textContent = elements.length;

    if (!elements.length) {
      browserElementList.innerHTML =
        '<p style="font-size:12px;color:var(--text-muted);">No interactive elements found.</p>';
      return;
    }

    browserElementList.innerHTML = "";
    elements.forEach((el) => {
      const label = el.text || el.label || el.placeholder || el.name || el.href || el.tag;
      const item = document.createElement("button");
      item.className = "browser-element-item";
      item.title = `${el.tag}${el.type ? "[" + el.type + "]" : ""} - click to act on this element`;
      item.innerHTML =
        `<span class="browser-element-ref">${el.ref}</span>` +
        `<span class="browser-element-label">${escapeHtml(String(label).slice(0, 90))}</span>` +
        `<span class="browser-element-tag">${escapeHtml(el.tag)}</span>`;
      item.addEventListener("click", () => handleElementClick(el));
      browserElementList.appendChild(item);
    });
  } catch (e) {
    if (browserElementList) {
      browserElementList.innerHTML =
        `<p style="font-size:12px;color:var(--text-muted);">${escapeHtml(e.message)}</p>`;
    }
  }
}

/**
 * Acts on a ref the user clicked. Inputs prompt for text; anything else is
 * clicked directly. This mirrors exactly what the agent does.
 */
async function handleElementClick(el) {
  const nonTextTypes = ["checkbox", "radio", "submit", "button", "file"];
  const isTextInput = el.tag === "input" && !nonTextTypes.includes(el.type);
  if (isTextInput || el.tag === "textarea") {
    const value = window.prompt(
      `Enter text for "${el.placeholder || el.label || el.tag}":`, el.value || "");
    if (value === null) return;
    const submit = window.confirm("Press Enter to submit after typing?");
    await sendBrowserAction({ action: "type", ref: el.ref, text: value, submit },
      `typed into [${el.ref}]`);
    return;
  }
  await sendBrowserAction({ action: "click", ref: el.ref }, `clicked [${el.ref}]`);
}

/** POSTs a single action to the shared session. */
async function sendBrowserAction(payload, description) {
  try {
    const res = await apiPost(
      `/api/browser/action?chat_id=${encodeURIComponent(browserChatId())}`, payload);
    if (res && res.error) {
      logBrowserActivity(`Action failed: ${res.error}`);
    } else {
      logBrowserActivity(description);
      if (res && res.url && browserUrlInput) browserUrlInput.value = res.url;
    }
    await refreshElements();
  } catch (e) {
    logBrowserActivity(`Action error: ${e.message}`);
  }
}


/**
 * Runs the autonomous browser agent over a goal, streaming its progress.
 * The live viewport updates on its own via the WebSocket frames.
 */
async function runBrowserGoal() {
  const goal = browserGoalInput ? browserGoalInput.value.trim() : "";
  if (!goal) return;

  if (btnBrowserRunGoal) btnBrowserRunGoal.style.display = "none";
  if (btnBrowserStopGoal) btnBrowserStopGoal.style.display = "inline-flex";
  setGoalStatus(`Working on: ${goal}`, "running");
  logBrowserActivity(`Goal: ${goal}`);
  connectLiveView();

  browserGoalAbort = new AbortController();

  try {
    const response = await fetch("/api/browser/task", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        goal,
        chat_id: browserChatId(),
        model: modelSelector ? modelSelector.value : null
      }),
      signal: browserGoalAbort.signal
    });

    if (!response.ok || !response.body) {
      throw new Error(`Server returned ${response.status}`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finalAnswer = "";
    let stoppedByUser = false;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by a blank line.
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop() || "";
      for (const chunk of chunks) {
        const line = chunk.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        let event;
        try { event = JSON.parse(line.slice(6)); } catch (e) { continue; }

        if (event.type === "log") {
          setGoalStatus(event.data, "running");
          logBrowserActivity(event.data);
          await refreshElements();
        } else if (event.type === "url") {
          if (browserUrlInput) browserUrlInput.value = event.url;
        } else if (event.type === "_step") {
          const act = event.action || {};
          const bits = [act.action];
          if (act.ref !== undefined && act.ref !== null) bits.push(`[${act.ref}]`);
          if (act.text) bits.push(`"${String(act.text).slice(0, 40)}"`);
          if (act.url) bits.push(act.url);
          logBrowserActivity(`Step ${event.step}: ${bits.join(" ")}`);
        } else if (event.type === "ask_user") {
          // The agent is parked on a question; surface it with a reply box.
          showBrowserAsk(event.question);
          setGoalStatus(event.data || "Waiting for you.", "running");
          logBrowserActivity(`Pete asked: ${event.question}`);
        } else if (event.type === "handoff") {
          setGoalStatus(event.data, "running");
          logBrowserActivity(event.data);
        } else if (event.type === "_final") {
          finalAnswer = event.content || "";
        } else if (event.type === "stopped") {
          // The server ended the run on request. The stream stays open for the
          // final answer, so this only records that the stop took effect.
          stoppedByUser = true;
        } else if (event.type === "error") {
          throw new Error(event.error || "Browser agent failed.");
        }
      }
    }

    setGoalStatus(stoppedByUser ? "Stopped."
                                : (finalAnswer ? "Done." : "Finished without an answer."),
      stoppedByUser ? null : (finalAnswer ? "done" : null));
    if (finalAnswer) {
      logBrowserActivity(`Result: ${finalAnswer.slice(0, 600)}`);
    }
  } catch (e) {
    if (e.name === "AbortError") {
      // stopBrowserGoal() has already reported the stop; repeating it here would
      // log the same thing twice.
      if (!stoppedByUser) {
        setGoalStatus("Stopped.", null);
        logBrowserActivity("Goal stopped by user.");
      }
    } else {
      setGoalStatus(e.message, "error");
      logBrowserActivity(`Goal error: ${e.message}`);
    }
  } finally {
    browserGoalAbort = null;
    if (btnBrowserRunGoal) btnBrowserRunGoal.style.display = "inline-flex";
    if (btnBrowserStopGoal) btnBrowserStopGoal.style.display = "none";
    refreshElements();
  }
}

function stopBrowserGoal() {
  // Tell the server to stop. The abort below only closes the stream, which is not
  // enough on its own: the agent keeps working until its generator happens to be
  // unwound, so the run can outlive the button press.
  //
  // This used to also POST a take-over, to nudge the agent out of its loop. That
  // was wrong -- a take-over means "the human is driving", so it left the session
  // flagged as human-controlled and the NEXT run parked on its first line. Stop is
  // now its own endpoint, so stopping leaves the wheel where the user left it.
  apiPost(`/api/browser/stop?chat_id=${encodeURIComponent(browserChatId())}`, {})
    .catch(() => { /* best effort */ });

  if (browserGoalAbort) browserGoalAbort.abort();
  hideBrowserAsk();
  setGoalStatus("Stopped.", null);
  logBrowserActivity("Goal stopped by user.");
}

