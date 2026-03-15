#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod backend;
mod commands;
mod document;
mod moa;

use std::sync::Mutex;
use tauri::{Emitter, Manager};

/// Holds the auth token so Tauri commands can attach it to backend requests.
pub struct AuthToken(pub Mutex<String>);

fn show_error_msgbox(title: &str, msg: &str) {
    #[cfg(windows)]
    {
        use std::ffi::OsStr;
        use std::os::windows::ffi::OsStrExt;
        use std::iter::once;
        let title_wide: Vec<u16> = OsStr::new(title).encode_wide().chain(once(0)).collect();
        let msg_wide: Vec<u16> = OsStr::new(msg).encode_wide().chain(once(0)).collect();
        unsafe {
            extern "system" {
                fn MessageBoxW(hwnd: *mut std::ffi::c_void, text: *const u16, caption: *const u16, utype: u32) -> i32;
            }
            MessageBoxW(std::ptr::null_mut(), msg_wide.as_ptr(), title_wide.as_ptr(), 0x10);
        }
    }
    #[cfg(not(windows))]
    {
        eprintln!("{}: {}", title, msg);
    }
}

/// Simple timestamp without chrono dependency
fn chrono_simple_now() -> String {
    use std::time::SystemTime;
    let secs = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    format!("[timestamp={}]", secs)
}

/// Determine the best log directory. Falls back to exe directory if AppData is unavailable.
fn resolve_log_dir() -> std::path::PathBuf {
    // Primary: AppData\Local\MoA-DocConverter\logs
    if let Some(local) = dirs_next::data_local_dir() {
        let dir = local.join("MoA-DocConverter").join("logs");
        if std::fs::create_dir_all(&dir).is_ok() {
            return dir;
        }
    }
    // Fallback: next to the executable
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            let dir = parent.join("logs");
            if std::fs::create_dir_all(&dir).is_ok() {
                return dir;
            }
        }
    }
    // Last resort: current directory
    std::path::PathBuf::from(".")
}

/// Write a simple text file next to the exe to confirm the binary at least starts loading.
/// Returns a status message for later logging.
fn write_early_diagnostic() -> String {
    let exe_path = match std::env::current_exe() {
        Ok(p) => p,
        Err(e) => return format!("cannot get exe path: {}", e),
    };
    let exe_dir = match exe_path.parent() {
        Some(d) => d,
        None => return "cannot get exe parent dir".to_string(),
    };
    let diag_file = exe_dir.join("moa_diagnostic.txt");
    let info = format!(
        "MoA Document Converter - Diagnostic\n\
         Exe: {:?}\n\
         Dir: {:?}\n\
         data_local_dir: {:?}\n\
         Status: main() entered successfully\n",
        exe_path,
        std::env::current_dir().ok(),
        dirs_next::data_local_dir(),
    );
    match std::fs::write(&diag_file, &info) {
        Ok(_) => format!("wrote {:?}", diag_file),
        Err(e) => format!("failed to write {:?}: {}", diag_file, e),
    }
}

fn main() {
    // Write an early diagnostic file BEFORE anything else.
    // If the app crashes before tracing is set up, this file helps diagnose the issue.
    let early_diag = write_early_diagnostic();

    // Catch panics and show message box
    std::panic::set_hook(Box::new(|info| {
        let msg = format!("앱에서 예기치 않은 오류가 발생했습니다:\n\n{}", info);
        show_error_msgbox("MoA 문서 변환기 - 오류", &msg);
        // Also try to write panic info to diagnostic file
        if let Ok(exe) = std::env::current_exe() {
            let panic_file = exe.parent().unwrap_or(std::path::Path::new(".")).join("moa_panic.log");
            let _ = std::fs::write(&panic_file, format!("{}\n{}", chrono_simple_now(), msg));
        }
    }));

    // Set up file logging so we can diagnose launch failures.
    // Try AppData\Local first, fall back to exe directory.
    let log_dir = resolve_log_dir();
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
        // If log file fails, try stderr
        tracing_subscriber::fmt::init();
    }

    tracing::info!("=== App starting ===");
    tracing::info!("Log file: {:?}", log_file);
    tracing::info!("Early diagnostic: {}", early_diag);
    tracing::info!("Exe path: {:?}", std::env::current_exe());
    tracing::info!("Current dir: {:?}", std::env::current_dir());
    tracing::info!("data_local_dir: {:?}", dirs_next::data_local_dir());

    tauri::Builder::default()
        .manage(AuthToken(Mutex::new(String::new())))
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
            commands::file_ops::open_path_native,
            commands::file_ops::open_editor_window,
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
            commands::document_cmd::html_to_markdown,
            // MoA gateway
            commands::moa_cmd::moa_convert,
            commands::moa_cmd::moa_health,
            commands::moa_cmd::moa_supported_formats,
            commands::moa_cmd::moa_tool_manifest,
            // Backend lifecycle
            commands::backend_cmd::restart_backend,
            commands::backend_cmd::backend_health,
            // Auth
            commands::credit_cmd::auth_register,
            commands::credit_cmd::auth_login,
            commands::credit_cmd::auth_get_me,
            commands::credit_cmd::set_auth_token,
            commands::credit_cmd::auth_refresh_token,
            // API key status (read-only) & Credits
            commands::credit_cmd::get_api_key_status,
            commands::credit_cmd::get_upstage_api_key_status,
            commands::credit_cmd::get_exchange_rate,
            commands::credit_cmd::get_credits,
            commands::credit_cmd::purchase_credits,
            commands::credit_cmd::estimate_cost,
            commands::credit_cmd::get_pricing,
            commands::credit_cmd::get_credit_history,
            commands::credit_cmd::create_checkout,
            // R2 Upload & Image PDF parsing
            commands::credit_cmd::r2_status,
            commands::credit_cmd::r2_presigned_upload,
            commands::credit_cmd::parse_image_pdf,
            // Local LLM correction
            commands::credit_cmd::correct_with_llm,
        ])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                // Only stop the backend when the main window is closed, not the editor window
                if window.label() == "main" {
                    let handle = window.app_handle().clone();
                    // Use thread to ensure cleanup completes before exit
                    std::thread::spawn(move || {
                        match tokio::runtime::Builder::new_current_thread()
                            .enable_all()
                            .build()
                        {
                            Ok(rt) => {
                                rt.block_on(backend::process::stop_backend(&handle));
                            }
                            Err(e) => {
                                tracing::error!("Failed to create runtime for backend shutdown: {}", e);
                            }
                        }
                    });
                }
            }
        })
        .run(tauri::generate_context!())
        .unwrap_or_else(|e| {
            let msg = format!("앱을 시작할 수 없습니다:\n\n{}", e);
            tracing::error!("{}", msg);
            show_error_msgbox("MoA 문서 변환기 - 시작 실패", &msg);
        });
    tracing::info!("=== App exiting ===");
}
