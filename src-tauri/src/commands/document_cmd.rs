use serde::Serialize;

#[derive(Serialize)]
pub struct DocConvertResult {
    pub html: Option<String>,
    pub markdown: Option<String>,
    pub output_files: Vec<String>,
    pub images: Vec<String>,
}

#[tauri::command]
pub async fn convert_docx_to_html(
    input_path: String,
    output_dir: Option<String>,
) -> Result<DocConvertResult, String> {
    do_convert(&input_path, output_dir, &["html", "markdown"]).await
}

#[tauri::command]
pub async fn convert_hwpx_to_html(
    input_path: String,
    output_dir: Option<String>,
) -> Result<DocConvertResult, String> {
    do_convert(&input_path, output_dir, &["html", "markdown"]).await
}

#[tauri::command]
pub async fn convert_xlsx_to_html(
    input_path: String,
    output_dir: Option<String>,
) -> Result<DocConvertResult, String> {
    do_convert(&input_path, output_dir, &["html", "markdown"]).await
}

#[tauri::command]
pub async fn convert_pptx_to_html(
    input_path: String,
    output_dir: Option<String>,
) -> Result<DocConvertResult, String> {
    do_convert(&input_path, output_dir, &["html", "markdown"]).await
}

#[tauri::command]
pub async fn convert_any_document(
    input_path: String,
    output_dir: Option<String>,
    formats: Option<Vec<String>>,
) -> Result<DocConvertResult, String> {
    let fmts: Vec<&str> = formats
        .as_ref()
        .map(|f| f.iter().map(|s| s.as_str()).collect())
        .unwrap_or_else(|| vec!["html", "markdown"]);
    do_convert(&input_path, output_dir, &fmts).await
}

/// Convert HTML string to Markdown (used by the editor for markdown export/auto-save)
#[tauri::command]
pub fn html_to_markdown(html: String) -> Result<String, String> {
    Ok(crate::document::converter::html_to_markdown(&html))
}

async fn do_convert(
    input_path: &str,
    output_dir: Option<String>,
    formats: &[&str],
) -> Result<DocConvertResult, String> {
    let out_dir = output_dir.unwrap_or_else(|| {
        std::path::Path::new(input_path)
            .parent()
            .unwrap_or(std::path::Path::new("."))
            .to_string_lossy()
            .to_string()
    });

    let fmt_strings: Vec<String> = formats.iter().map(|s| s.to_string()).collect();
    let result = crate::document::converter::convert_file(input_path, &out_dir, &fmt_strings).await?;

    Ok(DocConvertResult {
        html: result.html,
        markdown: result.markdown,
        output_files: result.output_files,
        images: result.images,
    })
}
