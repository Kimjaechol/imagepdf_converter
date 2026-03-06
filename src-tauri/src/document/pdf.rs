use super::converter::DocMeta;
use lopdf::Document;

/// PDF → HTML converter (Rust-native, basic text extraction)
/// This is a fallback for when the Python AI backend is not available.
/// It extracts text from text-based PDFs but cannot handle image-only/scanned PDFs.
pub async fn convert_to_html(
    path: &str,
) -> Result<(String, Vec<(String, Vec<u8>)>, DocMeta), String> {
    let path = path.to_string();
    tokio::task::spawn_blocking(move || convert_sync(&path))
        .await
        .map_err(|e| format!("Task failed: {}", e))?
}

fn convert_sync(path: &str) -> Result<(String, Vec<(String, Vec<u8>)>, DocMeta), String> {
    let doc =
        Document::load(path).map_err(|e| format!("Cannot open PDF: {}", e))?;

    let page_count = doc.get_pages().len() as u32;

    let mut meta = DocMeta::default();
    meta.page_count = Some(page_count);

    // Try to extract title from document info
    if let Ok(info) = doc.trailer.get(b"Info") {
        if let Ok(info_ref) = info.as_reference() {
            if let Ok(info_dict) = doc.get_dictionary(info_ref) {
                if let Ok(title) = info_dict.get(b"Title") {
                    if let Ok(t) = title.as_str() {
                        let t = String::from_utf8_lossy(t);
                        let t = t.trim();
                        if !t.is_empty() {
                            meta.title = Some(t.to_string());
                        }
                    }
                }
                if let Ok(author) = info_dict.get(b"Author") {
                    if let Ok(a) = author.as_str() {
                        let a = String::from_utf8_lossy(a);
                        let a = a.trim();
                        if !a.is_empty() {
                            meta.author = Some(a.to_string());
                        }
                    }
                }
            }
        }
    }

    // Extract text from each page
    let mut pages_html = Vec::new();
    let mut sorted_pages: Vec<_> = doc.get_pages().into_iter().collect();
    sorted_pages.sort_by_key(|(num, _)| *num);

    for (page_num, _page_id) in &sorted_pages {
        let text = doc.extract_text(&[*page_num]).unwrap_or_default();
        let text = text.trim();

        if text.is_empty() {
            pages_html.push(format!(
                "<div class=\"pdf-page\" data-page=\"{}\">\
                 <p class=\"pdf-no-text\">[페이지 {} - 텍스트를 추출할 수 없습니다. \
                 이미지 기반 PDF는 Python AI 백엔드가 필요합니다.]</p>\
                 </div>",
                page_num, page_num
            ));
            continue;
        }

        // Convert text to HTML paragraphs
        let paragraphs: Vec<String> = text
            .split('\n')
            .map(|line| line.trim())
            .filter(|line| !line.is_empty())
            .map(|line| {
                let escaped = html_escape::encode_text(line);
                format!("<p>{}</p>", escaped)
            })
            .collect();

        pages_html.push(format!(
            "<div class=\"pdf-page\" data-page=\"{}\">\n{}\n</div>",
            page_num,
            paragraphs.join("\n")
        ));
    }

    let title_str = meta.title.as_deref().unwrap_or("PDF Document");
    let title_escaped = html_escape::encode_text(title_str);

    let html = format!(
        r#"<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<style>
body {{ font-family: 'Malgun Gothic', 'Apple SD Gothic Neo', sans-serif; margin: 2em; line-height: 1.8; color: #333; }}
.pdf-page {{ margin-bottom: 2em; padding-bottom: 1em; border-bottom: 1px solid #ddd; }}
.pdf-page p {{ margin: 0.5em 0; }}
.pdf-no-text {{ color: #999; font-style: italic; }}
.pdf-notice {{ background: #fff3cd; border: 1px solid #ffc107; padding: 1em; margin-bottom: 2em; border-radius: 4px; }}
</style>
</head>
<body>
<div class="pdf-notice">
이 문서는 기본 텍스트 추출 모드로 변환되었습니다.
AI 레이아웃 분석이 필요한 경우 Python 백엔드를 설정해 주세요.
</div>
{content}
</body>
</html>"#,
        title = title_escaped,
        content = pages_html.join("\n"),
    );

    // No images extracted in basic mode
    let images: Vec<(String, Vec<u8>)> = Vec::new();

    Ok((html, images, meta))
}
