const { app, BrowserWindow, ipcMain, dialog, shell } = require("electron");
const path = require("path");
const fs = require("fs");
const { spawn, execSync } = require("child_process");
const http = require("http");

let mainWindow;
let backendProcess;
const BACKEND_PORT = 8765;
const BACKEND_URL = `http://127.0.0.1:${BACKEND_PORT}`;

// -------------------------------------------------------------------
// Find Python - bundled portable or system-installed
// -------------------------------------------------------------------

function findPython() {
  // 1. Bundled portable Python (packaged app)
  if (app.isPackaged) {
    const bundledPython = path.join(
      process.resourcesPath,
      "portable_python",
      "python.exe"
    );
    if (fs.existsSync(bundledPython)) {
      console.log("Using bundled Python:", bundledPython);
      return bundledPython;
    }
  }

  // 2. Portable Python next to the app (development or manual setup)
  const devPortable = path.join(__dirname, "..", "..", "build_output", "portable_python", "python.exe");
  if (fs.existsSync(devPortable)) {
    console.log("Using dev portable Python:", devPortable);
    return devPortable;
  }

  // 3. System Python
  const systemCmd = process.platform === "win32" ? "python" : "python3";
  try {
    execSync(`${systemCmd} --version`, { stdio: "pipe" });
    console.log("Using system Python:", systemCmd);
    return systemCmd;
  } catch {
    // 4. Last resort on Windows - try common install paths
    if (process.platform === "win32") {
      const commonPaths = [
        path.join(process.env.LOCALAPPDATA || "", "Programs", "Python", "Python312", "python.exe"),
        path.join(process.env.LOCALAPPDATA || "", "Programs", "Python", "Python311", "python.exe"),
        path.join(process.env.LOCALAPPDATA || "", "Programs", "Python", "Python310", "python.exe"),
        "C:\\Python312\\python.exe",
        "C:\\Python311\\python.exe",
      ];
      for (const p of commonPaths) {
        if (fs.existsSync(p)) {
          console.log("Found Python at:", p);
          return p;
        }
      }
    }
  }

  console.error("Python not found!");
  return systemCmd; // will fail gracefully
}

// -------------------------------------------------------------------
// Backend lifecycle
// -------------------------------------------------------------------

function startBackend() {
  const pythonCmd = findPython();

  const backendDir = app.isPackaged
    ? path.join(process.resourcesPath, "app_backend")
    : path.join(__dirname, "..", "..");

  const configPath = app.isPackaged
    ? path.join(process.resourcesPath, "app_backend", "config", "pipeline_config.yaml")
    : path.join(__dirname, "..", "..", "config", "pipeline_config.yaml");

  console.log("Starting backend from:", backendDir);
  console.log("Config path:", configPath);

  backendProcess = spawn(
    pythonCmd,
    ["-m", "uvicorn", "backend.server:app", "--host", "127.0.0.1", "--port", String(BACKEND_PORT)],
    {
      cwd: backendDir,
      env: {
        ...process.env,
        PIPELINE_CONFIG: configPath,
        PORT: String(BACKEND_PORT),
        PYTHONDONTWRITEBYTECODE: "1",
      },
      stdio: ["pipe", "pipe", "pipe"],
    }
  );

  backendProcess.stdout.on("data", (data) => {
    const msg = data.toString().trim();
    console.log(`[backend] ${msg}`);
    // Notify renderer of backend logs
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send("backend-log", msg);
    }
  });

  backendProcess.stderr.on("data", (data) => {
    const msg = data.toString().trim();
    console.error(`[backend] ${msg}`);
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send("backend-log", msg);
    }
  });

  backendProcess.on("error", (err) => {
    console.error("Failed to start backend:", err);
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send("backend-error", err.message);
    }
  });

  backendProcess.on("exit", (code) => {
    console.log(`Backend exited with code ${code}`);
    backendProcess = null;
  });
}

function stopBackend() {
  if (backendProcess) {
    if (process.platform === "win32") {
      // On Windows, kill the process tree
      try {
        execSync(`taskkill /pid ${backendProcess.pid} /T /F`, { stdio: "pipe" });
      } catch {
        backendProcess.kill();
      }
    } else {
      backendProcess.kill();
    }
    backendProcess = null;
  }
}

function waitForBackend(maxRetries = 60) {
  return new Promise((resolve, reject) => {
    let retries = 0;
    const check = () => {
      http
        .get(`${BACKEND_URL}/api/health`, (res) => {
          if (res.statusCode === 200) {
            resolve();
          } else {
            retry();
          }
        })
        .on("error", () => {
          retry();
        });
    };
    const retry = () => {
      retries++;
      if (retries >= maxRetries) {
        reject(new Error("Backend failed to start"));
      } else {
        setTimeout(check, 1000);
      }
    };
    check();
  });
}

// -------------------------------------------------------------------
// Window
// -------------------------------------------------------------------

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: 900,
    minHeight: 600,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
    title: "PDF 변환기",
    icon: path.join(__dirname, "..", "public", "icon.png"),
  });

  mainWindow.loadFile(path.join(__dirname, "..", "public", "index.html"));

  if (process.env.NODE_ENV === "development") {
    mainWindow.webContents.openDevTools();
  }
}

// -------------------------------------------------------------------
// IPC handlers
// -------------------------------------------------------------------

ipcMain.handle("select-file", async () => {
  const result = await dialog.showOpenDialog(mainWindow, {
    properties: ["openFile"],
    filters: [
      { name: "지원 문서", extensions: ["pdf", "hwp", "hwpx", "doc", "docx", "xls", "xlsx", "ppt", "pptx"] },
      { name: "PDF", extensions: ["pdf"] },
      { name: "한글", extensions: ["hwp", "hwpx"] },
      { name: "Word", extensions: ["doc", "docx"] },
      { name: "Excel", extensions: ["xls", "xlsx"] },
      { name: "PowerPoint", extensions: ["ppt", "pptx"] },
    ],
  });
  return result.canceled ? null : result.filePaths[0];
});

ipcMain.handle("select-folder", async () => {
  const result = await dialog.showOpenDialog(mainWindow, {
    properties: ["openDirectory"],
  });
  return result.canceled ? null : result.filePaths[0];
});

ipcMain.handle("select-output-dir", async () => {
  const result = await dialog.showOpenDialog(mainWindow, {
    properties: ["openDirectory", "createDirectory"],
  });
  return result.canceled ? null : result.filePaths[0];
});

ipcMain.handle("get-backend-url", () => BACKEND_URL);

ipcMain.handle("open-folder", async (_event, folderPath) => {
  shell.openPath(folderPath);
});

ipcMain.handle("open-file", async (_event, filePath) => {
  shell.openPath(filePath);
});

ipcMain.handle("read-file", async (_event, filePath) => {
  try {
    return fs.readFileSync(filePath, "utf-8");
  } catch {
    return null;
  }
});

ipcMain.handle("save-file", async (_event, filePath, content) => {
  try {
    fs.writeFileSync(filePath, content, "utf-8");
    return true;
  } catch {
    return false;
  }
});

ipcMain.handle("open-editor", async (_event, filePath) => {
  // If filePath is a directory, find the first .html file inside it
  let resolvedPath = filePath;
  if (filePath) {
    try {
      const stat = fs.statSync(filePath);
      if (stat.isDirectory()) {
        const files = fs.readdirSync(filePath);
        const htmlFile = files.find((f) => /\.html?$/i.test(f));
        if (htmlFile) {
          resolvedPath = path.join(filePath, htmlFile);
        } else {
          resolvedPath = null; // No HTML file found, open editor empty
        }
      }
    } catch {
      // File doesn't exist or can't be read, proceed with the path as-is
    }
  }

  const editorWindow = new BrowserWindow({
    width: 1100,
    height: 750,
    minWidth: 800,
    minHeight: 500,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
    title: "HTML 에디터 - PDF 변환기",
  });
  const query = resolvedPath ? `?file=${encodeURIComponent(resolvedPath)}` : "";
  editorWindow.loadFile(path.join(__dirname, "..", "public", "editor.html"), {
    search: query,
  });
});

ipcMain.handle("select-html-file", async () => {
  const result = await dialog.showOpenDialog(mainWindow, {
    properties: ["openFile"],
    filters: [
      { name: "HTML Files", extensions: ["html", "htm"] },
      { name: "Markdown Files", extensions: ["md"] },
      { name: "All Files", extensions: ["*"] },
    ],
  });
  return result.canceled ? null : result.filePaths[0];
});

ipcMain.handle("save-file-dialog", async (_event, defaultName, filters) => {
  const result = await dialog.showSaveDialog(mainWindow, {
    defaultPath: defaultName,
    filters: filters || [
      { name: "HTML Files", extensions: ["html"] },
      { name: "Markdown Files", extensions: ["md"] },
      { name: "All Files", extensions: ["*"] },
    ],
  });
  return result.canceled ? null : result.filePath;
});

// -------------------------------------------------------------------
// App lifecycle
// -------------------------------------------------------------------

app.whenReady().then(async () => {
  createWindow();
  startBackend();
  try {
    await waitForBackend();
    console.log("Backend is ready");
  } catch (err) {
    console.error("Backend startup failed:", err);
  }
});

app.on("window-all-closed", () => {
  stopBackend();
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    createWindow();
  }
});

app.on("before-quit", () => {
  stopBackend();
});
