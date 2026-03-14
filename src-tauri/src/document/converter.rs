use serde::Serialize;
use std::path::Path;
use std::sync::OnceLock;

#[derive(Serialize, Debug)]
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
        "pdf" => super::pdf::convert_to_html(input_path).await?,
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

    // Save images - directory name must match HTML references (src="images/...")
    let img_dir = Path::new(output_dir).join("images");
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

    // Always keep HTML content for potential translation even if not saving HTML
    result.html = Some(html_content.clone());

    // Save HTML
    if want_html {
        let html_path = Path::new(output_dir).join(format!("{}.html", stem));
        tokio::fs::write(&html_path, &html_content)
            .await
            .map_err(|e| format!("Cannot write HTML: {}", e))?;
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

/// Extract just the tag name from a tag buffer (e.g. "img src=\"...\"" → "img")
fn extract_tag_name(tag_buf: &str) -> &str {
    let tag = tag_buf.trim();
    // Handle closing tags: "/td", "/tr" etc.
    if let Some(rest) = tag.strip_prefix('/') {
        rest.split_whitespace().next().unwrap_or(rest)
            .split('/')
            .next()
            .unwrap_or(rest)
    } else {
        tag.split_whitespace().next().unwrap_or(tag)
            .split('/')
            .next()
            .unwrap_or(tag)
    }
}

/// Extract attribute value from tag buffer: e.g. extract_attr("img src=\"foo.png\"", "src") → Some("foo.png")
fn extract_attr(tag_buf: &str, attr_name: &str) -> Option<String> {
    let search = format!("{}=\"", attr_name);
    if let Some(start) = tag_buf.find(&search) {
        let val_start = start + search.len();
        if let Some(end) = tag_buf[val_start..].find('"') {
            return Some(tag_buf[val_start..val_start + end].to_string());
        }
    }
    // Also try single quotes
    let search_sq = format!("{}='", attr_name);
    if let Some(start) = tag_buf.find(&search_sq) {
        let val_start = start + search_sq.len();
        if let Some(end) = tag_buf[val_start..].find('\'') {
            return Some(tag_buf[val_start..val_start + end].to_string());
        }
    }
    None
}

/// HTML → Markdown converter (also used by translation flow in conversion.rs)
#[allow(unused_assignments)]
pub fn html_to_markdown(html: &str) -> String {
    let mut md = String::new();
    let mut in_tag = false;
    let mut tag_buf = String::new();
    let mut chars = html.chars().peekable();
    let mut col_count: usize = 0;
    let mut row_col_count: usize = 0;
    let mut first_row_done = false;

    while let Some(c) = chars.next() {
        if c == '<' {
            in_tag = true;
            tag_buf.clear();
            continue;
        }
        if c == '>' && in_tag {
            in_tag = false;
            let full_tag = tag_buf.trim().to_lowercase();
            let tag_name = extract_tag_name(&full_tag);
            let is_closing = full_tag.starts_with('/');

            match tag_name {
                "h1" if !is_closing => md.push_str("\n# "),
                "h2" if !is_closing => md.push_str("\n## "),
                "h3" if !is_closing => md.push_str("\n### "),
                "h4" if !is_closing => md.push_str("\n#### "),
                "h5" if !is_closing => md.push_str("\n##### "),
                "h6" if !is_closing => md.push_str("\n###### "),
                "h1" | "h2" | "h3" | "h4" | "h5" | "h6" if is_closing => md.push_str("\n\n"),
                "p" if !is_closing => md.push('\n'),
                "p" if is_closing => md.push_str("\n\n"),
                "br" => md.push('\n'),
                "strong" | "b" => md.push_str("**"),
                "em" | "i" if tag_name == "em" || tag_name == "i" => md.push('*'),
                "u" if !is_closing => md.push_str("<u>"),
                "u" if is_closing => md.push_str("</u>"),
                "li" if !is_closing => md.push_str("\n- "),
                "li" if is_closing => {}
                "ul" | "ol" => {}
                "thead" | "tbody" => {}
                "table" if !is_closing => {
                    md.push('\n');
                    col_count = 0;
                    first_row_done = false;
                }
                "table" if is_closing => md.push('\n'),
                "tr" if !is_closing => {
                    md.push('\n');
                    row_col_count = 0;
                }
                "tr" if is_closing => {
                    md.push('|');
                    // After first row (header), emit separator
                    if !first_row_done {
                        col_count = row_col_count;
                        first_row_done = true;
                        md.push('\n');
                        for _ci in 0..col_count {
                            md.push_str("|---");
                        }
                        if col_count > 0 {
                            md.push('|');
                        }
                    }
                }
                "th" | "td" if !is_closing => {
                    md.push_str("| ");
                    row_col_count += 1;
                }
                "th" | "td" if is_closing => md.push(' '),
                "hr" => md.push_str("\n---\n"),
                "blockquote" if !is_closing => md.push_str("\n> "),
                "blockquote" if is_closing => md.push('\n'),
                "code" => md.push('`'),
                "pre" if !is_closing => md.push_str("\n```\n"),
                "pre" if is_closing => md.push_str("\n```\n"),
                "img" => {
                    // Handle <img src="..." alt="...">
                    let src = extract_attr(&full_tag, "src").unwrap_or_default();
                    let alt = extract_attr(&full_tag, "alt").unwrap_or_else(|| "image".to_string());
                    md.push_str(&format!("![{}]({})", alt, src));
                }
                "figure" | "figcaption" | "span" | "div" | "section" | "article"
                | "header" | "footer" | "nav" | "aside" | "main" => {}
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
                    if entity.len() > 10 {
                        break; // not a real entity
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
                    "apos" => md.push('\''),
                    "#39" => md.push('\''),
                    "#x27" => md.push('\''),
                    "mdash" => md.push('—'),
                    "ndash" => md.push('–'),
                    "hellip" => md.push_str("..."),
                    "laquo" => md.push('«'),
                    "raquo" => md.push('»'),
                    "ldquo" => md.push('\u{201C}'),
                    "rdquo" => md.push('\u{201D}'),
                    _ => {
                        // Handle numeric entities &#NNN;
                        if let Some(num_str) = entity.strip_prefix('#') {
                            let code = if let Some(hex_str) = num_str.strip_prefix('x').or_else(|| num_str.strip_prefix('X')) {
                                u32::from_str_radix(hex_str, 16).ok()
                            } else {
                                num_str.parse::<u32>().ok()
                            };
                            if let Some(ch) = code.and_then(char::from_u32) {
                                md.push(ch);
                            } else {
                                md.push('&');
                                md.push_str(&entity);
                                md.push(';');
                            }
                        } else {
                            md.push('&');
                            md.push_str(&entity);
                            md.push(';');
                        }
                    }
                }
            } else {
                md.push(c);
            }
        }
    }

    // Clean up excessive newlines (compile regex once)
    static RE: OnceLock<regex::Regex> = OnceLock::new();
    let re = RE.get_or_init(|| regex::Regex::new(r"\n{3,}").unwrap());
    re.replace_all(&md, "\n\n").trim().to_string()
}

/// Extract local name from a namespaced XML tag (e.g., "w:body" → "body")
/// Shared by docx, pptx, hwpx converters.
pub fn local_name(name: &[u8]) -> &str {
    let s = std::str::from_utf8(name).unwrap_or("");
    s.rsplit(':').next().unwrap_or(s)
}

/// Extract text between two tags in an XML string.
/// Shared by pptx, hwpx converters.
pub fn extract_between(text: &str, start_tag: &str, end_tag: &str) -> Option<String> {
    let start = text.find(start_tag)? + start_tag.len();
    let end = text[start..].find(end_tag)? + start;
    let val = text[start..end].trim();
    if val.is_empty() {
        None
    } else {
        Some(val.to_string())
    }
}
