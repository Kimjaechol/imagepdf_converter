/**
 * Tauri API Bridge - Backend communication layer
 * Uses Tauri v2 __TAURI__ global API (withGlobalTauri: true)
 */

const { invoke } = window.__TAURI__.core;
const { open: openDialog, save: saveDialog } = window.__TAURI__.dialog;
const { open: shellOpen } = window.__TAURI__.shell;

// ─── Backend URL ───────────────────────────────────────
let _backendUrl = null;

export async function getBackendUrl() {
  if (!_backendUrl) {
    _backendUrl = await invoke("get_backend_url");
  }
  return _backendUrl;
}

// ─── File Operations ───────────────────────────────────
export async function selectDocumentFile() {
  return await openDialog({
    multiple: false,
    filters: [
      { name: "지원 문서", extensions: ["pdf", "hwp", "hwpx", "doc", "docx", "xls", "xlsx", "ppt", "pptx"] },
      { name: "PDF", extensions: ["pdf"] },
      { name: "한글", extensions: ["hwp", "hwpx"] },
      { name: "Word", extensions: ["doc", "docx"] },
      { name: "Excel", extensions: ["xls", "xlsx"] },
      { name: "PowerPoint", extensions: ["ppt", "pptx"] },
    ],
  });
}

export async function selectFolder() {
  return await openDialog({ directory: true });
}

export async function selectOutputDir() {
  return await openDialog({ directory: true });
}

export async function selectHtmlFile() {
  return await openDialog({
    multiple: false,
    filters: [
      { name: "HTML", extensions: ["html", "htm"] },
      { name: "Markdown", extensions: ["md"] },
      { name: "모든 파일", extensions: ["*"] },
    ],
  });
}

export async function saveFileDialog(defaultName, filters) {
  return await saveDialog({
    defaultPath: defaultName,
    filters: filters || [
      { name: "HTML", extensions: ["html"] },
      { name: "Markdown", extensions: ["md"] },
    ],
  });
}

export async function readFile(path) {
  return await invoke("read_file_content", { path });
}

export async function writeFile(path, content) {
  return await invoke("write_file_content", { path, content });
}

export async function openFolder(path) {
  try {
    await invoke("open_path_native", { path });
  } catch (e) {
    // Fallback to shell:open
    console.warn("open_path_native failed, trying shell:open:", e);
    await shellOpen(path);
  }
}

export async function openFile(path) {
  try {
    await invoke("open_path_native", { path });
  } catch (e) {
    // Fallback to shell:open
    console.warn("open_path_native failed, trying shell:open:", e);
    await shellOpen(path);
  }
}

// ─── PDF Conversion (Python Backend) ──────────────────
export async function convertPdf(inputPath, outputDir, formats, translate, sourceLang, targetLang) {
  return await invoke("convert_pdf", {
    request: {
      input_path: inputPath,
      output_dir: outputDir || null,
      formats: formats || ["html", "markdown"],
      translate: translate || false,
      source_language: sourceLang || "",
      target_language: targetLang || "ko",
    },
  });
}

export async function convertBatch(folderPath, outputDir, formats, recursive) {
  return await invoke("convert_batch", {
    request: {
      folder_path: folderPath,
      output_dir: outputDir || null,
      formats: formats || ["html", "markdown"],
      recursive: recursive || false,
    },
  });
}

// ─── Document Conversion (Unified) ────────────────────
// Routes to Hancom DocsConverter (non-PDF) or Upstage+Gemini pipeline (PDF)
export async function convertDocument(inputPath, outputDir, formats, translate, sourceLang, targetLang) {
  return await invoke("convert_document", {
    inputPath: inputPath,
    outputDir: outputDir || null,
    formats: formats || ["html", "markdown"],
    translate: translate || false,
    sourceLanguage: sourceLang || "",
    targetLanguage: targetLang || "ko",
  });
}

// Rust-native only (docx/hwpx/xlsx/pptx)
export async function convertNativeDocument(inputPath, outputDir, formats) {
  return await invoke("convert_any_document", {
    inputPath: inputPath,
    outputDir: outputDir || null,
    formats: formats || ["html", "markdown"],
  });
}

// ─── Job Management ───────────────────────────────────
export async function getJobStatus(jobId) {
  return await invoke("get_job_status", { jobId: jobId });
}

export async function listJobs() {
  return await invoke("list_jobs");
}

// ─── Config ───────────────────────────────────────────
export async function getConfig() {
  return await invoke("get_config");
}

export async function updateConfig(key, value) {
  return await invoke("update_config", { update: { key, value } });
}

export async function addDictionaryTerm(wrong, correct, category) {
  return await invoke("add_dictionary_term", {
    term: { wrong, correct, category: category || null },
  });
}

// ─── Backend Lifecycle ────────────────────────────────
export async function restartBackend() {
  return await invoke("restart_backend");
}

export async function backendHealth() {
  return await invoke("backend_health");
}

// ─── MoA Integration ─────────────────────────────────
export async function moaConvert(request) {
  return await invoke("moa_convert", { request });
}

export async function moaHealth() {
  return await invoke("moa_health");
}

export async function moaSupportedFormats() {
  return await invoke("moa_supported_formats");
}

export async function moaToolManifest() {
  return await invoke("moa_tool_manifest");
}

// ─── Auth ─────────────────────────────────────────────
export async function register(email, password, displayName) {
  return await invoke("auth_register", { email, password, displayName: displayName || "" });
}

export async function login(email, password) {
  return await invoke("auth_login", { email, password });
}

export async function getMe() {
  return await invoke("auth_get_me");
}

export function getAuthToken() {
  return localStorage.getItem("auth_token") || "";
}

export async function setAuthToken(token) {
  localStorage.setItem("auth_token", token);
  // Also sync to Tauri state so Rust commands can use it
  try {
    await invoke("set_auth_token", { token });
  } catch {
    // Backend may not support this yet
  }
}

export async function refreshAuthToken() {
  const refreshToken = localStorage.getItem("refresh_token");
  if (!refreshToken) return null;
  try {
    const result = await invoke("auth_refresh_token", { refreshToken });
    if (result.token) {
      await setAuthToken(result.token);
      if (result.refresh_token) {
        localStorage.setItem("refresh_token", result.refresh_token);
      }
      if (result.user_id) {
        setUserInfo(result);
      }
      return result;
    }
    return null;
  } catch {
    return null;
  }
}

export function clearAuth() {
  localStorage.removeItem("auth_token");
  localStorage.removeItem("user_info");
  localStorage.removeItem("refresh_token");
}

export function getUserInfo() {
  try {
    return JSON.parse(localStorage.getItem("user_info") || "null");
  } catch {
    return null;
  }
}

export function setUserInfo(info) {
  localStorage.setItem("user_info", JSON.stringify(info));
}

// ─── API Key Status (read-only, operator sets via Railway env vars) ───
export async function getApiKeyStatus() {
  return await invoke("get_api_key_status");
}

export async function getUpstageApiKeyStatus() {
  return await invoke("get_upstage_api_key_status");
}

// ─── Exchange Rate ────────────────────────────────────
export async function getExchangeRate() {
  return await invoke("get_exchange_rate", {});
}

// ─── Credits ──────────────────────────────────────────
export async function getCredits() {
  return await invoke("get_credits", {});
}

export async function purchaseCredits(amountUsd) {
  return await invoke("purchase_credits", { amountUsd });
}

export async function estimateCost(numPages, docType) {
  return await invoke("estimate_cost", { numPages, docType: docType || "image_pdf" });
}

export async function getPricing() {
  return await invoke("get_pricing");
}

export async function getCreditHistory() {
  return await invoke("get_credit_history");
}

export async function createCheckout(amountUsd) {
  return await invoke("create_checkout", { amountUsd });
}

// ─── R2 Upload (Image PDF Hybrid Architecture) ──────
export async function getR2Status() {
  return await invoke("r2_status");
}

export async function getR2PresignedUpload(filename, contentType) {
  return await invoke("r2_presigned_upload", {
    filename,
    contentType: contentType || "application/pdf",
  });
}

export async function parseImagePdfFromR2(objectKey, outputFormats, upstageMode) {
  return await invoke("parse_image_pdf", {
    objectKey,
    outputFormats: outputFormats || ["html", "markdown"],
    upstageMode: upstageMode || "auto",
  });
}

// ─── Local LLM Correction ────────────────────────────
export async function correctWithLLM(html, provider, apiKey, model, sourceType) {
  return await invoke("correct_with_llm", {
    html,
    provider,
    apiKey,
    model: model || "",
    sourceType: sourceType || "image_pdf",
  });
}

// ─── User LLM API Key Management (stored locally) ───
export function getUserLLMConfig() {
  try {
    return JSON.parse(localStorage.getItem("user_llm_config") || "null") || {
      provider: "",
      api_key: "",
      model: "",
    };
  } catch {
    return { provider: "", api_key: "", model: "" };
  }
}

export function setUserLLMConfig(config) {
  localStorage.setItem("user_llm_config", JSON.stringify(config));
}

export function clearUserLLMConfig() {
  localStorage.removeItem("user_llm_config");
}

// ─── WebSocket Progress ──────────────────────────────
export async function connectProgress(jobId, onMessage) {
  try {
    const baseUrl = await getBackendUrl();
    const wsUrl = baseUrl.replace("http://", "ws://") + `/ws/progress/${jobId}`;
    const ws = new WebSocket(wsUrl);

    return new Promise((resolve) => {
      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          onMessage(data);
          // Auto-close on terminal states
          if (data.status === "completed" || data.status === "failed") {
            ws.close();
          }
        } catch {
          onMessage({ message: event.data });
        }
      };

      ws.onclose = () => {
        onMessage({ _wsClose: true });
      };

      ws.onopen = () => resolve(ws);
      ws.onerror = (err) => {
        console.warn("WebSocket connection failed for job", jobId, err);
        resolve(null);
      };
    });
  } catch (e) {
    console.warn("Failed to connect WebSocket:", e);
    return null;
  }
}

// ─── HTML to Markdown ─────────────────────────────────
export async function htmlToMarkdown(html) {
  return await invoke("html_to_markdown", { html });
}

// ─── Editor Window ────────────────────────────────────
export async function openEditorWindow(filePath, viewerPath, mdPath) {
  return await invoke("open_editor_window", {
    filePath: filePath || null,
    viewerPath: viewerPath || null,
    mdPath: mdPath || null,
  });
}

// ─── Utility ─────────────────────────────────────────
export function getFileExtension(path) {
  return (path || "").split(".").pop().toLowerCase();
}

export function isRustNativeFormat(ext) {
  return ["docx", "hwpx", "xlsx", "pptx"].includes(ext);
}

export function isHancomFormat(ext) {
  return ["hwp", "hwpx", "doc", "docx", "xls", "xlsx", "ppt", "pptx"].includes(ext);
}

export function isPdfFormat(ext) {
  return ext === "pdf";
}

export function isSupportedFormat(ext) {
  return ["pdf", "hwp", "hwpx", "doc", "docx", "xls", "xlsx", "ppt", "pptx"].includes(ext);
}
