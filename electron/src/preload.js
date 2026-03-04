const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("electronAPI", {
  selectFile: () => ipcRenderer.invoke("select-file"),
  selectFolder: () => ipcRenderer.invoke("select-folder"),
  selectOutputDir: () => ipcRenderer.invoke("select-output-dir"),
  selectHtmlFile: () => ipcRenderer.invoke("select-html-file"),
  getBackendUrl: () => ipcRenderer.invoke("get-backend-url"),
  openFolder: (path) => ipcRenderer.invoke("open-folder", path),
  openFile: (path) => ipcRenderer.invoke("open-file", path),
  openEditor: (filePath) => ipcRenderer.invoke("open-editor", filePath),
  readFile: (path) => ipcRenderer.invoke("read-file", path),
  saveFile: (path, content) => ipcRenderer.invoke("save-file", path, content),
  saveFileDialog: (defaultName, filters) => ipcRenderer.invoke("save-file-dialog", defaultName, filters),
});
