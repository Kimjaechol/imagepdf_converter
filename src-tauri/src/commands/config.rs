use crate::backend::process;
use serde::{Deserialize, Serialize};

fn backend_url() -> String {
    format!("http://127.0.0.1:{}", process::get_port())
}

#[derive(Serialize, Deserialize)]
pub struct ConfigUpdate {
    pub key: String,
    pub value: serde_json::Value,
}

#[derive(Serialize, Deserialize)]
pub struct DictTerm {
    pub wrong: String,
    pub correct: String,
    pub category: Option<String>,
}

#[tauri::command]
pub async fn get_config() -> Result<serde_json::Value, String> {
    let resp = reqwest::get(format!("{}/api/config", backend_url()))
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}

#[tauri::command]
pub async fn update_config(update: ConfigUpdate) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{}/api/config", backend_url()))
        .json(&update)
        .send()
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}

#[tauri::command]
pub async fn add_dictionary_term(term: DictTerm) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{}/api/dictionary/add", backend_url()))
        .json(&term)
        .send()
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Parse failed: {}", e))
}
