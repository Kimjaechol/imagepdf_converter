use crate::backend::process;
use tauri::Manager;

/// Railway remote backend URL for auth/credits/payments (has Supabase keys).
pub const REMOTE_BACKEND_URL: &str = "https://imagepdfconverter-production.up.railway.app";

/// Local Python backend URL for document conversion (needs local file access).
pub fn backend_url() -> String {
    format!("http://127.0.0.1:{}", process::get_port())
}

/// Remote backend URL for auth, credits, payments, etc.
pub fn remote_url() -> &'static str {
    REMOTE_BACKEND_URL
}

pub fn auth_header(app: &tauri::AppHandle) -> String {
    let state = app.state::<crate::AuthToken>();
    let token = state
        .0
        .lock()
        .unwrap_or_else(|e: std::sync::PoisonError<std::sync::MutexGuard<'_, String>>| {
            e.into_inner()
        });
    format!("Bearer {}", token)
}

/// Check HTTP response status and return a descriptive error if not successful.
pub async fn check_response(
    resp: reqwest::Response,
    context: &str,
) -> Result<reqwest::Response, String> {
    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(format!("{} failed ({}): {}", context, status, body));
    }
    Ok(resp)
}
