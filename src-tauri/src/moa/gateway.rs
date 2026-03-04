use serde::{Deserialize, Serialize};

/// MoA (zeroclaw) Gateway Tool Registration Interface
///
/// This module defines the tool specification that MoA gateway uses
/// to discover and invoke this document converter.
///
/// Integration flow:
/// 1. MoA gateway discovers tools via `tool_manifest()`
/// 2. AI agent decides to use "doc-converter" tool
/// 3. Gateway calls Tauri command `moa_convert` via IPC
/// 4. Results returned to AI agent for processing

#[derive(Serialize, Deserialize, Clone)]
pub struct ToolManifest {
    pub name: String,
    pub version: String,
    pub description: String,
    pub category: String,
    pub capabilities: Vec<ToolCapability>,
    pub input_schema: serde_json::Value,
    pub output_schema: serde_json::Value,
}

#[derive(Serialize, Deserialize, Clone)]
pub struct ToolCapability {
    pub name: String,
    pub description: String,
    pub input_formats: Vec<String>,
    pub output_formats: Vec<String>,
}

/// Returns the MoA tool manifest for discovery
pub fn tool_manifest() -> ToolManifest {
    ToolManifest {
        name: "moa-doc-converter".to_string(),
        version: "1.0.0".to_string(),
        description: "문서 변환 도구 - PDF, DOCX, HWPX, XLSX, PPTX를 HTML/Markdown으로 변환"
            .to_string(),
        category: "문서작업".to_string(),
        capabilities: vec![
            ToolCapability {
                name: "pdf_convert".to_string(),
                description: "PDF 문서를 AI 레이아웃 분석으로 HTML/Markdown 변환".to_string(),
                input_formats: vec!["pdf".to_string()],
                output_formats: vec!["html".to_string(), "markdown".to_string()],
            },
            ToolCapability {
                name: "docx_convert".to_string(),
                description: "Word 문서(DOCX)를 HTML/Markdown으로 변환".to_string(),
                input_formats: vec!["docx".to_string()],
                output_formats: vec!["html".to_string(), "markdown".to_string()],
            },
            ToolCapability {
                name: "hwpx_convert".to_string(),
                description: "한글 문서(HWPX)를 HTML/Markdown으로 변환".to_string(),
                input_formats: vec!["hwpx".to_string()],
                output_formats: vec!["html".to_string(), "markdown".to_string()],
            },
            ToolCapability {
                name: "xlsx_convert".to_string(),
                description: "Excel 스프레드시트(XLSX)를 HTML 테이블로 변환".to_string(),
                input_formats: vec!["xlsx".to_string()],
                output_formats: vec!["html".to_string(), "markdown".to_string()],
            },
            ToolCapability {
                name: "pptx_convert".to_string(),
                description: "PowerPoint(PPTX) 프레젠테이션을 HTML/Markdown으로 변환".to_string(),
                input_formats: vec!["pptx".to_string()],
                output_formats: vec!["html".to_string(), "markdown".to_string()],
            },
        ],
        input_schema: serde_json::json!({
            "type": "object",
            "required": ["source", "target_format"],
            "properties": {
                "source": {
                    "type": "string",
                    "description": "소스 문서 파일 경로"
                },
                "target_format": {
                    "type": "string",
                    "enum": ["html", "markdown", "both"],
                    "description": "출력 형식"
                },
                "output_dir": {
                    "type": "string",
                    "description": "출력 디렉토리 (선택)"
                },
                "quality": {
                    "type": "string",
                    "enum": ["fast", "balanced", "quality"],
                    "description": "변환 품질 (PDF만 해당)"
                },
                "task_id": {
                    "type": "string",
                    "description": "MoA 작업 추적 ID"
                }
            }
        }),
        output_schema: serde_json::json!({
            "type": "object",
            "properties": {
                "success": { "type": "boolean" },
                "task_id": { "type": "string" },
                "source_format": { "type": "string" },
                "html": { "type": "string", "description": "변환된 HTML" },
                "markdown": { "type": "string", "description": "변환된 Markdown" },
                "output_files": {
                    "type": "array",
                    "items": { "type": "string" }
                },
                "images": {
                    "type": "array",
                    "items": { "type": "string" }
                },
                "metadata": {
                    "type": "object",
                    "properties": {
                        "page_count": { "type": "integer" },
                        "title": { "type": "string" },
                        "author": { "type": "string" },
                        "file_size": { "type": "integer" }
                    }
                },
                "error": { "type": "string" }
            }
        }),
    }
}
