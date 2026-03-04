use crate::backend::process;
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize, Clone)]
pub struct ConvertRequest {
    pub input_path: String,
    pub output_dir: Option<String>,
    pub formats: Option<Vec<String>>,
}

#[derive(Serialize, Deserialize, Clone)]
pub struct BatchRequest {
    pub folder_path: String,
    pub output_dir: Option<String>,
    pub formats: Option<Vec<String>>,
    pub recursive: Option<bool>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct JobStatus {
    pub job_id: String,
    pub status: String,
    pub progress: f64,
    pub message: String,
    #[serde(default)]
    pub result: Option<serde_json::Value>,
}

fn backend_url() -> String {
    let port = process::get_port();
    format!("http://127.0.0.1:{}", port)
}

#[tauri::command]
pub async fn convert_pdf(request: ConvertRequest) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{}/api/convert", backend_url()))
        .json(&request)
        .send()
        .await
        .map_err(|e| format!("Backend request failed: {}", e))?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Failed to parse response: {}", e))
}

#[tauri::command]
pub async fn convert_batch(request: BatchRequest) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{}/api/convert/batch", backend_url()))
        .json(&request)
        .send()
        .await
        .map_err(|e| format!("Backend request failed: {}", e))?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Failed to parse response: {}", e))
}

/// Unified document conversion - routes to Rust-native or Python backend
#[tauri::command]
pub async fn convert_document(
    input_path: String,
    output_dir: Option<String>,
    formats: Option<Vec<String>>,
) -> Result<serde_json::Value, String> {
    let ext = std::path::Path::new(&input_path)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_lowercase();

    match ext.as_str() {
        // Rust-native converters
        "docx" | "hwpx" | "xlsx" | "pptx" => {
            let output_formats = formats.unwrap_or_else(|| vec!["html".to_string()]);
            let out_dir = output_dir.unwrap_or_else(|| {
                std::path::Path::new(&input_path)
                    .parent()
                    .unwrap_or(std::path::Path::new("."))
                    .to_string_lossy()
                    .to_string()
            });

            let result =
                crate::document::converter::convert_file(&input_path, &out_dir, &output_formats)
                    .await?;
            Ok(serde_json::to_value(result).unwrap())
        }
        // PDF → Python backend
        "pdf" => {
            convert_pdf(ConvertRequest {
                input_path,
                output_dir,
                formats,
            })
            .await
        }
        _ => Err(format!("Unsupported file format: .{}", ext)),
    }
}

#[tauri::command]
pub async fn get_job_status(job_id: String) -> Result<JobStatus, String> {
    let resp = reqwest::get(format!("{}/api/jobs/{}", backend_url(), job_id))
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    resp.json::<JobStatus>()
        .await
        .map_err(|e| format!("Failed to parse: {}", e))
}

#[tauri::command]
pub async fn list_jobs() -> Result<serde_json::Value, String> {
    let resp = reqwest::get(format!("{}/api/jobs", backend_url()))
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Failed to parse: {}", e))
}
