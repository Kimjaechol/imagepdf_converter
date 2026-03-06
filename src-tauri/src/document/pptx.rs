use super::converter::DocMeta;
use quick_xml::events::Event;
use quick_xml::Reader;
use std::collections::HashMap;
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

        // Parse slide-level relationships for images
        // e.g. ppt/slides/_rels/slide1.xml.rels
        let rels_name = slide_name.replace("slides/", "slides/_rels/") + ".rels";
        let mut slide_rels: HashMap<String, String> = HashMap::new();
        if let Ok(mut rels) = archive.by_name(&rels_name) {
            let mut rels_xml = String::new();
            let _ = rels.read_to_string(&mut rels_xml);
            slide_rels = parse_pptx_relationships(&rels_xml);
        }

        body_html.push_str(&format!(
            "<div class=\"slide\" id=\"slide-{}\">\n<div class=\"slide-header\">슬라이드 {}</div>\n",
            idx + 1,
            idx + 1
        ));
        body_html.push_str(&parse_slide_xml(&slide_xml, &slide_rels));
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

fn parse_slide_xml(xml: &str, rels: &HashMap<String, String>) -> String {
    let mut reader = Reader::from_str(xml);
    reader.config_mut().trim_text(true);
    let mut buf = Vec::new();
    let mut html = String::new();

    let mut in_text_body = false;
    let mut in_run = false;
    let mut is_bold = false;
    let mut is_italic = false;
    let mut is_underline = false;
    let mut run_font_size: Option<f32> = None;
    let mut is_title = false;
    let mut is_subtitle = false;
    let mut current_text = String::new();
    let mut para_texts: Vec<String> = Vec::new();
    let mut is_list_item = false;
    let mut list_level: i32 = -1;
    let mut _para_alignment: Option<String> = None;
    // Table state
    let mut in_table = false;
    let mut _in_table_row = false;
    let mut in_table_cell = false;
    let mut first_table_row = true;
    let mut table_cell_text = String::new();
    // Image state
    let mut in_blip_fill = false;

    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Start(ref e)) | Ok(Event::Empty(ref e)) => {
                let qname = e.name();
                let local = local_name(qname.as_ref());
                match local {
                    "txBody" => {
                        in_text_body = true;
                    }
                    "p" if in_text_body => {
                        current_text.clear();
                        is_list_item = false;
                        _para_alignment = None;
                    }
                    "r" if in_text_body => {
                        in_run = true;
                        is_bold = false;
                        is_italic = false;
                        is_underline = false;
                        run_font_size = None;
                    }
                    "b" if in_text_body => {
                        // Check for val="0" (explicitly not bold)
                        let mut val_off = false;
                        for attr in e.attributes().flatten() {
                            let key = local_name(attr.key.as_ref());
                            if key == "val" {
                                let v = String::from_utf8_lossy(&attr.value);
                                if v == "0" || v == "false" {
                                    val_off = true;
                                }
                            }
                        }
                        if !val_off { is_bold = true; }
                    }
                    "i" if in_text_body => {
                        let mut val_off = false;
                        for attr in e.attributes().flatten() {
                            let key = local_name(attr.key.as_ref());
                            if key == "val" {
                                let v = String::from_utf8_lossy(&attr.value);
                                if v == "0" || v == "false" {
                                    val_off = true;
                                }
                            }
                        }
                        if !val_off { is_italic = true; }
                    }
                    "u" if in_text_body => {
                        for attr in e.attributes().flatten() {
                            let key = local_name(attr.key.as_ref());
                            if key == "val" {
                                let v = String::from_utf8_lossy(&attr.value);
                                if v != "none" {
                                    is_underline = true;
                                }
                            }
                        }
                    }
                    "sz" if in_text_body && in_run => {
                        // Font size in hundredths of a point
                        for attr in e.attributes().flatten() {
                            let key = local_name(attr.key.as_ref());
                            if key == "val" {
                                if let Ok(hp) = String::from_utf8_lossy(&attr.value).parse::<f32>() {
                                    run_font_size = Some(hp / 100.0);
                                }
                            }
                        }
                    }
                    "buChar" | "buAutoNum" => {
                        is_list_item = true;
                    }
                    // Image: a:blip with r:embed
                    "blipFill" => {
                        in_blip_fill = true;
                    }
                    "blip" if in_blip_fill => {
                        for attr in e.attributes().flatten() {
                            let key = local_name(attr.key.as_ref());
                            if key == "embed" {
                                let rid = String::from_utf8_lossy(&attr.value).to_string();
                                if let Some(filename) = rels.get(&rid) {
                                    html.push_str(&format!(
                                        "<img src=\"images/{}\" alt=\"{}\">\n",
                                        html_escape::encode_text(filename),
                                        html_escape::encode_text(filename)
                                    ));
                                }
                            }
                        }
                    }
                    // Table support
                    "tbl" if !in_text_body => {
                        in_table = true;
                        first_table_row = true;
                        html.push_str("<table>\n<thead>\n");
                    }
                    "tr" if in_table => {
                        _in_table_row = true;
                        html.push_str("<tr>");
                    }
                    "tc" if in_table => {
                        in_table_cell = true;
                        table_cell_text.clear();
                        let cell_tag = if first_table_row { "th" } else { "td" };
                        html.push_str(&format!("<{}>", cell_tag));
                    }
                    "pPr" if in_text_body => {
                        for attr in e.attributes().flatten() {
                            let key = local_name(attr.key.as_ref());
                            if key == "lvl" {
                                list_level = String::from_utf8_lossy(&attr.value)
                                    .parse()
                                    .unwrap_or(0);
                            }
                            if key == "algn" {
                                _para_alignment = Some(
                                    String::from_utf8_lossy(&attr.value).to_lowercase()
                                );
                            }
                        }
                    }
                    "ph" => {
                        for attr in e.attributes().flatten() {
                            let key = local_name(attr.key.as_ref());
                            if key == "type" {
                                let val = String::from_utf8_lossy(&attr.value);
                                if val == "title" || val == "ctrTitle" {
                                    is_title = true;
                                } else if val == "subTitle" {
                                    is_subtitle = true;
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
                        // Build inline style
                        let mut style_parts = Vec::new();
                        if let Some(fs) = run_font_size {
                            if (fs - 12.0).abs() > 0.5 {
                                style_parts.push(format!("font-size:{:.0}pt", fs));
                            }
                        }
                        let mut segment = if style_parts.is_empty() {
                            escaped
                        } else {
                            format!("<span style=\"{}\">{}</span>", style_parts.join(";"), escaped)
                        };
                        if is_bold {
                            segment = format!("<strong>{}</strong>", segment);
                        }
                        if is_italic {
                            segment = format!("<em>{}</em>", segment);
                        }
                        if is_underline {
                            segment = format!("<u>{}</u>", segment);
                        }
                        if in_table_cell {
                            table_cell_text.push_str(&segment);
                        } else {
                            current_text.push_str(&segment);
                        }
                    }
                }
            }
            Ok(Event::End(ref e)) => {
                let qname = e.name();
                let local = local_name(qname.as_ref());
                match local {
                    "txBody" => {
                        in_text_body = false;
                        if !para_texts.is_empty() {
                            if is_title {
                                for t in &para_texts {
                                    if !t.trim().is_empty() {
                                        html.push_str(&format!("<h1>{}</h1>\n", t));
                                    }
                                }
                                is_title = false;
                            } else if is_subtitle {
                                for t in &para_texts {
                                    if !t.trim().is_empty() {
                                        html.push_str(&format!("<h2>{}</h2>\n", t));
                                    }
                                }
                                is_subtitle = false;
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
                        is_list_item = false;
                        list_level = -1;
                    }
                    "r" => {
                        in_run = false;
                    }
                    "blipFill" => {
                        in_blip_fill = false;
                    }
                    "tc" if in_table => {
                        in_table_cell = false;
                        html.push_str(&table_cell_text);
                        let cell_tag = if first_table_row { "th" } else { "td" };
                        html.push_str(&format!("</{}>", cell_tag));
                        table_cell_text.clear();
                    }
                    "tr" if in_table => {
                        _in_table_row = false;
                        html.push_str("</tr>\n");
                        if first_table_row {
                            first_table_row = false;
                            html.push_str("</thead>\n<tbody>\n");
                        }
                    }
                    "tbl" if in_table => {
                        in_table = false;
                        if !first_table_row {
                            html.push_str("</tbody>\n");
                        } else {
                            html.push_str("</thead>\n");
                        }
                        html.push_str("</table>\n");
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

/// Parse slide-level relationships (ppt/slides/_rels/slideN.xml.rels)
/// Maps rId → media filename (e.g. "rId2" → "image1.png")
fn parse_pptx_relationships(xml: &str) -> HashMap<String, String> {
    let mut map = HashMap::new();
    let mut reader = Reader::from_str(xml);
    reader.config_mut().trim_text(true);
    let mut buf = Vec::new();

    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Start(ref e)) | Ok(Event::Empty(ref e)) => {
                let qname = e.name();
                let local = local_name(qname.as_ref());
                if local == "Relationship" {
                    let mut rid: Option<String> = None;
                    let mut target: Option<String> = None;
                    for attr in e.attributes().flatten() {
                        let key = std::str::from_utf8(attr.key.as_ref()).unwrap_or("");
                        match key {
                            "Id" => rid = Some(String::from_utf8_lossy(&attr.value).to_string()),
                            "Target" => target = Some(String::from_utf8_lossy(&attr.value).to_string()),
                            _ => {}
                        }
                    }
                    if let (Some(id), Some(tgt)) = (rid, target) {
                        // Target is relative: ../media/image1.png
                        if tgt.contains("media/") {
                            let filename = tgt.rsplit('/').next().unwrap_or(&tgt).to_string();
                            map.insert(id, filename);
                        }
                    }
                }
            }
            Ok(Event::Eof) => break,
            Err(_) => break,
            _ => {}
        }
        buf.clear();
    }

    map
}

fn local_name(name: &[u8]) -> &str {
    let s = std::str::from_utf8(name).unwrap_or("");
    s.rsplit(':').next().unwrap_or(s)
}
