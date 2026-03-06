use crate::backend::process;
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize, Clone)]
pub struct ConvertRequest {
    pub input_path: String,
    pub output_dir: Option<String>,
    #[serde(alias = "formats")]
    pub output_formats: Option<Vec<String>>,
    #[serde(default)]
    pub translate: bool,
    #[serde(default)]
    pub source_language: String,
    #[serde(default = "default_target_language")]
    pub target_language: String,
}

fn default_target_language() -> String {
    "ko".to_string()
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
    // Build the JSON payload with field names matching the Python backend
    let input_path = &request.input_path;
    let output_dir = request.output_dir.clone().unwrap_or_else(|| {
        std::path::Path::new(input_path)
            .parent()
            .unwrap_or(std::path::Path::new("."))
            .to_string_lossy()
            .to_string()
    });
    let output_formats = request.output_formats.clone()
        .unwrap_or_else(|| vec!["html".to_string(), "markdown".to_string()]);

    let payload = serde_json::json!({
        "input_path": input_path,
        "output_dir": output_dir,
        "output_formats": output_formats,
        "translate": request.translate,
        "source_language": request.source_language,
        "target_language": request.target_language,
    });

    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{}/api/convert", backend_url()))
        .json(&payload)
        .send()
        .await
        .map_err(|e| format!("Backend request failed: {}", e))?;

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Failed to parse response: {}", e))
}

#[tauri::command]
pub async fn convert_batch(request: BatchRequest) -> Result<serde_json::Value, String> {
    let output_dir = request.output_dir.clone().unwrap_or_else(|| {
        request.folder_path.clone()
    });
    let output_formats = request.formats.clone()
        .unwrap_or_else(|| vec!["html".to_string(), "markdown".to_string()]);

    let payload = serde_json::json!({
        "folder_path": request.folder_path,
        "output_dir": output_dir,
        "output_formats": output_formats,
        "recursive": request.recursive.unwrap_or(false),
    });

    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{}/api/convert/batch", backend_url()))
        .json(&payload)
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
    translate: Option<bool>,
    source_language: Option<String>,
    target_language: Option<String>,
) -> Result<serde_json::Value, String> {
    let ext = std::path::Path::new(&input_path)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_lowercase();

    let do_translate = translate.unwrap_or(false);
    let src_lang = source_language.unwrap_or_default();
    let tgt_lang = target_language.unwrap_or_else(|| "ko".to_string());

    match ext.as_str() {
        // Rust-native converters
        "docx" | "hwpx" | "xlsx" | "pptx" => {
            let output_formats = formats.unwrap_or_else(|| vec!["html".to_string(), "markdown".to_string()]);
            let out_dir = output_dir.unwrap_or_else(|| {
                std::path::Path::new(&input_path)
                    .parent()
                    .unwrap_or(std::path::Path::new("."))
                    .to_string_lossy()
                    .to_string()
            });

            let mut result =
                crate::document::converter::convert_file(&input_path, &out_dir, &output_formats)
                    .await?;

            // If translation is requested, send HTML to Python backend for Gemini translation
            if do_translate {
                if let Some(ref html) = result.html {
                    let translated = translate_html_via_backend(
                        html, &src_lang, &tgt_lang,
                    ).await?;

                    // Save translated HTML
                    let stem = std::path::Path::new(&input_path)
                        .file_stem()
                        .and_then(|s| s.to_str())
                        .unwrap_or("output");
                    let translated_html_path = std::path::Path::new(&out_dir)
                        .join(format!("{}_translated.html", stem));
                    tokio::fs::write(&translated_html_path, &translated)
                        .await
                        .map_err(|e| format!("Cannot write translated HTML: {}", e))?;

                    // Also produce translated markdown if requested
                    let want_md = output_formats.iter().any(|f| f == "markdown" || f == "md");
                    if want_md {
                        let md = crate::document::converter::html_to_markdown_public(&translated);
                        let md_path = std::path::Path::new(&out_dir)
                            .join(format!("{}_translated.md", stem));
                        tokio::fs::write(&md_path, &md)
                            .await
                            .map_err(|e| format!("Cannot write translated markdown: {}", e))?;
                        result.output_files.push(md_path.to_string_lossy().to_string());
                    }

                    result.html = Some(translated);
                    result.output_files.push(translated_html_path.to_string_lossy().to_string());
                }
            }

            serde_json::to_value(result)
                .map_err(|e| format!("Failed to serialize result: {}", e))
        }
        // PDF → Python backend
        "pdf" => {
            convert_pdf(ConvertRequest {
                input_path,
                output_dir,
                output_formats: formats,
                translate: do_translate,
                source_language: src_lang,
                target_language: tgt_lang,
            })
            .await
        }
        _ => Err(format!("Unsupported file format: .{}", ext)),
    }
}

/// Call the Python backend's /api/translate-html endpoint
async fn translate_html_via_backend(
    html: &str,
    source_language: &str,
    target_language: &str,
) -> Result<String, String> {
    let client = reqwest::Client::new();

    #[derive(Serialize)]
    struct TranslateRequest<'a> {
        html: &'a str,
        source_language: &'a str,
        target_language: &'a str,
    }

    let resp = client
        .post(format!("{}/api/translate-html", backend_url()))
        .json(&TranslateRequest {
            html,
            source_language,
            target_language,
        })
        .timeout(std::time::Duration::from_secs(300))
        .send()
        .await
        .map_err(|e| format!("Translation request failed: {}", e))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(format!("Translation failed ({}): {}", status, body));
    }

    #[derive(Deserialize)]
    struct TranslateResponse {
        translated_html: String,
    }

    let result: TranslateResponse = resp
        .json()
        .await
        .map_err(|e| format!("Failed to parse translation response: {}", e))?;

    Ok(result.translated_html)
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
