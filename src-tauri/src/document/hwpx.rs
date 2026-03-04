use super::converter::DocMeta;
use quick_xml::events::Event;
use quick_xml::Reader;
use std::io::Read;
use zip::ZipArchive;

/// HWPX → HTML converter (Rust-native)
/// HWPX is the Korean ODF-based format (ZIP with XML inside)
/// Structure: Contents/section0.xml, Contents/section1.xml, ...
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
        std::fs::File::open(path).map_err(|e| format!("Cannot open HWPX file: {}", e))?;
    let mut archive =
        ZipArchive::new(file).map_err(|e| format!("Invalid HWPX (not a ZIP): {}", e))?;

    let mut meta = DocMeta::default();
    let mut images: Vec<(String, Vec<u8>)> = Vec::new();

    // Extract metadata from META-INF/container.xml or header.xml
    if let Ok(mut header) = archive.by_name("Contents/header.xml") {
        let mut xml = String::new();
        let _ = header.read_to_string(&mut xml);
        meta.title = extract_between(&xml, "<hp:title>", "</hp:title>");
        meta.author = extract_between(&xml, "<hp:author>", "</hp:author>");
    }

    // Extract images from BinData/
    let bin_names: Vec<String> = (0..archive.len())
        .filter_map(|i| {
            let entry = archive.by_index(i).ok()?;
            let name = entry.name().to_string();
            if name.starts_with("BinData/") && !name.ends_with('/') {
                Some(name)
            } else {
                None
            }
        })
        .collect();

    for name in &bin_names {
        if let Ok(mut entry) = archive.by_name(name) {
            let mut data = Vec::new();
            let _ = entry.read_to_end(&mut data);
            let filename = name.rsplit('/').next().unwrap_or(name).to_string();
            images.push((filename, data));
        }
    }

    // Find all section files
    let mut section_names: Vec<String> = (0..archive.len())
        .filter_map(|i| {
            let entry = archive.by_index(i).ok()?;
            let name = entry.name().to_string();
            if name.starts_with("Contents/section") && name.ends_with(".xml") {
                Some(name)
            } else {
                None
            }
        })
        .collect();
    section_names.sort();

    if section_names.is_empty() {
        return Err("No section files found in HWPX".to_string());
    }

    let mut all_html = String::new();
    for (idx, section_name) in section_names.iter().enumerate() {
        let mut section_xml = String::new();
        if let Ok(mut section) = archive.by_name(section_name) {
            let _ = section.read_to_string(&mut section_xml);
        }

        if idx > 0 {
            all_html.push_str("<div class=\"page-break\"></div>\n");
        }
        all_html.push_str(&parse_hwpx_section(&section_xml));
    }

    meta.page_count = Some(section_names.len() as u32);

    let full_html = format!(
        r#"<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{}</title>
<style>
body {{ font-family: '맑은 고딕', 'Malgun Gothic', 'Nanum Gothic', sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; line-height: 1.8; color: #333; }}
h1 {{ font-size: 22px; border-bottom: 2px solid #333; padding-bottom: 8px; }}
h2 {{ font-size: 18px; border-bottom: 1px solid #ccc; padding-bottom: 4px; }}
h3 {{ font-size: 15px; }}
table {{ border-collapse: collapse; width: 100%; margin: 16px 0; }}
th, td {{ border: 1px solid #999; padding: 6px 10px; }}
th {{ background: #e8e8e8; font-weight: bold; }}
img {{ max-width: 100%; height: auto; }}
.page-break {{ page-break-before: always; border-top: 2px dashed #ccc; margin: 32px 0; padding-top: 16px; }}
.footnote {{ font-size: 0.85em; color: #666; }}
</style>
</head>
<body>
{}
</body>
</html>"#,
        html_escape::encode_text(meta.title.as_deref().unwrap_or("한글 문서")),
        all_html
    );

    Ok((full_html, images, meta))
}

fn parse_hwpx_section(xml: &str) -> String {
    let mut reader = Reader::from_str(xml);
    reader.config_mut().trim_text(true);
    let mut buf = Vec::new();
    let mut html = String::new();

    let mut in_paragraph = false;
    let mut in_run = false;
    let mut in_table = false;
    let mut in_table_cell = false;
    let mut text_buf = String::new();
    let mut is_bold = false;
    let mut is_italic = false;
    let mut para_style = String::new();

    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Start(ref e)) | Ok(Event::Empty(ref e)) => {
                let local = local_name(e.name().as_ref());
                match local {
                    "p" => {
                        in_paragraph = true;
                        text_buf.clear();
                        is_bold = false;
                        is_italic = false;
                        para_style.clear();
                    }
                    "run" => {
                        in_run = true;
                    }
                    "bold" => {
                        is_bold = true;
                    }
                    "italic" => {
                        is_italic = true;
                    }
                    "pPr" | "paraStyle" => {
                        // Paragraph style - check for heading
                        for attr in e.attributes().flatten() {
                            let key = local_name(attr.key.as_ref());
                            if key == "id" || key == "paraPrIDRef" {
                                para_style =
                                    String::from_utf8_lossy(&attr.value).to_string();
                            }
                        }
                    }
                    "tbl" => {
                        in_table = true;
                        html.push_str("<table>\n");
                    }
                    "tr" => {
                        html.push_str("<tr>");
                    }
                    "tc" => {
                        in_table_cell = true;
                        html.push_str("<td>");
                    }
                    "lineseg" | "lineBreak" => {
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
                        text_buf.push_str(&segment);
                    }
                }
            }
            Ok(Event::End(ref e)) => {
                let local = local_name(e.name().as_ref());
                match local {
                    "p" => {
                        if !text_buf.trim().is_empty() || in_table_cell {
                            if in_table_cell {
                                html.push_str(&text_buf);
                            } else if is_heading_style(&para_style) {
                                let level = heading_level_from_style(&para_style);
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
                    "run" => {
                        in_run = false;
                    }
                    "tbl" => {
                        in_table = false;
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
                tracing::warn!("HWPX XML parse error: {}", e);
                break;
            }
            _ => {}
        }
        buf.clear();
    }

    html
}

fn is_heading_style(style: &str) -> bool {
    let s = style.to_lowercase();
    s.contains("heading")
        || s.contains("제목")
        || s.contains("개요")
        || s.contains("outline")
}

fn heading_level_from_style(style: &str) -> u8 {
    let num: u8 = style
        .chars()
        .filter(|c| c.is_ascii_digit())
        .collect::<String>()
        .parse()
        .unwrap_or(1);
    num.clamp(1, 6)
}

fn extract_between(text: &str, start_tag: &str, end_tag: &str) -> Option<String> {
    let start = text.find(start_tag)? + start_tag.len();
    let end = text[start..].find(end_tag)? + start;
    let val = text[start..end].trim();
    if val.is_empty() {
        None
    } else {
        Some(val.to_string())
    }
}

fn local_name(name: &[u8]) -> &str {
    let s = std::str::from_utf8(name).unwrap_or("");
    s.rsplit(':').next().unwrap_or(s)
}
