use super::common::{auth_header, backend_url, check_response};

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

    let resp = check_response(resp, "Registration").await?;

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

    let resp = check_response(resp, "Login").await?;

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

    let resp = check_response(resp, "Get user info").await?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}

#[tauri::command]
pub async fn set_auth_token(app: tauri::AppHandle, token: String) -> Result<(), String> {
    let state = app.state::<crate::AuthToken>();
    let mut t = state.0.lock().unwrap_or_else(|e: std::sync::PoisonError<std::sync::MutexGuard<'_, String>>| e.into_inner());
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

    let resp = check_response(resp, "Set API key").await?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}

#[tauri::command]
pub async fn get_api_key_status() -> Result<serde_json::Value, String> {
    let resp = reqwest::get(format!("{}/api/settings/api-key/status", backend_url()))
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    let resp = check_response(resp, "Get API key status").await?;

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

    let resp = check_response(resp, "Set Upstage API key").await?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}

#[tauri::command]
pub async fn get_upstage_api_key_status() -> Result<serde_json::Value, String> {
    let resp = reqwest::get(format!("{}/api/settings/upstage-api-key/status", backend_url()))
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    let resp = check_response(resp, "Get Upstage API key status").await?;

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

    let resp = check_response(resp, "Get credits").await?;

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

    let resp = check_response(resp, "Purchase credits").await?;

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

    let resp = check_response(resp, "Estimate cost").await?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}

#[tauri::command]
pub async fn get_pricing() -> Result<serde_json::Value, String> {
    let resp = reqwest::get(format!("{}/api/credits/pricing", backend_url()))
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    let resp = check_response(resp, "Get pricing").await?;

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

    let resp = check_response(resp, "Get credit history").await?;

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

    let resp = check_response(resp, "Create checkout").await?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}
