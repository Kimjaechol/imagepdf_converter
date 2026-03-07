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
  await checkBackendHealth();
  setInterval(checkBackendHealth, 10000);
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
    loadApiKeyStatus();
    loadCreditBalance();
  });

  // Settings apply
  setupSettingsListeners();

  // API Key
  $("#btn-save-api-key")?.addEventListener("click", handleSaveApiKey);

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
    const engine = api.isRustNativeFormat(ext) ? "Rust 네이티브" : "Python AI 파이프라인";
    fileInfo.innerHTML = `
      <div class="file-card">
        <span class="file-icon">${icon}</span>
        <div class="file-details">
          <div class="file-name">${fileName}</div>
          <div class="file-meta">${ext.toUpperCase()} · ${engine}</div>
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

  // Unified conversion - routes via convert_document command which
  // handles backend availability check and Rust-native fallback for PDF
  const label = state.translate ? `${ext.toUpperCase()} 변환 + 번역 중...` : `${ext.toUpperCase()} 변환 중...`;
  updateProgress(30, label);

  try {
    const result = await api.convertDocument(
      state.selectedFile,
      state.outputDir,
      state.formats,
      state.translate,
      state.sourceLanguage,
      state.targetLanguage
    );

    // If backend returned a job_id, track progress via WebSocket
    if (result.job_id) {
      const jobId = result.job_id;
      state.currentJobId = jobId;

      const ws = await api.connectProgress(jobId, (data) => {
        if (data._wsClose) return;
        if (data.progress !== undefined) {
          updateProgress(data.progress * 100, data.message || "처리 중...");
        }
        if (data.status === "completed") {
          updateProgress(100, "변환 완료!");
          showStatus("변환 완료!", "success");
          pollJobResult(jobId);
          cleanupWs();
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
      // Rust-native result (instant)
      updateProgress(100, "변환 완료!");
      showResults(result);
      showStatus("변환 완료!", "success");
    }
  } catch (e) {
    showStatus(`변환 실패: ${e}`, "error");
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

function showResults(result) {
  // Hide welcome, show results
  const welcome = $("#welcome-section");
  if (welcome) welcome.style.display = "none";

  const el = $("#results-section");
  if (!el) return;
  el.style.display = "block";

  const fileList = $("#result-files");
  if (!fileList) return;

  const files = result?.output_files || result?.outputFiles || [];
  fileList.innerHTML = files
    .map((f) => {
      const name = f.split(/[\\/]/).pop();
      const ext = name.split(".").pop();
      return `<div class="result-file" data-path="${f}">
        <span class="file-icon">${getFileIcon(ext)}</span>
        <span class="file-name">${name}</span>
      </div>`;
    })
    .join("");

  fileList.querySelectorAll(".result-file").forEach((item) => {
    item.addEventListener("click", () => {
      api.openFile(item.dataset.path);
    });
  });
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
  window.open("editor.html", "_blank", "width=1100,height=750");
}

function getFileIcon(ext) {
  const icons = {
    pdf: "\u{1F4C4}", docx: "\u{1F4DD}", hwpx: "\u{1F4D1}",
    xlsx: "\u{1F4CA}", pptx: "\u{1F4CA}", html: "\u{1F310}", md: "\u{1F4D6}",
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

// ─── API Key Management ─────────────────────────────────
async function loadApiKeyStatus() {
  try {
    const status = await api.getApiKeyStatus();
    const el = $("#api-key-status");
    if (el) {
      el.textContent = status.configured
        ? `설정됨: ${status.masked}`
        : "미설정 (Gemini AI 기능 비활성)";
      el.style.color = status.configured ? "#4caf50" : "#f44336";
    }
  } catch {
    // Backend may not support this yet
  }
}

async function handleSaveApiKey() {
  const input = $("#set-api-key");
  if (!input || !input.value.trim()) return;
  try {
    await api.setApiKey(input.value.trim());
    input.value = "";
    showSettingsStatus("API 키 저장 완료", "success");
    loadApiKeyStatus();
  } catch (e) {
    showSettingsStatus(`API 키 저장 실패: ${e}`, "error");
  }
}

// ─── Credit Management ──────────────────────────────────
async function loadCreditBalance() {
  try {
    const userId = $("#credit-user-id")?.value || "default";
    const info = await api.getCredits(userId);
    const el = $("#credit-balance");
    if (el) {
      el.textContent = `$${info.balance_usd.toFixed(4)}`;
      el.style.color = info.balance_usd > 0 ? "#4caf50" : "#f44336";
    }
  } catch {
    // Backend may not support this yet
  }
}

async function handlePurchaseCredit() {
  const userId = $("#credit-user-id")?.value || "default";
  const amount = parseFloat($("#credit-amount")?.value || "0");
  if (amount <= 0) {
    showSettingsStatus("충전 금액을 입력해주세요", "warning");
    return;
  }
  try {
    const result = await api.purchaseCredits(userId, amount);
    showSettingsStatus(`$${amount.toFixed(2)} 충전 완료. 잔액: $${result.new_balance_usd.toFixed(4)}`, "success");
    loadCreditBalance();
  } catch (e) {
    showSettingsStatus(`충전 실패: ${e}`, "error");
  }
}

async function handleEstimateCost() {
  const pages = parseInt($("#estimate-pages")?.value || "0", 10);
  if (pages <= 0) return;
  try {
    const est = await api.estimateCost(pages);
    const el = $("#cost-estimate-result");
    if (el) {
      el.innerHTML = `
        ${pages}페이지 추산:<br>
        API 원가: $${est.raw_cost_usd.toFixed(4)}<br>
        이용자 부과액 (${est.markup}배): <strong>$${est.charged_usd.toFixed(4)}</strong>
      `;
    }
  } catch (e) {
    showSettingsStatus(`추산 실패: ${e}`, "error");
  }
}
