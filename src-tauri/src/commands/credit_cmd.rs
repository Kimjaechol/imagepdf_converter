use crate::backend::process;

fn backend_url() -> String {
    format!("http://127.0.0.1:{}", process::get_port())
}

/// Read the auth token stored by the frontend (passed from localStorage via Tauri state).
fn auth_header(app: &tauri::AppHandle) -> String {
    let state = app.state::<crate::AuthToken>();
    let token = state.0.lock().unwrap();
    format!("Bearer {}", token)
}

// ─── Auth ──────────────────────────────────────────────

#[tauri::command]
pub async fn auth_register(
    email: String,
    password: String,
    display_name: String,
) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{}/api/auth/register", backend_url()))
        .json(&serde_json::json!({
            "email": email,
            "password": password,
            "display_name": display_name,
        }))
        .send()
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(format!("Registration failed ({}): {}", status, body));
    }

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}

#[tauri::command]
pub async fn auth_login(email: String, password: String) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{}/api/auth/login", backend_url()))
        .json(&serde_json::json!({
            "email": email,
            "password": password,
        }))
        .send()
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(format!("Login failed ({}): {}", status, body));
    }

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}

#[tauri::command]
pub async fn auth_get_me(app: tauri::AppHandle) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let resp = client
        .get(format!("{}/api/auth/me", backend_url()))
        .header("Authorization", auth_header(&app))
        .send()
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}

#[tauri::command]
pub async fn set_auth_token(app: tauri::AppHandle, token: String) -> Result<(), String> {
    let state = app.state::<crate::AuthToken>();
    let mut t = state.0.lock().unwrap();
    *t = token;
    Ok(())
}

// ─── API Key (operator only) ──────────────────────────

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

#[tauri::command]
pub async fn set_upstage_api_key(api_key: String) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{}/api/settings/upstage-api-key", backend_url()))
        .json(&serde_json::json!({ "api_key": api_key }))
        .send()
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}

#[tauri::command]
pub async fn get_upstage_api_key_status() -> Result<serde_json::Value, String> {
    let resp = reqwest::get(format!("{}/api/settings/upstage-api-key/status", backend_url()))
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}

// ─── Credits (authenticated) ──────────────────────────

#[tauri::command]
pub async fn get_credits(app: tauri::AppHandle) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let resp = client
        .get(format!("{}/api/credits", backend_url()))
        .header("Authorization", auth_header(&app))
        .send()
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}

#[tauri::command]
pub async fn purchase_credits(
    app: tauri::AppHandle,
    amount_usd: f64,
) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{}/api/credits/purchase", backend_url()))
        .header("Authorization", auth_header(&app))
        .json(&serde_json::json!({
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
pub async fn estimate_cost(
    num_pages: u32,
    doc_type: String,
) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{}/api/credits/estimate", backend_url()))
        .json(&serde_json::json!({
            "num_pages": num_pages,
            "doc_type": doc_type,
        }))
        .send()
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}

#[tauri::command]
pub async fn get_pricing() -> Result<serde_json::Value, String> {
    let resp = reqwest::get(format!("{}/api/credits/pricing", backend_url()))
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}

#[tauri::command]
pub async fn get_credit_history(app: tauri::AppHandle) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let resp = client
        .get(format!("{}/api/credits/history", backend_url()))
        .header("Authorization", auth_header(&app))
        .send()
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}

#[tauri::command]
pub async fn create_checkout(
    app: tauri::AppHandle,
    amount_usd: f64,
) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{}/api/payments/create-checkout", backend_url()))
        .header("Authorization", auth_header(&app))
        .json(&serde_json::json!({
            "amount_usd": amount_usd,
        }))
        .send()
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}
