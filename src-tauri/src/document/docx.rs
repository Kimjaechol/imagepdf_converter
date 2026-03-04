use super::converter::DocMeta;
use quick_xml::events::Event;
use quick_xml::Reader;
use std::io::Read;
use zip::ZipArchive;

/// DOCX → HTML converter (Rust-native)
/// DOCX is a ZIP containing word/document.xml + relationships + images
pub async fn convert_to_html(
    path: &str,
) -> Result<(String, Vec<(String, Vec<u8>)>, DocMeta), String> {
    let path = path.to_string();
    tokio::task::spawn_blocking(move || convert_sync(&path))
        .await
        .map_err(|e| format!("Task failed: {}", e))?
}

fn convert_sync(path: &str) -> Result<(String, Vec<(String, Vec<u8>)>, DocMeta), String> {
    let file =
        std::fs::File::open(path).map_err(|e| format!("Cannot open DOCX file: {}", e))?;
    let mut archive =
        ZipArchive::new(file).map_err(|e| format!("Invalid DOCX (not a ZIP): {}", e))?;

    let mut meta = DocMeta::default();

    // Extract core.xml for metadata
    if let Ok(mut core) = archive.by_name("docProps/core.xml") {
        let mut xml = String::new();
        let _ = core.read_to_string(&mut xml);
        meta.title = extract_xml_text(&xml, "dc:title");
        meta.author = extract_xml_text(&xml, "dc:creator");
    }

    // Extract images from word/media/
    let mut images: Vec<(String, Vec<u8>)> = Vec::new();
    let image_names: Vec<String> = (0..archive.len())
        .filter_map(|i| {
            let entry = archive.by_index(i).ok()?;
            let name = entry.name().to_string();
            if name.starts_with("word/media/") {
                Some(name)
            } else {
                None
            }
        })
        .collect();

    for name in &image_names {
        if let Ok(mut entry) = archive.by_name(name) {
            let mut data = Vec::new();
            let _ = entry.read_to_end(&mut data);
            let filename = name.rsplit('/').next().unwrap_or(name).to_string();
            images.push((filename, data));
        }
    }

    // Parse word/document.xml
    let mut doc_xml = String::new();
    {
        let mut doc = archive
            .by_name("word/document.xml")
            .map_err(|_| "No word/document.xml found in DOCX")?;
        doc.read_to_string(&mut doc_xml)
            .map_err(|e| format!("Cannot read document.xml: {}", e))?;
    }

    let html = parse_document_xml(&doc_xml, &images);

    let page_count = html.matches("<div class=\"page-break\"").count() as u32 + 1;
    meta.page_count = Some(page_count.max(1));

    let full_html = format!(
        r#"<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{}</title>
<style>
body {{ font-family: 'Malgun Gothic', '맑은 고딕', sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; line-height: 1.6; color: #333; }}
h1 {{ font-size: 24px; border-bottom: 2px solid #333; padding-bottom: 8px; }}
h2 {{ font-size: 20px; border-bottom: 1px solid #ccc; padding-bottom: 4px; }}
h3 {{ font-size: 16px; }}
table {{ border-collapse: collapse; width: 100%; margin: 16px 0; }}
th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
th {{ background: #f5f5f5; font-weight: bold; }}
img {{ max-width: 100%; height: auto; }}
.page-break {{ page-break-before: always; border-top: 2px dashed #ccc; margin: 32px 0; padding-top: 16px; }}
blockquote {{ border-left: 4px solid #ccc; margin: 16px 0; padding: 8px 16px; color: #555; }}
</style>
</head>
<body>
{}
</body>
</html>"#,
        html_escape::encode_text(meta.title.as_deref().unwrap_or("문서")),
        html
    );

    Ok((full_html, images, meta))
}

fn parse_document_xml(xml: &str, images: &[(String, Vec<u8>)]) -> String {
    let mut reader = Reader::from_str(xml);
    reader.config_mut().trim_text(true);
    let mut html = String::new();
    let mut buf = Vec::new();

    let mut in_paragraph = false;
    let mut in_run = false;
    let mut is_bold = false;
    let mut is_italic = false;
    let mut is_underline = false;
    let mut heading_level: Option<u8> = None;
    let mut in_table_cell = false;
    let mut text_buf = String::new();
    let mut image_idx = 0usize;

    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Start(ref e)) | Ok(Event::Empty(ref e)) => {
                let qname = e.name();
                let local = local_name(qname.as_ref());
                match local {
                    "p" => {
                        in_paragraph = true;
                        text_buf.clear();
                        is_bold = false;
                        is_italic = false;
                        is_underline = false;
                        heading_level = None;
                    }
                    "r" => {
                        in_run = true;
                    }
                    "b" if in_run || in_paragraph => {
                        is_bold = true;
                    }
                    "i" if in_run || in_paragraph => {
                        is_italic = true;
                    }
                    "u" if in_run || in_paragraph => {
                        is_underline = true;
                    }
                    "pStyle" => {
                        for attr in e.attributes().flatten() {
                            if local_name(attr.key.as_ref()) == "val" {
                                let val = String::from_utf8_lossy(&attr.value).to_lowercase();
                                if val.contains("heading") || val.contains("제목") {
                                    // Extract heading number
                                    let num: u8 = val
                                        .chars()
                                        .filter(|c| c.is_ascii_digit())
                                        .collect::<String>()
                                        .parse()
                                        .unwrap_or(1);
                                    heading_level = Some(num.clamp(1, 6));
                                }
                            }
                        }
                    }
                    "tbl" => {
                        html.push_str("<table>\n");
                    }
                    "tr" => {
                        html.push_str("<tr>");
                    }
                    "tc" => {
                        in_table_cell = true;
                        html.push_str("<td>");
                    }
                    "drawing" | "pict" => {
                        if image_idx < images.len() {
                            let (name, _) = &images[image_idx];
                            text_buf.push_str(&format!(
                                "<img src=\"{}_images/{}\" alt=\"{}\">",
                                "", name, name
                            ));
                            image_idx += 1;
                        }
                    }
                    "br" => {
                        // Page or line break
                        for attr in e.attributes().flatten() {
                            if local_name(attr.key.as_ref()) == "type" {
                                let val = String::from_utf8_lossy(&attr.value);
                                if val == "page" {
                                    html.push_str("<div class=\"page-break\"></div>\n");
                                }
                            }
                        }
                        text_buf.push_str("<br>");
                    }
                    _ => {}
                }
            }
            Ok(Event::Text(ref e)) => {
                if in_run || in_paragraph {
                    let text = e.unescape().unwrap_or_default().to_string();
                    if !text.is_empty() {
                        let mut segment = html_escape::encode_text(&text).to_string();
                        if is_bold {
                            segment = format!("<strong>{}</strong>", segment);
                        }
                        if is_italic {
                            segment = format!("<em>{}</em>", segment);
                        }
                        if is_underline {
                            segment = format!("<u>{}</u>", segment);
                        }
                        text_buf.push_str(&segment);
                    }
                }
            }
            Ok(Event::End(ref e)) => {
                let qname = e.name();
                let local = local_name(qname.as_ref());
                match local {
                    "p" => {
                        if !text_buf.trim().is_empty() || in_table_cell {
                            if in_table_cell {
                                html.push_str(&text_buf);
                            } else if let Some(level) = heading_level {
                                html.push_str(&format!(
                                    "<h{l}>{}</h{l}>\n",
                                    text_buf,
                                    l = level
                                ));
                            } else {
                                html.push_str(&format!("<p>{}</p>\n", text_buf));
                            }
                        }
                        text_buf.clear();
                        in_paragraph = false;
                    }
                    "r" => {
                        in_run = false;
                        is_bold = false;
                        is_italic = false;
                        is_underline = false;
                    }
                    "tbl" => {
                        html.push_str("</table>\n");
                    }
                    "tr" => {
                        html.push_str("</tr>\n");
                    }
                    "tc" => {
                        in_table_cell = false;
                        html.push_str("</td>");
                    }
                    _ => {}
                }
            }
            Ok(Event::Eof) => break,
            Err(e) => {
                tracing::warn!("XML parse error in DOCX: {}", e);
                break;
            }
            _ => {}
        }
        buf.clear();
    }

    html
}

fn extract_xml_text(xml: &str, tag: &str) -> Option<String> {
    let open = format!("<{}", tag);
    let close = format!("</{}>", tag);
    if let Some(start) = xml.find(&open) {
        if let Some(tag_end) = xml[start..].find('>') {
            let content_start = start + tag_end + 1;
            if let Some(end) = xml[content_start..].find(&close) {
                let text = &xml[content_start..content_start + end];
                if !text.trim().is_empty() {
                    return Some(text.trim().to_string());
                }
            }
        }
    }
    None
}

fn local_name(name: &[u8]) -> &str {
    let s = std::str::from_utf8(name).unwrap_or("");
    s.rsplit(':').next().unwrap_or(s)
}
