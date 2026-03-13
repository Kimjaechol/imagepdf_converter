/**
 * MoA 문서 변환기 - Main Application Logic
 */
import * as api from "./api.js";

// ─── State ─────────────────────────────────────────────
const state = {
  selectedFile: null,
  selectedFolder: null,
  outputDir: null,
  mode: "single", // single | batch
  formats: ["html", "markdown"],
  translate: false,
  sourceLanguage: "",
  targetLanguage: "ko",
  currentJobId: null,
  ws: null,
  backendHealthy: false,
};

// ─── DOM References ─────────────────────────────────────
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

function cleanupWs() {
  if (state.ws && state.ws.readyState !== WebSocket.CLOSED) {
    state.ws.close();
  }
  state.ws = null;
}

// ─── Initialization ─────────────────────────────────────
document.addEventListener("DOMContentLoaded", async () => {
  setupEventListeners();
  setupLoginOverlay();
  await checkBackendHealth();
  setInterval(checkBackendHealth, 10000);

  // Restore auth state – validate token before skipping login
  const token = api.getAuthToken();
  if (token) {
    try {
      await api.setAuthToken(token);
      // Verify the token is still valid by calling getMe
      await api.getMe();
      const userInfo = api.getUserInfo();
      if (userInfo) {
        hideLoginOverlay();
        updateAuthUI();
        return;
      }
    } catch {
      // Token expired or invalid – try refresh
      const refreshed = await api.refreshAuthToken();
      if (refreshed) {
        hideLoginOverlay();
        updateAuthUI();
        return;
      }
      // Refresh also failed – clear stale auth data
      api.clearAuth();
    }
  }
  // No valid session – show login overlay
  showLoginOverlay();
});

async function checkBackendHealth() {
  try {
    const health = await api.backendHealth();
    state.backendHealthy = health.healthy;
    updateStatusBadge(health.healthy);
  } catch {
    state.backendHealthy = false;
    updateStatusBadge(false);
  }
}

function updateStatusBadge(healthy) {
  const badge = $("#status-badge");
  if (!badge) return;
  badge.className = `status-badge ${healthy ? "connected" : "disconnected"}`;
  badge.textContent = healthy ? "연결됨" : "연결 중...";
}

// ─── Event Listeners ─────────────────────────────────────
function setupEventListeners() {
  // Mode switch
  $$(".mode-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".mode-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.mode = btn.dataset.mode;
      updateModeUI();
    });
  });

  // File selection
  $("#btn-select-file")?.addEventListener("click", handleSelectFile);
  $("#btn-select-folder")?.addEventListener("click", handleSelectFolder);
  $("#btn-select-output")?.addEventListener("click", handleSelectOutput);

  // Format checkboxes
  $$(".format-check").forEach((cb) => {
    cb.addEventListener("change", () => {
      state.formats = Array.from($$(".format-check:checked")).map((c) => c.value);
    });
  });

  // Translation toggle
  const translateToggle = $("#translate-toggle");
  if (translateToggle) {
    translateToggle.addEventListener("change", () => {
      state.translate = translateToggle.checked;
      const opts = $("#translate-options");
      if (opts) opts.style.display = translateToggle.checked ? "block" : "none";
    });
  }
  $("#source-language")?.addEventListener("change", (e) => {
    state.sourceLanguage = e.target.value;
  });
  $("#target-language")?.addEventListener("change", (e) => {
    state.targetLanguage = e.target.value;
  });

  // Convert button
  $("#btn-convert")?.addEventListener("click", handleConvert);

  // Drop zone click → file select
  const dropZone = $("#drop-zone");
  if (dropZone) {
    dropZone.addEventListener("click", (e) => {
      if (e.target.tagName !== "BUTTON") handleSelectFile();
    });
    dropZone.addEventListener("dragover", (e) => {
      e.preventDefault();
      dropZone.classList.add("dragover");
    });
    dropZone.addEventListener("dragleave", () => {
      dropZone.classList.remove("dragover");
    });
    dropZone.addEventListener("drop", (e) => {
      e.preventDefault();
      dropZone.classList.remove("dragover");
      // In Tauri v2, file drop paths come from the event
      const files = e.dataTransfer?.files;
      if (files && files.length > 0) {
        // webview file drop provides File objects, use path if available
        const file = files[0];
        if (file.path) {
          handleFileSelected(file.path);
        }
      }
    });
  }

  // Result actions
  $("#btn-open-folder")?.addEventListener("click", () => {
    if (state.outputDir) api.openFolder(state.outputDir);
  });
  $("#btn-open-editor")?.addEventListener("click", handleOpenEditor);

  // Settings tabs
  $("#tab-convert")?.addEventListener("click", () => switchTab("convert"));
  $("#tab-settings")?.addEventListener("click", () => {
    switchTab("settings");
    updateAuthUI();
  });

  // Settings apply
  setupSettingsListeners();
  setupLLMSettingsListeners();

  // Auth
  $("#btn-login")?.addEventListener("click", handleLogin);
  $("#btn-register")?.addEventListener("click", () => {
    // Show display name field on first click, then register
    const nameInput = $("#auth-display-name");
    const nameLabel = $("#auth-name-label");
    if (nameInput && nameInput.style.display === "none") {
      nameInput.style.display = "block";
      if (nameLabel) nameLabel.style.display = "block";
      return;
    }
    handleRegister();
  });
  $("#btn-logout")?.addEventListener("click", handleLogout);

  // Credits
  $("#btn-purchase-credit")?.addEventListener("click", handlePurchaseCredit);
  $("#btn-estimate-cost")?.addEventListener("click", handleEstimateCost);

  // Backend restart
  $("#btn-restart-backend")?.addEventListener("click", async () => {
    showStatus("백엔드 재시작 중...", "info");
    try {
      await api.restartBackend();
      showStatus("백엔드 재시작 완료", "success");
      await checkBackendHealth();
    } catch (e) {
      showStatus(`재시작 실패: ${e}`, "error");
    }
  });
}

function setupLLMSettingsListeners() {
  const providerSelect = $("#set-llm-provider");
  const keyGroup = $("#llm-key-group");
  const modelGroup = $("#llm-model-group");
  const saveGroup = $("#llm-save-group");
  const statusEl = $("#llm-status");

  if (!providerSelect) return;

  // Load saved config
  const saved = api.getUserLLMConfig();
  if (saved.provider) {
    providerSelect.value = saved.provider;
    if (keyGroup) keyGroup.style.display = "block";
    if (modelGroup) modelGroup.style.display = "block";
    if (saveGroup) saveGroup.style.display = "block";
    const keyInput = $("#set-llm-api-key");
    const modelInput = $("#set-llm-model");
    if (keyInput && saved.api_key) keyInput.value = saved.api_key;
    if (modelInput && saved.model) modelInput.value = saved.model;
  }

  providerSelect.addEventListener("change", () => {
    const hasProvider = !!providerSelect.value;
    if (keyGroup) keyGroup.style.display = hasProvider ? "block" : "none";
    if (modelGroup) modelGroup.style.display = hasProvider ? "block" : "none";
    if (saveGroup) saveGroup.style.display = hasProvider ? "block" : "none";
    if (!hasProvider) {
      api.clearUserLLMConfig();
      if (statusEl) statusEl.textContent = "AI 교정이 비활성화되었습니다.";
    }
  });

  $("#btn-save-llm")?.addEventListener("click", () => {
    const provider = providerSelect.value;
    const apiKey = $("#set-llm-api-key")?.value?.trim() || "";
    const model = $("#set-llm-model")?.value?.trim() || "";

    if (provider && !apiKey) {
      if (statusEl) {
        statusEl.textContent = "API 키를 입력해주세요.";
        statusEl.style.color = "#f44336";
      }
      return;
    }

    api.setUserLLMConfig({ provider, api_key: apiKey, model });
    if (statusEl) {
      const providerName = {
        gemini: "Google Gemini",
        openai: "OpenAI",
        claude: "Anthropic Claude",
      }[provider] || provider;
      statusEl.textContent = `${providerName} AI 교정이 설정되었습니다.`;
      statusEl.style.color = "#4caf50";
    }
  });
}

function setupSettingsListeners() {
  const settingsMap = {
    "set-workers": "pipeline.max_workers",
    "set-dpi": "pipeline.dpi",
    "set-ocr-lang": "ocr.languages",
    "set-reading-order": "reading_order.mode",
    "set-heading": "heading.mode",
    "set-correction": "correction.mode",
  };

  for (const [id, configKey] of Object.entries(settingsMap)) {
    const el = $(`#${id}`);
    if (el) {
      el.addEventListener("change", async () => {
        try {
          let value = el.value;
          if (id === "set-workers" || id === "set-dpi") {
            value = parseInt(value, 10);
          }
          if (id === "set-ocr-lang") {
            value = value.split(",");
          }
          await api.updateConfig(configKey, value);
        } catch (e) {
          console.error(`Failed to update ${configKey}:`, e);
        }
      });
    }
  }
}

// ─── File Selection ─────────────────────────────────────
async function handleSelectFile() {
  const path = await api.selectDocumentFile();
  if (!path) return;
  handleFileSelected(path);
}

function handleFileSelected(path) {
  state.selectedFile = path;
  const fileName = path.split(/[\\/]/).pop();
  const ext = api.getFileExtension(path);

  const fileInfo = $("#file-info");
  if (fileInfo) {
    const icon = getFileIcon(ext);
    let engine;
    if (api.isPdfFormat(ext)) {
      engine = "PDF 자동 감지 (디지털: PyMuPDF / 이미지: Upstage OCR)";
    } else {
      engine = "한컴 DocsConverter";
    }
    const llmConfig = api.getUserLLMConfig();
    const llmLabel = llmConfig.provider
      ? ` + ${llmConfig.provider.toUpperCase()} 교정`
      : "";
    fileInfo.innerHTML = `
      <div class="file-card">
        <span class="file-icon">${icon}</span>
        <div class="file-details">
          <div class="file-name">${fileName}</div>
          <div class="file-meta">${ext.toUpperCase()} · ${engine}${llmLabel}</div>
        </div>
      </div>`;
  }

  // Auto-set output dir to parent directory of selected file
  if (!state.outputDir) {
    const lastSep = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
    if (lastSep > 0) {
      state.outputDir = path.substring(0, lastSep);
      const outputInfo = $("#output-info");
      if (outputInfo) outputInfo.textContent = state.outputDir;
    }
  }

  updateConvertButton();
}

async function handleSelectFolder() {
  const path = await api.selectFolder();
  if (!path) return;
  state.selectedFolder = path;
  const folderInfo = $("#folder-info");
  if (folderInfo) folderInfo.textContent = path;
  if (!state.outputDir) state.outputDir = path;
  updateConvertButton();
}

async function handleSelectOutput() {
  const path = await api.selectOutputDir();
  if (!path) return;
  state.outputDir = path;
  const outputInfo = $("#output-info");
  if (outputInfo) outputInfo.textContent = path;
}

// ─── Conversion ─────────────────────────────────────────
async function handleConvert() {
  if (state.mode === "single" && !state.selectedFile) {
    showStatus("파일을 선택해주세요", "warning");
    return;
  }
  if (state.mode === "batch" && !state.selectedFolder) {
    showStatus("폴더를 선택해주세요", "warning");
    return;
  }
  if (state.formats.length === 0) {
    showStatus("출력 형식을 선택해주세요", "warning");
    return;
  }

  // Warn if API key is not configured on the server
  try {
    const keyStatus = await api.getApiKeyStatus();
    if (!keyStatus.configured) {
      showStatus(
        "⚠ 서버 API 키 미설정: 관리자에게 문의하세요.",
        "warning",
      );
    }
  } catch {
    // Backend may not support this yet, continue anyway
  }

  const btn = $("#btn-convert");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "변환 중...";
  }
  showProgress(true);
  updateProgress(0, "변환 준비 중...");

  try {
    if (state.mode === "single") {
      await convertSingle();
    } else {
      await convertBatch();
    }
  } catch (e) {
    showStatus(`변환 실패: ${e}`, "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "변환 시작";
    }
  }
}

async function convertSingle() {
  const ext = api.getFileExtension(state.selectedFile);

  // Check credits for PDF conversion (costs money)
  if (api.isPdfFormat(ext) && api.getAuthToken()) {
    try {
      const creditInfo = await api.getCredits();
      if (creditInfo.balance_usd <= 0) {
        showCreditExhaustedPopup();
        return;
      }
    } catch {
      // Backend may not support credits yet, continue
    }
  }

  const label = state.translate ? `${ext.toUpperCase()} 변환 + 번역 중...` : `${ext.toUpperCase()} 변환 중...`;
  updateProgress(30, label);

  try {
    let result;
    try {
      result = await api.convertDocument(
        state.selectedFile,
        state.outputDir,
        state.formats,
        state.translate,
        state.sourceLanguage,
        state.targetLanguage
      );
    } catch (firstErr) {
      // If 401 Unauthorized, try refreshing the token and retry once
      if (String(firstErr).includes("401")) {
        const refreshed = await api.refreshAuthToken();
        if (refreshed) {
          result = await api.convertDocument(
            state.selectedFile,
            state.outputDir,
            state.formats,
            state.translate,
            state.sourceLanguage,
            state.targetLanguage
          );
        } else {
          api.clearAuth();
          updateAuthUI();
          showLoginOverlay();
          showStatus("세션이 만료되었습니다. 다시 로그인해주세요.", "error");
          return;
        }
      } else {
        throw firstErr;
      }
    }

    // If backend returned a job_id, track progress via WebSocket
    if (result.job_id) {
      const jobId = result.job_id;
      state.currentJobId = jobId;

      const ws = await api.connectProgress(jobId, async (data) => {
        if (data._wsClose) return;
        if (data.progress !== undefined) {
          updateProgress(data.progress * 100, data.message || "처리 중...");
        }
        if (data.status === "completed") {
          updateProgress(90, "변환 완료, AI 교정 확인 중...");
          cleanupWs();
          // Apply optional LLM correction if user has configured it
          await applyOptionalLLMCorrection(jobId, data);
        }
        if (data.status === "failed") {
          showStatus(`변환 실패: ${data.message}`, "error");
          cleanupWs();
        }
      });
      state.ws = ws;

      if (!ws) {
        pollJobProgress(jobId);
      }
    } else {
      // Direct result (Hancom or Rust-native)
      // Apply optional LLM correction for direct results
      result = await maybeApplyLLMCorrection(result, ext);

      const engine = result.engine === "hancom" ? "한컴 DocsConverter" : "네이티브";
      const corrected = result.llm_corrected ? " + AI 교정" : "";
      updateProgress(100, "변환 완료!");
      showResults(result);
      showStatus(`변환 완료! (${engine}${corrected})`, "success");
    }
  } catch (e) {
    showStatus(`변환 실패: ${e}`, "error");
  }
}

/**
 * Apply optional LLM correction to conversion results.
 * Only runs if the user has configured their own LLM API key in settings.
 */
async function maybeApplyLLMCorrection(result, ext) {
  const llmConfig = api.getUserLLMConfig();
  if (!llmConfig.provider || !llmConfig.api_key) {
    return result; // No LLM configured, skip correction
  }

  if (!result.html) {
    return result; // No HTML to correct
  }

  const sourceType = api.isPdfFormat(ext) ? "image_pdf" : "document";

  try {
    updateProgress(92, `${llmConfig.provider.toUpperCase()} AI 교정 중...`);
    const corrected = await api.correctWithLLM(
      result.html,
      llmConfig.provider,
      llmConfig.api_key,
      llmConfig.model || "",
      sourceType,
    );

    if (corrected.corrected && corrected.html) {
      result.html = corrected.html;
      result.llm_corrected = true;
      result.llm_provider = corrected.provider;

      // Re-save the corrected HTML if we have output files
      if (result.output_files) {
        const htmlFile = result.output_files.find((f) => /\.html?$/i.test(f));
        if (htmlFile) {
          try {
            await api.writeFile(htmlFile, corrected.html);
          } catch {
            // File write may fail, continue
          }
        }
      }
    }
  } catch (e) {
    console.warn("LLM correction failed:", e);
    // Don't fail the whole conversion, just skip correction
  }

  return result;
}

/**
 * Apply optional LLM correction after a job completes (WebSocket flow).
 */
async function applyOptionalLLMCorrection(jobId, data) {
  try {
    const status = await api.getJobStatus(jobId);
    if (!status.result) {
      updateProgress(100, "변환 완료!");
      showStatus("변환 완료!", "success");
      return;
    }

    // Check if LLM correction is configured
    const llmConfig = api.getUserLLMConfig();
    const hasLLM = llmConfig.provider && llmConfig.api_key;

    if (hasLLM && status.result.output_files) {
      const htmlFile = status.result.output_files.find((f) => /\.html?$/i.test(f));
      if (htmlFile) {
        try {
          updateProgress(92, `${llmConfig.provider.toUpperCase()} AI 교정 중...`);
          const htmlContent = await api.readFile(htmlFile);
          if (htmlContent) {
            const corrected = await api.correctWithLLM(
              htmlContent,
              llmConfig.provider,
              llmConfig.api_key,
              llmConfig.model || "",
              "image_pdf",
            );
            if (corrected.corrected && corrected.html) {
              await api.writeFile(htmlFile, corrected.html);
              // Also update markdown file
              const mdFile = status.result.output_files.find((f) => /\.md$/i.test(f));
              if (mdFile) {
                try {
                  const md = await api.htmlToMarkdown(corrected.html);
                  await api.writeFile(mdFile, md);
                } catch { /* continue */ }
              }
            }
          }
        } catch (e) {
          console.warn("Post-job LLM correction failed:", e);
        }
      }
    }

    updateProgress(100, "변환 완료!");
    showStatus(
      hasLLM ? "변환 완료! (AI 교정 적용)" : "변환 완료!",
      "success"
    );
    showResults(status.result);
  } catch (e) {
    updateProgress(100, "변환 완료!");
    showStatus("변환 완료!", "success");
    pollJobResult(jobId);
  }
}

async function convertBatch() {
  const resp = await api.convertBatch(
    state.selectedFolder,
    state.outputDir,
    state.formats,
    true
  );
  const jobId = resp.job_id;
  state.currentJobId = jobId;

  const ws = await api.connectProgress(jobId, (data) => {
    if (data._wsClose) return;
    if (data.progress !== undefined) {
      updateProgress(data.progress * 100, data.message || "처리 중...");
    }
    if (data.status === "completed") {
      updateProgress(100, "배치 변환 완료!");
      showStatus("배치 변환 완료!", "success");
      pollJobResult(jobId);
      cleanupWs();
    }
    if (data.status === "failed") {
      showStatus(`배치 변환 실패: ${data.message}`, "error");
      cleanupWs();
    }
  });
  state.ws = ws;
  if (!ws) pollJobProgress(jobId);
}

async function pollJobProgress(jobId) {
  const poll = async () => {
    try {
      const status = await api.getJobStatus(jobId);
      updateProgress(status.progress * 100, status.message);
      if (status.status === "completed") {
        updateProgress(100, "완료!");
        showStatus("변환 완료!", "success");
        showResults(status.result);
        return;
      }
      if (status.status === "failed") {
        showStatus(`실패: ${status.message}`, "error");
        return;
      }
      setTimeout(poll, 1000);
    } catch {
      setTimeout(poll, 2000);
    }
  };
  poll();
}

async function pollJobResult(jobId) {
  try {
    const status = await api.getJobStatus(jobId);
    if (status.result) {
      showResults(status.result);
    }
  } catch (e) {
    console.error("Failed to poll job result:", e);
  }
}

// ─── UI Updates ─────────────────────────────────────────
function updateModeUI() {
  const singleUI = $("#single-mode");
  const batchUI = $("#batch-mode");
  if (singleUI) singleUI.style.display = state.mode === "single" ? "block" : "none";
  if (batchUI) batchUI.style.display = state.mode === "batch" ? "block" : "none";
}

function updateConvertButton() {
  const btn = $("#btn-convert");
  if (!btn) return;
  const hasInput = state.mode === "single" ? !!state.selectedFile : !!state.selectedFolder;
  btn.disabled = !hasInput || state.formats.length === 0;
}

function showProgress(visible) {
  const el = $("#progress-section");
  if (el) el.style.display = visible ? "block" : "none";
}

function updateProgress(percent, message) {
  const bar = $("#progress-bar");
  const msg = $("#progress-message");
  if (bar) {
    bar.style.width = `${percent}%`;
    bar.setAttribute("data-percent", `${Math.round(percent)}%`);
  }
  if (msg) msg.textContent = message || "";
}

function showStatus(message, type) {
  const el = $("#status-message");
  if (!el) return;
  el.textContent = message;
  el.className = `status-message ${type}`;
  el.style.display = "block";
  if (type === "success" || type === "info") {
    setTimeout(() => { el.style.display = "none"; }, 5000);
  }
}

async function showResults(result) {
  // Hide welcome, show results
  const welcome = $("#welcome-section");
  if (welcome) welcome.style.display = "none";

  const el = $("#results-section");
  if (!el) return;
  el.style.display = "block";

  const fileList = $("#result-files");
  if (!fileList) return;

  const files = result?.output_files || result?.outputFiles || [];
  fileList.innerHTML = "";
  files.forEach((f) => {
    const name = f.split(/[\\/]/).pop();
    const ext = name.split(".").pop();
    const div = document.createElement("div");
    div.className = "result-file";
    div.dataset.path = f;
    div.innerHTML = `<span class="file-icon">${getFileIcon(ext)}</span>
        <span class="file-name"></span>`;
    div.querySelector(".file-name").textContent = name;
    fileList.appendChild(div);
  });

  fileList.querySelectorAll(".result-file").forEach((item) => {
    item.addEventListener("click", async () => {
      const filePath = item.dataset.path;
      const ext = filePath.split(".").pop().toLowerCase();
      if (ext === "html" || ext === "htm") {
        // Open HTML files in the built-in editor
        try {
          await api.openEditorWindow(filePath);
        } catch (e) {
          // Fallback to system open
          api.openFile(filePath);
        }
      } else {
        api.openFile(filePath);
      }
    });
  });

  // Auto-open the first HTML file in the editor after conversion
  const firstHtml = files.find((f) => /\.html?$/i.test(f));
  if (firstHtml) {
    try {
      await api.openEditorWindow(firstHtml);
    } catch (e) {
      console.warn("Auto-open editor failed:", e);
    }
  }
}

function switchTab(tab) {
  $$(".tab-btn").forEach((b) => b.classList.remove("active"));
  const activeTab = $(`#tab-${tab}`);
  if (activeTab) activeTab.classList.add("active");
  $$(".tab-content").forEach((c) => { c.style.display = "none"; });
  const activeContent = $(`#content-${tab}`);
  if (activeContent) activeContent.style.display = "block";
}

async function handleOpenEditor() {
  try {
    await api.openEditorWindow();
  } catch (e) {
    console.error("Failed to open editor:", e);
    showStatus("에디터 열기 실패: " + e, "error");
  }
}

function getFileIcon(ext) {
  const icons = {
    pdf: "\u{1F4C4}",
    doc: "\u{1F4DD}", docx: "\u{1F4DD}",
    hwp: "\u{1F4D1}", hwpx: "\u{1F4D1}",
    xls: "\u{1F4CA}", xlsx: "\u{1F4CA}",
    ppt: "\u{1F4CA}", pptx: "\u{1F4CA}",
    html: "\u{1F310}", md: "\u{1F4D6}",
  };
  return icons[ext] || "\u{1F4C1}";
}

// ─── Settings Status ─────────────────────────────────────
function showSettingsStatus(message, type) {
  const el = $("#settings-status-message");
  if (!el) return;
  el.textContent = message;
  el.className = `status-message ${type}`;
  el.style.display = "block";
  if (type === "success" || type === "info") {
    setTimeout(() => { el.style.display = "none"; }, 5000);
  }
}

// ─── Login Overlay ───────────────────────────────────────
function setupLoginOverlay() {
  const overlay = $("#login-overlay");
  if (!overlay) return;

  // Login button
  $("#overlay-btn-login")?.addEventListener("click", handleOverlayLogin);

  // Register button – first click shows name field, second click registers
  let registerMode = false;
  $("#overlay-btn-register")?.addEventListener("click", () => {
    if (!registerMode) {
      const nameField = $("#login-name-field");
      if (nameField) nameField.style.display = "block";
      registerMode = true;
      $("#overlay-btn-register").textContent = "가입하기";
      return;
    }
    handleOverlayRegister();
  });

  // Guest button
  $("#overlay-btn-guest")?.addEventListener("click", () => {
    hideLoginOverlay();
  });

  // Enter key on password field triggers login
  $("#login-password")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      if (registerMode) handleOverlayRegister();
      else handleOverlayLogin();
    }
  });

  // Enter key on email field moves to password
  $("#login-email")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      $("#login-password")?.focus();
    }
  });
}

function showLoginOverlay() {
  const overlay = $("#login-overlay");
  if (overlay) overlay.classList.remove("hidden");
}

function hideLoginOverlay() {
  const overlay = $("#login-overlay");
  if (overlay) overlay.classList.add("hidden");
}

function showLoginError(msg) {
  const el = $("#login-error");
  if (el) el.textContent = msg;
}

async function handleOverlayLogin() {
  const email = $("#login-email")?.value?.trim();
  const password = $("#login-password")?.value;
  if (!email || !password) {
    showLoginError("이메일과 비밀번호를 입력하세요");
    return;
  }
  showLoginError("");
  const btn = $("#overlay-btn-login");
  if (btn) { btn.disabled = true; btn.textContent = "로그인 중..."; }

  try {
    const result = await api.login(email, password);
    await api.setAuthToken(result.token);
    // Store refresh token if provided (Supabase)
    if (result.refresh_token) {
      localStorage.setItem("refresh_token", result.refresh_token);
    }
    api.setUserInfo(result);
    hideLoginOverlay();
    updateAuthUI();
  } catch (e) {
    showLoginError(`로그인 실패: ${e}`);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "로그인"; }
  }
}

async function handleOverlayRegister() {
  const email = $("#login-email")?.value?.trim();
  const password = $("#login-password")?.value;
  const displayName = $("#login-display-name")?.value?.trim();
  if (!email || !password) {
    showLoginError("이메일과 비밀번호를 입력하세요");
    return;
  }
  if (password.length < 6) {
    showLoginError("비밀번호는 6자 이상이어야 합니다");
    return;
  }
  showLoginError("");
  const btn = $("#overlay-btn-register");
  if (btn) { btn.disabled = true; btn.textContent = "가입 중..."; }

  try {
    const result = await api.register(email, password, displayName);
    await api.setAuthToken(result.token);
    if (result.refresh_token) {
      localStorage.setItem("refresh_token", result.refresh_token);
    }
    api.setUserInfo(result);

    // If email confirmation required (Supabase), token may be empty
    if (!result.token && result.email_confirmed === false) {
      showLoginError("가입 완료! 이메일 인증 후 로그인하세요.");
      if (btn) { btn.disabled = false; btn.textContent = "가입하기"; }
      return;
    }

    hideLoginOverlay();
    updateAuthUI();
  } catch (e) {
    showLoginError(`회원가입 실패: ${e}`);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "가입하기"; }
  }
}


// ─── Auth Management ────────────────────────────────────
function updateAuthUI() {
  const userInfo = api.getUserInfo();
  const authSection = $("#auth-section");
  const creditSection = $("#credit-section");
  const userInfoEl = $("#user-info");

  if (userInfo && api.getAuthToken()) {
    // Logged in
    if (authSection) authSection.style.display = "none";
    if (creditSection) creditSection.style.display = "block";
    if (userInfoEl) {
      userInfoEl.style.display = "flex";
      const nameEl = $("#user-display-name");
      if (nameEl) nameEl.textContent = userInfo.display_name || userInfo.email;
    }
    loadCreditBalance();
  } else {
    // Not logged in
    if (authSection) authSection.style.display = "block";
    if (creditSection) creditSection.style.display = "none";
    if (userInfoEl) userInfoEl.style.display = "none";
  }
}

async function handleLogin() {
  const email = $("#auth-email")?.value?.trim();
  const password = $("#auth-password")?.value;
  if (!email || !password) {
    showAuthStatus("이메일과 비밀번호를 입력하세요", "error");
    return;
  }
  try {
    const result = await api.login(email, password);
    await api.setAuthToken(result.token);
    api.setUserInfo(result);
    showAuthStatus("로그인 성공!", "success");
    updateAuthUI();
  } catch (e) {
    showAuthStatus(`로그인 실패: ${e}`, "error");
  }
}

async function handleRegister() {
  const email = $("#auth-email")?.value?.trim();
  const password = $("#auth-password")?.value;
  const displayName = $("#auth-display-name")?.value?.trim();
  if (!email || !password) {
    showAuthStatus("이메일과 비밀번호를 입력하세요", "error");
    return;
  }
  if (password.length < 6) {
    showAuthStatus("비밀번호는 6자 이상이어야 합니다", "error");
    return;
  }
  try {
    const result = await api.register(email, password, displayName);
    await api.setAuthToken(result.token);
    api.setUserInfo(result);
    showAuthStatus("회원가입 성공!", "success");
    updateAuthUI();
  } catch (e) {
    showAuthStatus(`회원가입 실패: ${e}`, "error");
  }
}

function handleLogout() {
  api.clearAuth();
  localStorage.removeItem("refresh_token");
  updateAuthUI();
  showLoginOverlay();
  showSettingsStatus("로그아웃 완료", "info");
}

function showAuthStatus(message, type) {
  const el = $("#auth-status");
  if (!el) return;
  el.textContent = message;
  el.style.color = type === "error" ? "#f44336" : "#4caf50";
}

// ─── Credit Management ──────────────────────────────────
function formatKRW(amount) {
  return `\u20A9${Math.round(amount).toLocaleString()}`;
}

async function loadCreditBalance() {
  try {
    const info = await api.getCredits();
    const el = $("#credit-balance");
    if (el) {
      el.innerHTML = `$${info.balance_usd.toFixed(4)} <span style="color:#888;font-size:0.9em">(${formatKRW(info.balance_krw)})</span>`;
      el.style.color = info.balance_usd > 0 ? "#4caf50" : "#f44336";
    }
    // Update header badge
    const badge = $("#user-balance-badge");
    if (badge) {
      badge.textContent = `$${info.balance_usd.toFixed(2)} (${formatKRW(info.balance_krw)})`;
    }
  } catch {
    // Not logged in or backend not ready
  }
}

async function handlePurchaseCredit() {
  const amount = parseFloat($("#credit-amount")?.value || "0");
  if (amount <= 0) {
    showSettingsStatus("충전 금액을 입력해주세요", "warning");
    return;
  }
  try {
    // Try Stripe checkout first
    const checkout = await api.createCheckout(amount);
    if (checkout.checkout_url) {
      window.open(checkout.checkout_url, "_blank");
      return;
    }
  } catch {
    // Stripe not configured, use manual top-up
  }
  try {
    const result = await api.purchaseCredits(amount);
    showSettingsStatus(`$${amount.toFixed(2)} (${formatKRW(result.amount_krw)}) 충전 완료. 잔액: $${result.new_balance_usd.toFixed(4)} (${formatKRW(result.new_balance_krw)})`, "success");
    loadCreditBalance();
  } catch (e) {
    showSettingsStatus(`충전 실패: ${e}`, "error");
  }
}

async function handleEstimateCost() {
  const pages = parseInt($("#estimate-pages")?.value || "0", 10);
  const docType = $("#estimate-doc-type")?.value || "image_pdf";
  if (pages <= 0) return;
  try {
    const est = await api.estimateCost(pages, docType);
    const el = $("#cost-estimate-result");
    if (el) {
      const typeLabel = docType === "image_pdf" ? "이미지 PDF"
        : docType === "digital_pdf" ? "디지털 PDF" : "기타 문서";
      if (est.charged_usd === 0) {
        el.innerHTML = `${typeLabel} ${pages}페이지: <strong>무료</strong>`;
      } else {
        el.innerHTML = `
          ${typeLabel} ${pages}페이지:<br>
          단가: $${est.per_page_usd} / 페이지 (${formatKRW(est.per_page_krw)})<br>
          합계: <strong>$${est.charged_usd.toFixed(4)} (${formatKRW(est.charged_krw)})</strong><br>
          <span style="font-size:0.85em;color:#888;">환율: 1 USD = ${formatKRW(est.exchange_rate)}</span>
        `;
      }
    }
  } catch (e) {
    showSettingsStatus(`추산 실패: ${e}`, "error");
  }
}

// ─── Credit Exhausted Popup ─────────────────────────────
async function showCreditExhaustedPopup() {
  // Remove existing popup if any
  const existing = document.getElementById("credit-popup-overlay");
  if (existing) existing.remove();

  // Fetch exchange rate for KRW display
  let rate = 1350;
  try {
    const rateInfo = await api.getExchangeRate();
    if (rateInfo && rateInfo.rate) rate = rateInfo.rate;
  } catch { /* use default */ }

  const imgPdfKrw = Math.round(0.02 * rate);
  const digPdfKrw = Math.round(0.005 * rate);

  const overlay = document.createElement("div");
  overlay.id = "credit-popup-overlay";
  overlay.style.cssText = `
    position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.6); z-index: 10000;
    display: flex; align-items: center; justify-content: center;
  `;

  overlay.innerHTML = `
    <div style="
      background: white; border-radius: 12px; padding: 32px; max-width: 420px;
      box-shadow: 0 8px 32px rgba(0,0,0,0.3); text-align: center;
    ">
      <div style="font-size: 48px; margin-bottom: 16px;">&#x1F4B3;</div>
      <h3 style="margin: 0 0 12px 0; color: #333; font-size: 18px;">
        무료 크레딧이 모두 소진되었습니다
      </h3>
      <p style="color: #666; margin: 0 0 20px 0; line-height: 1.6;">
        PDF 문서 변환을 계속하려면 크레딧을 충전해 주세요.<br>
        <span style="font-size: 13px; color: #999;">
          이미지 PDF: $0.02/페이지 (\u20A9${imgPdfKrw}) &middot; 디지털 PDF: $0.005/페이지 (\u20A9${digPdfKrw})<br>
          기타 문서 (DOCX, HWPX 등): 무료
        </span>
      </p>
      <div style="display: flex; gap: 8px; justify-content: center;">
        <button id="credit-popup-purchase" style="
          background: #4caf50; color: white; border: none; padding: 10px 24px;
          border-radius: 6px; cursor: pointer; font-size: 14px; font-weight: bold;
        ">크레딧 충전하기</button>
        <button id="credit-popup-close" style="
          background: #eee; color: #666; border: none; padding: 10px 24px;
          border-radius: 6px; cursor: pointer; font-size: 14px;
        ">닫기</button>
      </div>
    </div>
  `;

  document.body.appendChild(overlay);

  document.getElementById("credit-popup-purchase").addEventListener("click", () => {
    overlay.remove();
    // Switch to settings tab and scroll to credit section
    switchTab("settings");
    const creditSection = $("#credit-section");
    if (creditSection) {
      creditSection.style.display = "block";
      creditSection.scrollIntoView({ behavior: "smooth" });
    }
  });

  document.getElementById("credit-popup-close").addEventListener("click", () => {
    overlay.remove();
  });

  // Close on overlay click
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) overlay.remove();
  });
}
