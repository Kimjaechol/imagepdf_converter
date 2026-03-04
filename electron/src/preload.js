const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("electronAPI", {
  selectFile: () => ipcRenderer.invoke("select-file"),
  selectFolder: () => ipcRenderer.invoke("select-folder"),
  selectOutputDir: () => ipcRenderer.invoke("select-output-dir"),
  getBackendUrl: () => ipcRenderer.invoke("get-backend-url"),
  openFolder: (path) => ipcRenderer.invoke("open-folder", path),
  openFile: (path) => ipcRenderer.invoke("open-file", path),
});
