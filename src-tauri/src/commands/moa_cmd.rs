use crate::document::converter;
use serde::{Deserialize, Serialize};

/// MoA Gateway Tool Interface
/// Designed to be called by MoA(zeroclaw) gateway as an AI document tool

#[derive(Serialize, Deserialize)]
pub struct MoaConvertRequest {
    /// Source document path
    pub source: String,
    /// Target format: "html" | "markdown" | "both"
    pub target_format: String,
    /// Optional output directory
    pub output_dir: Option<String>,
    /// Optional: specific pages for PDF (e.g., "1-5,10")
    pub pages: Option<String>,
    /// Optional: conversion quality "fast" | "balanced" | "quality"
    pub quality: Option<String>,
    /// Callback URL for async progress (optional)
    pub callback_url: Option<String>,
    /// MoA session/task ID for tracking
    pub task_id: Option<String>,
}

#[derive(Serialize, Deserialize)]
pub struct MoaConvertResponse {
    pub success: bool,
    pub task_id: Option<String>,
    pub source_format: String,
    pub target_format: String,
    pub html: Option<String>,
    pub markdown: Option<String>,
    pub output_files: Vec<String>,
    pub images: Vec<String>,
    pub metadata: MoaDocMetadata,
    pub error: Option<String>,
}

#[derive(Serialize, Deserialize)]
pub struct MoaDocMetadata {
    pub page_count: Option<u32>,
    pub title: Option<String>,
    pub author: Option<String>,
    pub file_size: u64,
    pub converter_version: String,
}

#[derive(Serialize, Deserialize)]
pub struct MoaSupportedFormat {
    pub extension: String,
    pub mime_type: String,
    pub description: String,
    pub engine: String, // "rust-native" or "python-pipeline"
}

/// MoA tool: convert any supported document
#[tauri::command]
pub async fn moa_convert(request: MoaConvertRequest) -> Result<MoaConvertResponse, String> {
    let path = std::path::Path::new(&request.source);

    let file_size = tokio::fs::metadata(&request.source)
        .await
        .map(|m| m.len())
        .unwrap_or(0);

    let ext = path
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_lowercase();

    let formats = match request.target_format.as_str() {
        "html" => vec!["html".to_string()],
        "markdown" | "md" => vec!["markdown".to_string()],
        _ => vec!["html".to_string(), "markdown".to_string()],
    };

    let out_dir = request.output_dir.unwrap_or_else(|| {
        path.parent()
            .unwrap_or(std::path::Path::new("."))
            .to_string_lossy()
            .to_string()
    });

    let source_format = ext.clone();

    match ext.as_str() {
        "docx" | "hwpx" | "xlsx" | "pptx" => {
            match converter::convert_file(&request.source, &out_dir, &formats).await {
                Ok(result) => Ok(MoaConvertResponse {
                    success: true,
                    task_id: request.task_id,
                    source_format,
                    target_format: request.target_format,
                    html: result.html,
                    markdown: result.markdown,
                    output_files: result.output_files,
                    images: result.images,
                    metadata: MoaDocMetadata {
                        page_count: result.page_count,
                        title: result.title,
                        author: result.author,
                        file_size,
                        converter_version: "1.0.0".to_string(),
                    },
                    error: None,
                }),
                Err(e) => Ok(MoaConvertResponse {
                    success: false,
                    task_id: request.task_id,
                    source_format,
                    target_format: request.target_format,
                    html: None,
                    markdown: None,
                    output_files: vec![],
                    images: vec![],
                    metadata: MoaDocMetadata {
                        page_count: None,
                        title: None,
                        author: None,
                        file_size,
                        converter_version: "1.0.0".to_string(),
                    },
                    error: Some(e),
                }),
            }
        }
        "pdf" => {
            // Route to Python backend
            let port = crate::backend::process::get_port();
            let client = reqwest::Client::new();
            let body = serde_json::json!({
                "input_path": request.source,
                "output_dir": out_dir,
                "formats": formats,
            });

            match client
                .post(format!("http://127.0.0.1:{}/api/convert", port))
                .json(&body)
                .send()
                .await
            {
                Ok(resp) => {
                    let data: serde_json::Value = resp
                        .json()
                        .await
                        .unwrap_or(serde_json::json!({"error": "parse failed"}));
                    Ok(MoaConvertResponse {
                        success: true,
                        task_id: request.task_id.or(data.get("job_id").and_then(|v| v.as_str()).map(String::from)),
                        source_format,
                        target_format: request.target_format,
                        html: None, // async - use job_id to track
                        markdown: None,
                        output_files: vec![],
                        images: vec![],
                        metadata: MoaDocMetadata {
                            page_count: None,
                            title: None,
                            author: None,
                            file_size,
                            converter_version: "1.0.0".to_string(),
                        },
                        error: data.get("error").and_then(|v| v.as_str()).map(String::from),
                    })
                }
                Err(e) => Ok(MoaConvertResponse {
                    success: false,
                    task_id: request.task_id,
                    source_format,
                    target_format: request.target_format,
                    html: None,
                    markdown: None,
                    output_files: vec![],
                    images: vec![],
                    metadata: MoaDocMetadata {
                        page_count: None,
                        title: None,
                        author: None,
                        file_size,
                        converter_version: "1.0.0".to_string(),
                    },
                    error: Some(format!("Backend error: {}", e)),
                }),
            }
        }
        _ => Err(format!("Unsupported format: .{}", ext)),
    }
}

#[tauri::command]
pub async fn moa_health() -> Result<serde_json::Value, String> {
    let python_ok = crate::backend::process::health_check().await;
    Ok(serde_json::json!({
        "status": "ok",
        "tool_name": "moa-doc-converter",
        "tool_version": "1.0.0",
        "python_backend": if python_ok { "running" } else { "stopped" },
        "supported_formats": ["pdf", "docx", "hwpx", "xlsx", "pptx"],
        "rust_native_formats": ["docx", "hwpx", "xlsx", "pptx"],
        "python_pipeline_formats": ["pdf"],
    }))
}

#[tauri::command]
pub fn moa_supported_formats() -> Vec<MoaSupportedFormat> {
    vec![
        MoaSupportedFormat {
            extension: "pdf".into(),
            mime_type: "application/pdf".into(),
            description: "PDF 문서 (AI 레이아웃 분석 + OCR)".into(),
            engine: "python-pipeline".into(),
        },
        MoaSupportedFormat {
            extension: "docx".into(),
            mime_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document".into(),
            description: "Microsoft Word 문서".into(),
            engine: "rust-native".into(),
        },
        MoaSupportedFormat {
            extension: "hwpx".into(),
            mime_type: "application/hwp+zip".into(),
            description: "한글 문서 (HWPX 형식)".into(),
            engine: "rust-native".into(),
        },
        MoaSupportedFormat {
            extension: "xlsx".into(),
            mime_type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet".into(),
            description: "Microsoft Excel 스프레드시트".into(),
            engine: "rust-native".into(),
        },
        MoaSupportedFormat {
            extension: "pptx".into(),
            mime_type: "application/vnd.openxmlformats-officedocument.presentationml.presentation".into(),
            description: "Microsoft PowerPoint 프레젠테이션".into(),
            engine: "rust-native".into(),
        },
    ]
}
