use crate::backend::process;

#[tauri::command]
pub async fn restart_backend(app: tauri::AppHandle) -> Result<String, String> {
    process::restart_backend(&app).await?;
    Ok("Backend restarted successfully".to_string())
}

#[tauri::command]
pub async fn backend_health() -> Result<serde_json::Value, String> {
    let healthy = process::health_check().await;
    let port = process::get_port();
    Ok(serde_json::json!({
        "healthy": healthy,
        "port": port,
        "url": format!("http://127.0.0.1:{}", port),
    }))
}
