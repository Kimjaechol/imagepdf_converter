use std::path::PathBuf;
use std::process::Stdio;
use std::sync::OnceLock;
use tauri::AppHandle;
use tokio::process::{Child, Command};
use tokio::sync::Mutex;

static BACKEND_PROCESS: OnceLock<Mutex<Option<Child>>> = OnceLock::new();
static BACKEND_PORT: OnceLock<u16> = OnceLock::new();

fn get_process_mutex() -> &'static Mutex<Option<Child>> {
    BACKEND_PROCESS.get_or_init(|| Mutex::new(None))
}

pub fn get_port() -> u16 {
    *BACKEND_PORT.get_or_init(|| portpicker::pick_unused_port().unwrap_or(8765))
}

/// Find Python executable - bundled portable or system
fn find_python(app: &AppHandle) -> PathBuf {
    let resource_dir = app
        .path()
        .resource_dir()
        .unwrap_or_default();

    // 1. Bundled portable Python (packaged app)
    let bundled = resource_dir.join("portable_python").join(if cfg!(windows) {
        "python.exe"
    } else {
        "bin/python3"
    });
    if bundled.exists() {
        tracing::info!("Using bundled Python: {:?}", bundled);
        return bundled;
    }

    // 2. Portable Python in build_output (dev mode)
    let dev_portable = resource_dir
        .join("..")
        .join("build_output")
        .join("portable_python")
        .join(if cfg!(windows) { "python.exe" } else { "bin/python3" });
    if dev_portable.exists() {
        tracing::info!("Using dev portable Python: {:?}", dev_portable);
        return dev_portable;
    }

    // 3. System Python
    let system_python = if cfg!(windows) { "python" } else { "python3" };
    tracing::info!("Using system Python: {}", system_python);
    PathBuf::from(system_python)
}

/// Find the backend directory
fn find_backend_dir(app: &AppHandle) -> PathBuf {
    let resource_dir = app
        .path()
        .resource_dir()
        .unwrap_or_default();

    // Packaged: resources/backend/
    let packaged = resource_dir.join("backend");
    if packaged.exists() {
        return resource_dir.to_path_buf();
    }

    // Dev: project root
    let dev = resource_dir.join("..").join("backend");
    if dev.exists() {
        return resource_dir.join("..");
    }

    // Fallback: current directory
    std::env::current_dir().unwrap_or_default()
}

pub async fn start_backend(app: &AppHandle) -> Result<(), String> {
    let python = find_python(app);
    let backend_dir = find_backend_dir(app);
    let port = get_port();

    let config_path = backend_dir.join("config").join("pipeline_config.yaml");

    tracing::info!(
        "Starting backend: python={:?}, dir={:?}, port={}",
        python,
        backend_dir,
        port
    );

    let child = Command::new(&python)
        .args([
            "-m",
            "uvicorn",
            "backend.server:app",
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
        ])
        .current_dir(&backend_dir)
        .env("PIPELINE_CONFIG", &config_path)
        .env("PORT", port.to_string())
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .map_err(|e| format!("Failed to spawn Python backend: {}", e))?;

    tracing::info!("Backend process started (PID: {:?})", child.id());

    let mut lock = get_process_mutex().lock().await;
    *lock = Some(child);

    // Wait for backend to be healthy
    wait_for_health(port, 60).await?;
    tracing::info!("Backend is healthy on port {}", port);

    Ok(())
}

pub async fn stop_backend(_app: &AppHandle) {
    let mut lock = get_process_mutex().lock().await;
    if let Some(mut child) = lock.take() {
        tracing::info!("Stopping backend process...");
        let _ = child.kill().await;
        tracing::info!("Backend stopped");
    }
}

pub async fn restart_backend(app: &AppHandle) -> Result<(), String> {
    stop_backend(app).await;
    tokio::time::sleep(std::time::Duration::from_secs(1)).await;
    start_backend(app).await
}

pub async fn health_check() -> bool {
    let port = get_port();
    let url = format!("http://127.0.0.1:{}/api/health", port);
    matches!(reqwest::get(&url).await, Ok(resp) if resp.status().is_success())
}

async fn wait_for_health(port: u16, max_retries: u32) -> Result<(), String> {
    let url = format!("http://127.0.0.1:{}/api/health", port);
    for i in 0..max_retries {
        tokio::time::sleep(std::time::Duration::from_secs(1)).await;
        if let Ok(resp) = reqwest::get(&url).await {
            if resp.status().is_success() {
                return Ok(());
            }
        }
        if i % 10 == 9 {
            tracing::info!("Still waiting for backend... ({}s)", i + 1);
        }
    }
    Err("Backend health check timed out".to_string())
}
