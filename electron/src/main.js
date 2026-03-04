const { app, BrowserWindow, ipcMain, dialog, shell } = require("electron");
const path = require("path");
const { spawn } = require("child_process");
const http = require("http");

let mainWindow;
let backendProcess;
const BACKEND_PORT = 8765;
const BACKEND_URL = `http://127.0.0.1:${BACKEND_PORT}`;

// -------------------------------------------------------------------
// Backend lifecycle
// -------------------------------------------------------------------

function startBackend() {
  const pythonCmd = process.platform === "win32" ? "python" : "python3";
  const backendDir = app.isPackaged
    ? path.join(process.resourcesPath, "backend")
    : path.join(__dirname, "..", "..", "backend");

  backendProcess = spawn(pythonCmd, ["-m", "uvicorn", "backend.server:app", "--host", "127.0.0.1", "--port", String(BACKEND_PORT)], {
    cwd: path.join(backendDir, ".."),
    env: {
      ...process.env,
      PIPELINE_CONFIG: path.join(
        app.isPackaged ? process.resourcesPath : path.join(__dirname, "..", ".."),
        "config",
        "pipeline_config.yaml"
      ),
      PORT: String(BACKEND_PORT),
    },
    stdio: ["pipe", "pipe", "pipe"],
  });

  backendProcess.stdout.on("data", (data) => {
    console.log(`[backend] ${data.toString().trim()}`);
  });

  backendProcess.stderr.on("data", (data) => {
    console.error(`[backend] ${data.toString().trim()}`);
  });

  backendProcess.on("error", (err) => {
    console.error("Failed to start backend:", err);
  });

  backendProcess.on("exit", (code) => {
    console.log(`Backend exited with code ${code}`);
    backendProcess = null;
  });
}

function stopBackend() {
  if (backendProcess) {
    backendProcess.kill();
    backendProcess = null;
  }
}

function waitForBackend(maxRetries = 30) {
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
    title: "PDF → HTML/Markdown Converter",
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
    filters: [{ name: "PDF Files", extensions: ["pdf"] }],
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

// -------------------------------------------------------------------
// App lifecycle
// -------------------------------------------------------------------

app.whenReady().then(async () => {
  startBackend();
  try {
    await waitForBackend();
    console.log("Backend is ready");
  } catch (err) {
    console.error("Backend startup failed:", err);
  }
  createWindow();
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
