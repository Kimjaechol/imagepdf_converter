use super::converter::DocMeta;
use quick_xml::events::Event;
use quick_xml::Reader;
use std::collections::HashMap;
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

    // Parse word/_rels/document.xml.rels for image relationship IDs
    // Maps rId (e.g. "rId5") → filename (e.g. "image1.png")
    let mut rel_to_image: HashMap<String, String> = HashMap::new();
    if let Ok(mut rels) = archive.by_name("word/_rels/document.xml.rels") {
        let mut rels_xml = String::new();
        let _ = rels.read_to_string(&mut rels_xml);
        rel_to_image = parse_relationships(&rels_xml);
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

    let html = parse_document_xml(&doc_xml, &images, &rel_to_image);

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

fn parse_document_xml(
    xml: &str,
    images: &[(String, Vec<u8>)],
    rel_to_image: &HashMap<String, String>,
) -> String {
    let mut reader = Reader::from_str(xml);
    reader.config_mut().trim_text(true);
    let mut html = String::new();
    let mut buf = Vec::new();

    let mut in_paragraph = false;
    let mut in_run = false;
    let mut is_bold = false;
    let mut is_italic = false;
    let mut is_underline = false;
    let mut run_font_size: Option<f32> = None;
    let mut run_color: Option<String> = None;
    let mut heading_level: Option<u8> = None;
    let mut para_alignment: Option<String> = None;
    let mut is_list_item = false;
    let mut _list_num_id: Option<String> = None;
    let mut in_table = false;
    let mut _in_table_row = false;
    let mut first_table_row = true;
    let mut in_table_cell = false;
    let mut cell_colspan: u32 = 1;
    let mut _cell_vmerge_restart = false;
    let mut cell_vmerge_continue = false;
    let mut text_buf = String::new();
    let mut image_idx = 0usize;
    let mut in_run_props = false;
    let mut in_para_props = false;
    let mut in_cell_props = false;
    let mut cell_tag_emitted = false;
    let mut in_drawing = false;
    let mut drawing_embed_id: Option<String> = None;

    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Start(ref e)) | Ok(Event::Empty(ref e)) => {
                let qname = e.name();
                let local = local_name(qname.as_ref());
                match local {
                    "p" => {
                        in_paragraph = true;
                        text_buf.clear();
                        heading_level = None;
                        para_alignment = None;
                        is_list_item = false;
                        _list_num_id = None;
                    }
                    "pPr" if in_paragraph => {
                        in_para_props = true;
                    }
                    "r" => {
                        in_run = true;
                        is_bold = false;
                        is_italic = false;
                        is_underline = false;
                        run_font_size = None;
                        run_color = None;
                    }
                    "rPr" if in_run => {
                        in_run_props = true;
                    }
                    "b" if in_run_props || in_para_props => {
                        // Check for val="0" (explicit not bold)
                        let val = get_attr_val(e, "val");
                        if val.as_deref() != Some("0") && val.as_deref() != Some("false") {
                            is_bold = true;
                        }
                    }
                    "i" if in_run_props || in_para_props => {
                        let val = get_attr_val(e, "val");
                        if val.as_deref() != Some("0") && val.as_deref() != Some("false") {
                            is_italic = true;
                        }
                    }
                    "u" if in_run_props || in_para_props => {
                        is_underline = true;
                    }
                    "sz" if in_run_props => {
                        // Font size in half-points
                        if let Some(val) = get_attr_val(e, "val") {
                            if let Ok(hp) = val.parse::<f32>() {
                                run_font_size = Some(hp / 2.0); // convert half-points to points
                            }
                        }
                    }
                    "color" if in_run_props => {
                        if let Some(val) = get_attr_val(e, "val") {
                            if val != "auto" && val.len() == 6 {
                                run_color = Some(format!("#{}", val));
                            }
                        }
                    }
                    "jc" if in_para_props => {
                        // Paragraph alignment
                        if let Some(val) = get_attr_val(e, "val") {
                            para_alignment = Some(val);
                        }
                    }
                    "numPr" if in_para_props => {
                        is_list_item = true;
                    }
                    "numId" if is_list_item => {
                        _list_num_id = get_attr_val(e, "val");
                    }
                    "pStyle" if in_para_props => {
                        if let Some(val) = get_attr_val(e, "val") {
                            let val_lower = val.to_lowercase();
                            if val_lower.contains("heading") || val_lower.contains("제목") {
                                let num: u8 = val_lower
                                    .chars()
                                    .filter(|c| c.is_ascii_digit())
                                    .collect::<String>()
                                    .parse()
                                    .unwrap_or(1);
                                heading_level = Some(num.clamp(1, 6));
                            } else if val_lower.contains("listparagraph") || val_lower.contains("목록") {
                                is_list_item = true;
                            }
                        }
                    }
                    "tbl" => {
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
                        cell_colspan = 1;
                        _cell_vmerge_restart = false;
                        cell_vmerge_continue = false;
                        cell_tag_emitted = false;
                    }
                    "tcPr" if in_table_cell => {
                        in_cell_props = true;
                    }
                    "gridSpan" if in_cell_props => {
                        if let Some(val) = get_attr_val(e, "val") {
                            cell_colspan = val.parse().unwrap_or(1);
                        }
                    }
                    "vMerge" if in_cell_props => {
                        let val = get_attr_val(e, "val");
                        if val.as_deref() == Some("restart") {
                            _cell_vmerge_restart = true;
                        } else {
                            // Continue merge (no val or val="continue")
                            cell_vmerge_continue = true;
                        }
                    }
                    "drawing" | "pict" => {
                        in_drawing = true;
                        drawing_embed_id = None;
                    }
                    // a:blip contains r:embed="rId5" pointing to the actual image
                    "blip" if in_drawing => {
                        for attr in e.attributes().flatten() {
                            let key = local_name(attr.key.as_ref());
                            if key == "embed" {
                                drawing_embed_id = Some(
                                    String::from_utf8_lossy(&attr.value).to_string()
                                );
                            }
                        }
                    }
                    "br" => {
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
                if in_run && in_paragraph {
                    let text = e.unescape().unwrap_or_default().to_string();
                    if !text.is_empty() {
                        let escaped = html_escape::encode_text(&text).to_string();
                        // Build inline style for run
                        let mut style_parts = Vec::new();
                        if let Some(fs) = run_font_size {
                            if (fs - 12.0).abs() > 0.5 {
                                style_parts.push(format!("font-size:{:.1}pt", fs));
                            }
                        }
                        if let Some(ref c) = run_color {
                            style_parts.push(format!("color:{}", c));
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
                        text_buf.push_str(&segment);
                    }
                }
            }
            Ok(Event::End(ref e)) => {
                let qname = e.name();
                let local = local_name(qname.as_ref());
                match local {
                    "p" => {
                        let has_content = !text_buf.trim().is_empty();
                        if has_content || in_table_cell {
                            if in_table_cell {
                                // Emit cell tag if tcPr was absent
                                if !cell_tag_emitted && !cell_vmerge_continue {
                                    let cell_tag = if first_table_row { "th" } else { "td" };
                                    html.push_str(&format!("<{}>", cell_tag));
                                    cell_tag_emitted = true;
                                }
                                html.push_str(&text_buf);
                            } else if let Some(level) = heading_level {
                                let align_attr = align_style_attr(para_alignment.as_deref());
                                html.push_str(&format!(
                                    "<h{l}{a}>{}</h{l}>\n",
                                    text_buf,
                                    l = level,
                                    a = align_attr
                                ));
                            } else if is_list_item && has_content {
                                html.push_str(&format!("<li>{}</li>\n", text_buf));
                            } else {
                                let align_attr = align_style_attr(para_alignment.as_deref());
                                html.push_str(&format!("<p{}>{}</p>\n", align_attr, text_buf));
                            }
                        }
                        text_buf.clear();
                        in_paragraph = false;
                        in_para_props = false;
                    }
                    "pPr" => {
                        in_para_props = false;
                    }
                    "r" => {
                        in_run = false;
                        in_run_props = false;
                    }
                    "rPr" => {
                        in_run_props = false;
                    }
                    "tcPr" => {
                        in_cell_props = false;
                        // Now emit the <td>/<th> tag with attributes
                        if cell_vmerge_continue {
                            // This cell is a continuation of a vertical merge; skip it
                        } else {
                            let cell_tag = if first_table_row { "th" } else { "td" };
                            let mut attrs = String::new();
                            if cell_colspan > 1 {
                                attrs.push_str(&format!(" colspan=\"{}\"", cell_colspan));
                            }
                            html.push_str(&format!("<{}{}>", cell_tag, attrs));
                            cell_tag_emitted = true;
                        }
                    }
                    "drawing" | "pict" => {
                        // Resolve image: prefer relationship ID, fallback to index
                        let img_name = drawing_embed_id
                            .as_ref()
                            .and_then(|rid| rel_to_image.get(rid))
                            .cloned()
                            .or_else(|| {
                                if image_idx < images.len() {
                                    let name = images[image_idx].0.clone();
                                    image_idx += 1;
                                    Some(name)
                                } else {
                                    None
                                }
                            });
                        if let Some(name) = img_name {
                            text_buf.push_str(&format!(
                                "<img src=\"images/{}\" alt=\"{}\">",
                                html_escape::encode_text(&name),
                                html_escape::encode_text(&name)
                            ));
                        }
                        in_drawing = false;
                        drawing_embed_id = None;
                    }
                    "tbl" => {
                        in_table = false;
                        // Close tbody if we already transitioned past header
                        if !first_table_row {
                            html.push_str("</tbody>\n");
                        } else {
                            // Table had no rows or only header; close thead
                            html.push_str("</thead>\n");
                        }
                        html.push_str("</table>\n");
                    }
                    "tr" if in_table => {
                        _in_table_row = false;
                        html.push_str("</tr>\n");
                        if first_table_row {
                            first_table_row = false;
                            html.push_str("</thead>\n<tbody>\n");
                        }
                    }
                    "tc" => {
                        in_table_cell = false;
                        in_cell_props = false;
                        if cell_tag_emitted {
                            let cell_tag = if first_table_row { "th" } else { "td" };
                            html.push_str(&format!("</{}>", cell_tag));
                        }
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

fn get_attr_val(e: &quick_xml::events::BytesStart, key: &str) -> Option<String> {
    for attr in e.attributes().flatten() {
        if local_name(attr.key.as_ref()) == key {
            return Some(String::from_utf8_lossy(&attr.value).to_string());
        }
    }
    None
}

fn align_style_attr(alignment: Option<&str>) -> String {
    match alignment {
        Some("center") => " style=\"text-align:center\"".to_string(),
        Some("right") | Some("end") => " style=\"text-align:right\"".to_string(),
        Some("both") | Some("distribute") => " style=\"text-align:justify\"".to_string(),
        _ => String::new(),
    }
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

/// Parse word/_rels/document.xml.rels to get rId → media filename mapping
fn parse_relationships(xml: &str) -> HashMap<String, String> {
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
                        if tgt.starts_with("media/") {
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
