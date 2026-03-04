use crate::backend::process;

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
