use super::common::{auth_header, backend_url, check_response};
use tauri::Manager;

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

#[tauri::command]
pub async fn auth_refresh_token(
    app: tauri::AppHandle,
    refresh_token: String,
) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{}/api/auth/refresh", backend_url()))
        .json(&serde_json::json!({
            "refresh_token": refresh_token,
        }))
        .send()
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    let resp = check_response(resp, "Token refresh").await?;

    let result: serde_json::Value = resp
        .json()
        .await
        .map_err(|e| format!("Parse failed: {}", e))?;

    // Auto-update the Rust-side auth token if refresh succeeded
    if let Some(new_token) = result.get("token").and_then(|t| t.as_str()) {
        let state = app.state::<crate::AuthToken>();
        let mut t = state.0.lock().unwrap_or_else(|e: std::sync::PoisonError<std::sync::MutexGuard<'_, String>>| e.into_inner());
        *t = new_token.to_string();
    }

    Ok(result)
}

// ─── API Key Status (read-only, operator sets via Railway env vars) ───

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
pub async fn get_upstage_api_key_status() -> Result<serde_json::Value, String> {
    let resp = reqwest::get(format!("{}/api/settings/upstage-api-key/status", backend_url()))
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    let resp = check_response(resp, "Get Upstage API key status").await?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}

// ─── R2 Upload (Image PDF Hybrid Architecture) ───────

#[tauri::command]
pub async fn r2_status() -> Result<serde_json::Value, String> {
    let resp = reqwest::get(format!("{}/api/r2/status", backend_url()))
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    let resp = check_response(resp, "R2 status").await?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}

#[tauri::command]
pub async fn r2_presigned_upload(
    app: tauri::AppHandle,
    filename: String,
    content_type: String,
) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{}/api/r2/presigned-upload", backend_url()))
        .header("Authorization", auth_header(&app))
        .json(&serde_json::json!({
            "filename": filename,
            "content_type": content_type,
        }))
        .send()
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    let resp = check_response(resp, "R2 presigned upload").await?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}

#[tauri::command]
pub async fn parse_image_pdf(
    app: tauri::AppHandle,
    object_key: String,
    output_formats: Vec<String>,
    upstage_mode: String,
) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{}/api/parse/image-pdf", backend_url()))
        .header("Authorization", auth_header(&app))
        .json(&serde_json::json!({
            "object_key": object_key,
            "output_formats": output_formats,
            "upstage_mode": upstage_mode,
        }))
        .timeout(std::time::Duration::from_secs(600))
        .send()
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    let resp = check_response(resp, "Image PDF parse").await?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}

// ─── Local LLM Correction ────────────────────────────

#[tauri::command]
pub async fn correct_with_llm(
    html: String,
    provider: String,
    api_key: String,
    model: String,
    source_type: String,
) -> Result<serde_json::Value, String> {
    // This calls the LOCAL Python backend (user's machine).
    // The API key goes from user's machine directly to the LLM provider.
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{}/api/correct/llm", backend_url()))
        .json(&serde_json::json!({
            "html": html,
            "provider": provider,
            "api_key": api_key,
            "model": model,
            "source_type": source_type,
        }))
        .timeout(std::time::Duration::from_secs(300))
        .send()
        .await
        .map_err(|e| format!("LLM correction request failed: {}", e))?;

    let resp = check_response(resp, "LLM correction").await?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}

// ─── Exchange Rate ────────────────────────────────────

#[tauri::command]
pub async fn get_exchange_rate() -> Result<serde_json::Value, String> {
    let resp = reqwest::get(format!("{}/api/exchange-rate", backend_url()))
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    let resp = check_response(resp, "Get exchange rate").await?;

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
