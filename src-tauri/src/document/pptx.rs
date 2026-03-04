use super::converter::DocMeta;
use quick_xml::events::Event;
use quick_xml::Reader;
use std::io::Read;
use zip::ZipArchive;

/// PPTX → HTML converter (Rust-native)
/// PPTX is a ZIP containing ppt/slides/slide1.xml, slide2.xml, ...
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
        std::fs::File::open(path).map_err(|e| format!("Cannot open PPTX file: {}", e))?;
    let mut archive =
        ZipArchive::new(file).map_err(|e| format!("Invalid PPTX (not a ZIP): {}", e))?;

    let mut meta = DocMeta::default();

    // Metadata from docProps/core.xml
    if let Ok(mut core) = archive.by_name("docProps/core.xml") {
        let mut xml = String::new();
        let _ = core.read_to_string(&mut xml);
        meta.title = extract_between(&xml, "<dc:title>", "</dc:title>");
        meta.author = extract_between(&xml, "<dc:creator>", "</dc:creator>");
    }

    // Extract images from ppt/media/
    let mut images: Vec<(String, Vec<u8>)> = Vec::new();
    let media_names: Vec<String> = (0..archive.len())
        .filter_map(|i| {
            let entry = archive.by_index(i).ok()?;
            let name = entry.name().to_string();
            if name.starts_with("ppt/media/") && !name.ends_with('/') {
                Some(name)
            } else {
                None
            }
        })
        .collect();

    for name in &media_names {
        if let Ok(mut entry) = archive.by_name(name) {
            let mut data = Vec::new();
            let _ = entry.read_to_end(&mut data);
            let filename = name.rsplit('/').next().unwrap_or(name).to_string();
            images.push((filename, data));
        }
    }

    // Find all slide files
    let mut slide_names: Vec<String> = (0..archive.len())
        .filter_map(|i| {
            let entry = archive.by_index(i).ok()?;
            let name = entry.name().to_string();
            if name.starts_with("ppt/slides/slide") && name.ends_with(".xml") {
                Some(name)
            } else {
                None
            }
        })
        .collect();
    slide_names.sort_by(|a, b| {
        let num_a = extract_slide_num(a);
        let num_b = extract_slide_num(b);
        num_a.cmp(&num_b)
    });

    meta.page_count = Some(slide_names.len() as u32);

    let mut body_html = String::new();
    for (idx, slide_name) in slide_names.iter().enumerate() {
        let mut slide_xml = String::new();
        if let Ok(mut slide) = archive.by_name(slide_name) {
            let _ = slide.read_to_string(&mut slide_xml);
        }

        body_html.push_str(&format!(
            "<div class=\"slide\" id=\"slide-{}\">\n<div class=\"slide-header\">슬라이드 {}</div>\n",
            idx + 1,
            idx + 1
        ));
        body_html.push_str(&parse_slide_xml(&slide_xml));
        body_html.push_str("</div>\n");
    }

    let full_html = format!(
        r#"<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{}</title>
<style>
body {{ font-family: 'Malgun Gothic', '맑은 고딕', sans-serif; max-width: 960px; margin: 0 auto; padding: 20px; background: #f0f0f0; color: #333; }}
.slide {{ background: white; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.15); margin: 24px 0; padding: 32px 40px; min-height: 300px; }}
.slide-header {{ font-size: 12px; color: #999; margin-bottom: 16px; padding-bottom: 8px; border-bottom: 1px solid #eee; }}
h1 {{ font-size: 28px; color: #1a1a2e; margin: 0 0 16px 0; }}
h2 {{ font-size: 22px; color: #16213e; }}
h3 {{ font-size: 18px; color: #333; }}
p {{ font-size: 16px; line-height: 1.6; margin: 8px 0; }}
ul, ol {{ margin: 8px 0 8px 24px; }}
li {{ margin: 4px 0; font-size: 16px; }}
table {{ border-collapse: collapse; width: 100%; margin: 16px 0; }}
th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
th {{ background: #e8e8e8; }}
img {{ max-width: 100%; height: auto; border-radius: 4px; }}
.notes {{ background: #fffde7; padding: 12px 16px; border-left: 3px solid #fbc02d; margin-top: 16px; font-size: 14px; color: #555; }}
</style>
</head>
<body>
{}
</body>
</html>"#,
        html_escape::encode_text(meta.title.as_deref().unwrap_or("프레젠테이션")),
        body_html
    );

    Ok((full_html, images, meta))
}

fn parse_slide_xml(xml: &str) -> String {
    let mut reader = Reader::from_str(xml);
    reader.config_mut().trim_text(true);
    let mut buf = Vec::new();
    let mut html = String::new();

    let mut text_parts: Vec<String> = Vec::new();
    let mut in_text_body = false;
    let mut in_paragraph = false;
    let mut in_run = false;
    let mut is_bold = false;
    let mut is_title = false;
    let mut current_text = String::new();
    let mut para_texts: Vec<String> = Vec::new();
    let mut is_list_item = false;
    let mut list_level: i32 = -1;

    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Start(ref e)) | Ok(Event::Empty(ref e)) => {
                let local = local_name(e.name().as_ref());
                match local {
                    "txBody" => {
                        in_text_body = true;
                        text_parts.clear();
                    }
                    "p" if in_text_body => {
                        in_paragraph = true;
                        current_text.clear();
                        is_bold = false;
                        is_list_item = false;
                    }
                    "r" if in_text_body => {
                        in_run = true;
                    }
                    "b" if in_text_body => {
                        is_bold = true;
                    }
                    "buChar" | "buAutoNum" => {
                        is_list_item = true;
                    }
                    "pPr" if in_text_body => {
                        for attr in e.attributes().flatten() {
                            let key = local_name(attr.key.as_ref());
                            if key == "lvl" {
                                list_level = String::from_utf8_lossy(&attr.value)
                                    .parse()
                                    .unwrap_or(0);
                            }
                        }
                    }
                    "ph" => {
                        // Placeholder type
                        for attr in e.attributes().flatten() {
                            let key = local_name(attr.key.as_ref());
                            if key == "type" {
                                let val = String::from_utf8_lossy(&attr.value);
                                if val == "title" || val == "ctrTitle" {
                                    is_title = true;
                                }
                            }
                        }
                    }
                    "br" if in_text_body => {
                        current_text.push_str("<br>");
                    }
                    _ => {}
                }
            }
            Ok(Event::Text(ref e)) => {
                if in_run && in_text_body {
                    let text = e.unescape().unwrap_or_default().to_string();
                    if !text.is_empty() {
                        let escaped = html_escape::encode_text(&text).to_string();
                        if is_bold {
                            current_text.push_str(&format!("<strong>{}</strong>", escaped));
                        } else {
                            current_text.push_str(&escaped);
                        }
                    }
                }
            }
            Ok(Event::End(ref e)) => {
                let local = local_name(e.name().as_ref());
                match local {
                    "txBody" => {
                        in_text_body = false;
                        // Flush paragraphs
                        if !para_texts.is_empty() {
                            if is_title {
                                for t in &para_texts {
                                    if !t.trim().is_empty() {
                                        html.push_str(&format!("<h1>{}</h1>\n", t));
                                    }
                                }
                                is_title = false;
                            } else {
                                for t in &para_texts {
                                    if !t.trim().is_empty() {
                                        html.push_str(&format!("<p>{}</p>\n", t));
                                    }
                                }
                            }
                            para_texts.clear();
                        }
                    }
                    "p" if in_text_body => {
                        if !current_text.trim().is_empty() {
                            if is_list_item {
                                let indent = "  ".repeat(list_level.max(0) as usize);
                                para_texts.push(format!("{}• {}", indent, current_text));
                            } else {
                                para_texts.push(current_text.clone());
                            }
                        }
                        current_text.clear();
                        in_paragraph = false;
                        is_list_item = false;
                        list_level = -1;
                    }
                    "r" => {
                        in_run = false;
                        is_bold = false;
                    }
                    _ => {}
                }
            }
            Ok(Event::Eof) => break,
            Err(e) => {
                tracing::warn!("PPTX XML parse error: {}", e);
                break;
            }
            _ => {}
        }
        buf.clear();
    }

    let _ = in_paragraph;
    html
}

fn extract_slide_num(name: &str) -> u32 {
    name.chars()
        .filter(|c| c.is_ascii_digit())
        .collect::<String>()
        .parse()
        .unwrap_or(0)
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
