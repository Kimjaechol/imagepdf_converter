use std::path::PathBuf;
use std::process::Stdio;
use std::sync::OnceLock;
use tauri::{AppHandle, Manager};
use tokio::process::{Child, Command};
use tokio::sync::Mutex;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

static BACKEND_PROCESS: OnceLock<Mutex<Option<Child>>> = OnceLock::new();
static BACKEND_PORT: OnceLock<u16> = OnceLock::new();

fn get_process_mutex() -> &'static Mutex<Option<Child>> {
    BACKEND_PROCESS.get_or_init(|| Mutex::new(None))
}

pub fn get_port() -> u16 {
    *BACKEND_PORT.get_or_init(|| portpicker::pick_unused_port().unwrap_or(8765))
}

/// Resolve the resource directory, falling back to current dir
fn resolve_resource_dir(app: &AppHandle) -> PathBuf {
    app.path()
        .resource_dir()
        .unwrap_or_else(|_| std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")))
}

/// Find Python executable - bundled portable or system
fn find_python(app: &AppHandle) -> PathBuf {
    let resource_dir = resolve_resource_dir(app);

    let python_bin = if cfg!(windows) { "python.exe" } else { "bin/python3" };

    // Tauri v2 maps "../" resource paths to "_up_/" in the bundle.
    // So "../build_output/portable_python/**/*" becomes "_up_/build_output/portable_python/..."
    let candidates = [
        // 1. Tauri v2 bundled path (_up_/ prefix from ../ in tauri.conf.json resources)
        resource_dir.join("_up_").join("build_output").join("portable_python").join(python_bin),
        // 2. Direct path (in case resources are flattened)
        resource_dir.join("portable_python").join(python_bin),
        // 3. Under build_output/ without _up_
        resource_dir.join("build_output").join("portable_python").join(python_bin),
        // 4. Dev mode: project_root/build_output/portable_python/
        resource_dir.join("..").join("build_output").join("portable_python").join(python_bin),
    ];

    for candidate in &candidates {
        if candidate.exists() {
            tracing::info!("Using Python: {:?}", candidate);
            return candidate.clone();
        }
        tracing::debug!("Python not found at: {:?}", candidate);
    }

    // Fallback: System Python
    let system_python = if cfg!(windows) { "python" } else { "python3" };
    tracing::warn!("No bundled Python found, falling back to system: {}", system_python);
    PathBuf::from(system_python)
}

/// Find the backend directory containing backend/ and config/
fn find_backend_dir(app: &AppHandle) -> PathBuf {
    let resource_dir = resolve_resource_dir(app);

    // Tauri v2 maps "../backend/**/*.py" to "_up_/backend/..." in the bundle
    let candidates = [
        // 1. Tauri v2 bundled path (_up_/ prefix)
        resource_dir.join("_up_"),
        // 2. Direct resource dir (resources/backend/)
        resource_dir.clone(),
        // 3. Dev: project root (resource_dir/../)
        resource_dir.join(".."),
    ];

    for candidate in &candidates {
        if candidate.join("backend").exists() {
            tracing::info!("Using backend dir: {:?}", candidate);
            return candidate.clone();
        }
        tracing::debug!("backend/ not found at: {:?}", candidate);
    }

    // Fallback: current directory
    tracing::warn!("No backend dir found, falling back to current dir");
    std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
}

pub async fn start_backend(app: &AppHandle) -> Result<(), String> {
    let python = find_python(app);
    let backend_dir = find_backend_dir(app);
    let port = get_port();

    let config_path = backend_dir.join("config").join("pipeline_config.yaml");

    tracing::info!(
        "Starting backend: python={:?} (exists={}), dir={:?} (exists={}), port={}",
        python, python.exists(), backend_dir, backend_dir.exists(), port
    );
    tracing::info!("Resource dir: {:?}", resolve_resource_dir(app));

    let mut cmd = Command::new(&python);
    cmd.args([
            "-m", "uvicorn", "backend.server:app",
            "--host", "127.0.0.1",
            "--port", &port.to_string(),
        ])
        .current_dir(&backend_dir)
        .env("PIPELINE_CONFIG", &config_path)
        .env("PORT", port.to_string())
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);

    // Hide the console window on Windows
    #[cfg(windows)]
    {
        #[allow(unused_imports)]
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

    let child = cmd
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
