/**
 * Tauri API Bridge - Backend communication layer
 * Replaces Electron preload.js with Tauri invoke calls
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
export async function selectFile(filters) {
  const result = await openDialog({
    multiple: false,
    filters: filters || [
      { name: "PDF 파일", extensions: ["pdf"] },
      { name: "문서 파일", extensions: ["pdf", "docx", "hwpx", "xlsx", "pptx"] },
      { name: "모든 파일", extensions: ["*"] },
    ],
  });
  return result; // string path or null
}

export async function selectDocumentFile() {
  return await openDialog({
    multiple: false,
    filters: [
      {
        name: "지원 문서",
        extensions: ["pdf", "docx", "hwpx", "xlsx", "pptx"],
      },
      { name: "PDF", extensions: ["pdf"] },
      { name: "Word", extensions: ["docx"] },
      { name: "한글", extensions: ["hwpx"] },
      { name: "Excel", extensions: ["xlsx"] },
      { name: "PowerPoint", extensions: ["pptx"] },
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
  await shellOpen(path);
}

export async function openFile(path) {
  await shellOpen(path);
}

// ─── PDF Conversion (Python Backend) ──────────────────
export async function convertPdf(inputPath, outputDir, formats) {
  return await invoke("convert_pdf", {
    request: {
      input_path: inputPath,
      output_dir: outputDir || null,
      formats: formats || ["html", "markdown"],
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

// ─── Document Conversion (Rust Native) ────────────────
export async function convertDocument(inputPath, outputDir, formats) {
  return await invoke("convert_any_document", {
    inputPath,
    outputDir: outputDir || null,
    formats: formats || ["html", "markdown"],
  });
}

export async function convertAny(inputPath, outputDir, formats) {
  return await invoke("convert_document", {
    inputPath,
    outputDir: outputDir || null,
    formats: formats || ["html", "markdown"],
  });
}

// ─── Job Management ───────────────────────────────────
export async function getJobStatus(jobId) {
  return await invoke("get_job_status", { jobId });
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

// ─── WebSocket Progress ──────────────────────────────
export function connectProgress(jobId, onMessage) {
  return new Promise((resolve) => {
    getBackendUrl().then((baseUrl) => {
      const wsUrl = baseUrl.replace("http://", "ws://") + `/ws/progress/${jobId}`;
      const ws = new WebSocket(wsUrl);

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          onMessage(data);
        } catch {
          onMessage({ message: event.data });
        }
      };

      ws.onopen = () => resolve(ws);
      ws.onerror = () => resolve(null);
    });
  });
}

// ─── Utility ─────────────────────────────────────────
export function getFileExtension(path) {
  return path.split(".").pop().toLowerCase();
}

export function isRustNativeFormat(ext) {
  return ["docx", "hwpx", "xlsx", "pptx"].includes(ext);
}

export function isPdfFormat(ext) {
  return ext === "pdf";
}

export function isSupportedFormat(ext) {
  return ["pdf", "docx", "hwpx", "xlsx", "pptx"].includes(ext);
}
