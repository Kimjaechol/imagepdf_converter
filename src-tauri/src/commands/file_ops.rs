use crate::backend::process;
use tauri::Manager;

#[tauri::command]
pub fn get_backend_url() -> String {
    let port = process::get_port();
    format!("http://127.0.0.1:{}", port)
}

#[tauri::command]
pub async fn read_file_content(path: String) -> Result<String, String> {
    tokio::fs::read_to_string(&path)
        .await
        .map_err(|e| format!("Failed to read file: {}", e))
}

#[tauri::command]
pub async fn write_file_content(path: String, content: String) -> Result<bool, String> {
    tokio::fs::write(&path, &content)
        .await
        .map(|_| true)
        .map_err(|e| format!("Failed to write file: {}", e))
}

/// Open a file or folder with the system's default application.
/// This bypasses Tauri's shell:open URL restrictions by using the OS directly.
#[tauri::command]
pub fn open_path_native(path: String) -> Result<bool, String> {
    let p = std::path::Path::new(&path);
    if !p.exists() {
        return Err(format!("파일/폴더를 찾을 수 없습니다: {}", path));
    }
    open::that(&path).map(|_| true).map_err(|e| format!("열기 실패: {}", e))
}

/// Open the HTML editor in a new Tauri webview window.
#[tauri::command]
pub async fn open_editor_window(
    app: tauri::AppHandle,
    file_path: Option<String>,
) -> Result<bool, String> {
    use tauri::{WebviewUrl, WebviewWindowBuilder};

    let url = if let Some(ref fp) = file_path {
        // Properly percent-encode the file path (encode each UTF-8 byte individually)
        let encoded: String = fp
            .as_bytes()
            .iter()
            .map(|&b| {
                if b.is_ascii_alphanumeric() || b"-_.~/\\:".contains(&b) {
                    String::from(b as char)
                } else {
                    format!("%{:02X}", b)
                }
            })
            .collect();
        WebviewUrl::App(format!("editor.html?file={}", encoded).into())
    } else {
        WebviewUrl::App("editor.html".into())
    };

    // If the editor window already exists, close it first so it reopens with the new file
    if let Some(win) = app.get_webview_window("editor") {
        let _ = win.destroy();
    }

    WebviewWindowBuilder::new(&app, "editor", url)
        .title("HTML 에디터 - MoA 문서 변환기")
        .inner_size(1100.0, 750.0)
        .min_inner_size(800.0, 500.0)
        .center()
        .build()
        .map(|_| true)
        .map_err(|e| format!("에디터 창 열기 실패: {}", e))
}
