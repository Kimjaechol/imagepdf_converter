use serde::{Deserialize, Serialize};
use std::path::Path;

#[derive(Serialize, Deserialize, Debug)]
pub struct ConvertResult {
    pub html: Option<String>,
    pub markdown: Option<String>,
    pub output_files: Vec<String>,
    pub images: Vec<String>,
    pub page_count: Option<u32>,
    pub title: Option<String>,
    pub author: Option<String>,
}

/// Unified document converter - routes to the right Rust-native converter
pub async fn convert_file(
    input_path: &str,
    output_dir: &str,
    formats: &[String],
) -> Result<ConvertResult, String> {
    let path = Path::new(input_path);
    let ext = path
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_lowercase();

    // Ensure output dir exists
    tokio::fs::create_dir_all(output_dir)
        .await
        .map_err(|e| format!("Cannot create output dir: {}", e))?;

    let stem = path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("output");

    let want_html = formats.iter().any(|f| f == "html");
    let want_md = formats.iter().any(|f| f == "markdown" || f == "md");

    // Convert to HTML first (all converters produce HTML)
    let (html_content, images, meta) = match ext.as_str() {
        "docx" => super::docx::convert_to_html(input_path).await?,
        "hwpx" => super::hwpx::convert_to_html(input_path).await?,
        "xlsx" => super::xlsx::convert_to_html(input_path).await?,
        "pptx" => super::pptx::convert_to_html(input_path).await?,
        _ => return Err(format!("Unsupported format: .{}", ext)),
    };

    let mut result = ConvertResult {
        html: None,
        markdown: None,
        output_files: vec![],
        images: vec![],
        page_count: meta.page_count,
        title: meta.title,
        author: meta.author,
    };

    // Save images
    let img_dir = Path::new(output_dir).join(format!("{}_images", stem));
    if !images.is_empty() {
        tokio::fs::create_dir_all(&img_dir)
            .await
            .map_err(|e| format!("Cannot create image dir: {}", e))?;

        for (i, (name, data)) in images.iter().enumerate() {
            let img_name = if name.is_empty() {
                format!("image_{}.png", i + 1)
            } else {
                name.clone()
            };
            let img_path = img_dir.join(&img_name);
            tokio::fs::write(&img_path, data)
                .await
                .map_err(|e| format!("Cannot save image: {}", e))?;
            result.images.push(img_path.to_string_lossy().to_string());
        }
    }

    // Save HTML
    if want_html {
        let html_path = Path::new(output_dir).join(format!("{}.html", stem));
        tokio::fs::write(&html_path, &html_content)
            .await
            .map_err(|e| format!("Cannot write HTML: {}", e))?;
        result.html = Some(html_content.clone());
        result.output_files.push(html_path.to_string_lossy().to_string());
    }

    // Convert HTML → Markdown
    if want_md {
        let md_content = html_to_markdown(&html_content);
        let md_path = Path::new(output_dir).join(format!("{}.md", stem));
        tokio::fs::write(&md_path, &md_content)
            .await
            .map_err(|e| format!("Cannot write Markdown: {}", e))?;
        result.markdown = Some(md_content);
        result.output_files.push(md_path.to_string_lossy().to_string());
    }

    Ok(result)
}

/// Document metadata from conversion
#[derive(Default)]
pub struct DocMeta {
    pub page_count: Option<u32>,
    pub title: Option<String>,
    pub author: Option<String>,
}

/// Simple HTML → Markdown converter
fn html_to_markdown(html: &str) -> String {
    let mut md = String::new();
    let mut in_tag = false;
    let mut tag_buf = String::new();
    let mut chars = html.chars().peekable();

    // Simple state machine for HTML → Markdown
    while let Some(c) = chars.next() {
        if c == '<' {
            in_tag = true;
            tag_buf.clear();
            continue;
        }
        if c == '>' && in_tag {
            in_tag = false;
            let tag = tag_buf.trim().to_lowercase();
            match tag.as_str() {
                "h1" => md.push_str("\n# "),
                "h2" => md.push_str("\n## "),
                "h3" => md.push_str("\n### "),
                "h4" => md.push_str("\n#### "),
                "h5" => md.push_str("\n##### "),
                "h6" => md.push_str("\n###### "),
                "/h1" | "/h2" | "/h3" | "/h4" | "/h5" | "/h6" => md.push_str("\n\n"),
                "p" => md.push('\n'),
                "/p" => md.push_str("\n\n"),
                "br" | "br/" | "br /" => md.push('\n'),
                "strong" | "b" => md.push_str("**"),
                "/strong" | "/b" => md.push_str("**"),
                "em" | "i" => md.push('*'),
                "/em" | "/i" => md.push('*'),
                "li" => md.push_str("\n- "),
                "/li" => {}
                "tr" => md.push('\n'),
                "th" | "td" => md.push_str("| "),
                "/th" | "/td" => md.push(' '),
                "/tr" => md.push('|'),
                "hr" | "hr/" => md.push_str("\n---\n"),
                "blockquote" => md.push_str("\n> "),
                "/blockquote" => md.push('\n'),
                "code" => md.push('`'),
                "/code" => md.push('`'),
                "pre" => md.push_str("\n```\n"),
                "/pre" => md.push_str("\n```\n"),
                _ => {}
            }
            continue;
        }
        if in_tag {
            tag_buf.push(c);
        } else {
            // Decode basic HTML entities
            if c == '&' {
                let mut entity = String::new();
                while let Some(&nc) = chars.peek() {
                    if nc == ';' {
                        chars.next();
                        break;
                    }
                    entity.push(nc);
                    chars.next();
                }
                match entity.as_str() {
                    "amp" => md.push('&'),
                    "lt" => md.push('<'),
                    "gt" => md.push('>'),
                    "quot" => md.push('"'),
                    "nbsp" => md.push(' '),
                    _ => {
                        md.push('&');
                        md.push_str(&entity);
                        md.push(';');
                    }
                }
            } else {
                md.push(c);
            }
        }
    }

    // Clean up excessive newlines
    let re = regex::Regex::new(r"\n{3,}").unwrap();
    re.replace_all(&md, "\n\n").trim().to_string()
}
