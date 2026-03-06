use crate::backend::process;

fn backend_url() -> String {
    format!("http://127.0.0.1:{}", process::get_port())
}

// ─── API Key ────────────────────────────────────────────

#[tauri::command]
pub async fn set_api_key(api_key: String) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{}/api/settings/api-key", backend_url()))
        .json(&serde_json::json!({ "api_key": api_key }))
        .send()
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}

#[tauri::command]
pub async fn get_api_key_status() -> Result<serde_json::Value, String> {
    let resp = reqwest::get(format!("{}/api/settings/api-key/status", backend_url()))
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}

// ─── Credits ────────────────────────────────────────────

#[tauri::command]
pub async fn get_credits(user_id: String) -> Result<serde_json::Value, String> {
    let resp = reqwest::get(format!("{}/api/credits/{}", backend_url(), user_id))
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}

#[tauri::command]
pub async fn purchase_credits(
    user_id: String,
    amount_usd: f64,
) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{}/api/credits/purchase", backend_url()))
        .json(&serde_json::json!({
            "user_id": user_id,
            "amount_usd": amount_usd,
        }))
        .send()
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}

#[tauri::command]
pub async fn estimate_cost(num_pages: u32) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{}/api/credits/estimate", backend_url()))
        .json(&serde_json::json!({ "num_pages": num_pages }))
        .send()
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}

#[tauri::command]
pub async fn get_credit_history(user_id: String) -> Result<serde_json::Value, String> {
    let resp = reqwest::get(format!(
        "{}/api/credits/{}/history",
        backend_url(),
        user_id
    ))
    .await
    .map_err(|e| format!("Request failed: {}", e))?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}
