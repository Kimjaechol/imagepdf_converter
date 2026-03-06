#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod backend;
mod commands;
mod document;
mod moa;

use tauri::{Emitter, Manager};

fn main() {
    // Set up file logging so we can diagnose launch failures
    let log_dir = dirs_next::data_local_dir()
        .unwrap_or_else(|| std::path::PathBuf::from("."))
        .join("MoA-DocConverter")
        .join("logs");
    let _ = std::fs::create_dir_all(&log_dir);
    let log_file = log_dir.join("app.log");

    let file_appender = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_file);

    if let Ok(writer) = file_appender {
        tracing_subscriber::fmt()
            .with_writer(std::sync::Mutex::new(writer))
            .with_ansi(false)
            .init();
    } else {
        tracing_subscriber::fmt::init();
    }

    tracing::info!("=== App starting ===");
    tracing::info!("Log file: {:?}", log_file);

    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_process::init())
        .setup(|app| {
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                if let Err(e) = backend::process::start_backend(&handle).await {
                    tracing::error!("Failed to start backend: {}", e);
                    // Show error dialog so user knows what happened
                    let msg = format!(
                        "백엔드 서버 시작에 실패했습니다.\n\n오류: {}\n\n\
                         Python이 올바르게 번들되었는지 확인해 주세요.",
                        e
                    );
                    if let Some(window) = handle.get_webview_window("main") {
                        let _ = window.emit("backend-error", &msg);
                    }
                    tracing::error!("Backend error details: {}", msg);
                }
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            // File operations
            commands::file_ops::get_backend_url,
            commands::file_ops::read_file_content,
            commands::file_ops::write_file_content,
            // Conversion
            commands::conversion::convert_pdf,
            commands::conversion::convert_batch,
            commands::conversion::convert_document,
            commands::conversion::get_job_status,
            commands::conversion::list_jobs,
            // Config
            commands::config::get_config,
            commands::config::update_config,
            commands::config::add_dictionary_term,
            // Document conversion (Rust-native)
            commands::document_cmd::convert_docx_to_html,
            commands::document_cmd::convert_hwpx_to_html,
            commands::document_cmd::convert_xlsx_to_html,
            commands::document_cmd::convert_pptx_to_html,
            commands::document_cmd::convert_any_document,
            // MoA gateway
            commands::moa_cmd::moa_convert,
            commands::moa_cmd::moa_health,
            commands::moa_cmd::moa_supported_formats,
            commands::moa_cmd::moa_tool_manifest,
            // Backend lifecycle
            commands::backend_cmd::restart_backend,
            commands::backend_cmd::backend_health,
            // Credits & API key
            commands::credit_cmd::set_api_key,
            commands::credit_cmd::get_api_key_status,
            commands::credit_cmd::get_credits,
            commands::credit_cmd::purchase_credits,
            commands::credit_cmd::estimate_cost,
            commands::credit_cmd::get_credit_history,
        ])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                let handle = window.app_handle().clone();
                // Use thread to ensure cleanup completes before exit
                std::thread::spawn(move || {
                    let rt = tokio::runtime::Builder::new_current_thread()
                        .enable_all()
                        .build()
                        .unwrap();
                    rt.block_on(backend::process::stop_backend(&handle));
                });
            }
        })
        .run(tauri::generate_context!())
        .unwrap_or_else(|e| {
            tracing::error!("Tauri application failed to start: {}", e);
            eprintln!("error while running tauri application: {}", e);
        });
    tracing::info!("=== App exiting ===");
}
