use super::converter::DocMeta;
use calamine::{open_workbook, Reader, Xlsx};

/// XLSX → HTML converter (Rust-native using calamine)
pub async fn convert_to_html(
    path: &str,
) -> Result<(String, Vec<(String, Vec<u8>)>, DocMeta), String> {
    let path = path.to_string();
    tokio::task::spawn_blocking(move || convert_sync(&path))
        .await
        .map_err(|e| format!("Task failed: {}", e))?
}

fn convert_sync(path: &str) -> Result<(String, Vec<(String, Vec<u8>)>, DocMeta), String> {
    let mut workbook: Xlsx<_> =
        open_workbook(path).map_err(|e| format!("Cannot open XLSX: {}", e))?;

    let sheet_names = workbook.sheet_names().to_vec();
    let mut meta = DocMeta::default();
    meta.page_count = Some(sheet_names.len() as u32);
    meta.title = Some(
        std::path::Path::new(path)
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("스프레드시트")
            .to_string(),
    );

    let mut body_html = String::new();

    for (idx, sheet_name) in sheet_names.iter().enumerate() {
        if idx > 0 {
            body_html.push_str("<div class=\"page-break\"></div>\n");
        }

        body_html.push_str(&format!(
            "<h2>{}</h2>\n",
            html_escape::encode_text(sheet_name)
        ));

        if let Ok(range) = workbook.worksheet_range(sheet_name) {
            let (rows, cols) = range.get_size();
            if rows == 0 || cols == 0 {
                body_html.push_str("<p><em>(빈 시트)</em></p>\n");
                continue;
            }

            body_html.push_str("<table>\n");

            // First row as header
            let mut first_row = true;
            for row in range.rows() {
                body_html.push_str("<tr>");
                let tag = if first_row { "th" } else { "td" };
                for cell in row {
                    let text = cell_to_string(cell);
                    body_html.push_str(&format!(
                        "<{tag}>{}</{tag}>",
                        html_escape::encode_text(&text)
                    ));
                }
                body_html.push_str("</tr>\n");
                first_row = false;
            }

            body_html.push_str("</table>\n");
        } else {
            body_html.push_str("<p><em>(시트 읽기 실패)</em></p>\n");
        }
    }

    let full_html = format!(
        r#"<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{}</title>
<style>
body {{ font-family: 'Malgun Gothic', '맑은 고딕', sans-serif; max-width: 100%; margin: 0 auto; padding: 20px; color: #333; }}
h2 {{ font-size: 18px; color: #2d5016; border-bottom: 2px solid #4CAF50; padding-bottom: 4px; margin-top: 32px; }}
table {{ border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 14px; }}
th {{ background: #4CAF50; color: white; padding: 8px 12px; text-align: left; font-weight: bold; white-space: nowrap; }}
td {{ border: 1px solid #ddd; padding: 6px 10px; }}
tr:nth-child(even) {{ background: #f9f9f9; }}
tr:hover {{ background: #f1f1f1; }}
.page-break {{ page-break-before: always; border-top: 2px dashed #ccc; margin: 32px 0; }}
</style>
</head>
<body>
{}
</body>
</html>"#,
        html_escape::encode_text(meta.title.as_deref().unwrap_or("스프레드시트")),
        body_html
    );

    Ok((full_html, vec![], meta))
}

fn cell_to_string(cell: &calamine::Data) -> String {
    match cell {
        calamine::Data::Empty => String::new(),
        calamine::Data::String(s) => s.clone(),
        calamine::Data::Float(f) => {
            if f.fract() == 0.0 && f.abs() < 1e15 {
                format!("{}", *f as i64)
            } else {
                format!("{:.2}", f)
            }
        }
        calamine::Data::Int(i) => format!("{}", i),
        calamine::Data::Bool(b) => if *b { "TRUE" } else { "FALSE" }.to_string(),
        calamine::Data::Error(e) => format!("#ERR:{:?}", e),
        calamine::Data::DateTime(dt) => format!("{}", dt),
        calamine::Data::DateTimeIso(s) => s.clone(),
        calamine::Data::DurationIso(s) => s.clone(),
    }
}
