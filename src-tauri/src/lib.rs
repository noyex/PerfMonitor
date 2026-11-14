//! PerfMonitor window: serves the static UI from embedded assets and talks to the Python API over HTTP.
//! The collector, analytics and CLI stay in Python; this crate only owns the window and the API server process.

use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{Manager, RunEvent, WebviewWindowBuilder};

const DEFAULT_PORT: u16 = 8765;

/// The API server this app started itself. Dropped (and killed) on exit; a server that was already running is
/// never touched.
struct SpawnedServer {
    child: Child,
    #[cfg(windows)]
    _job: job::Job,
}

struct ApiServer(Mutex<Option<SpawnedServer>>);

/// `PERFMON_ROOT`, else the first directory above the exe that holds `perfmon\__init__.py`.
fn find_root() -> Option<PathBuf> {
    if let Some(root) = std::env::var_os("PERFMON_ROOT").filter(|r| !r.is_empty()) {
        return Some(PathBuf::from(root));
    }
    let exe = std::env::current_exe().ok()?;
    exe.parent()?
        .ancestors()
        .find(|dir| dir.join("perfmon").join("__init__.py").is_file())
        .map(Path::to_path_buf)
}

/// `ui_port` from `<data dir>\config.json` (data dir: `PERFMON_DATA_DIR`, else `<root>\data`, like perfmon/config.py).
fn ui_port(root: Option<&Path>) -> u16 {
    let data_dir = match std::env::var_os("PERFMON_DATA_DIR").filter(|d| !d.is_empty()) {
        Some(dir) => PathBuf::from(dir),
        None => match root {
            Some(root) => root.join("data"),
            None => return DEFAULT_PORT,
        },
    };
    std::fs::read_to_string(data_dir.join("config.json"))
        .ok()
        .and_then(|text| serde_json::from_str::<serde_json::Value>(&text).ok())
        .and_then(|cfg| cfg.get("ui_port")?.as_u64())
        .and_then(|port| u16::try_from(port).ok())
        .filter(|port| *port != 0)
        .unwrap_or(DEFAULT_PORT)
}

fn port_open(port: u16) -> bool {
    TcpStream::connect_timeout(&SocketAddr::from(([127, 0, 0, 1], port)), Duration::from_millis(500)).is_ok()
}

/// Starts `<root>\.venv\Scripts\pythonw.exe -m perfmon.ui.server <port>` hidden, when the venv exists.
fn spawn_server(root: &Path, port: u16) -> Option<SpawnedServer> {
    let python = root.join(".venv").join("Scripts").join("pythonw.exe");
    if !python.is_file() {
        return None;
    }
    let mut cmd = Command::new(python);
    cmd.args(["-m", "perfmon.ui.server", &port.to_string()])
        .current_dir(root)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    let mut child = cmd.spawn().ok()?;
    // The venv's pythonw.exe is a launcher that starts the real interpreter as its child: a job object takes both
    // down, also when this process is killed.
    #[cfg(windows)]
    let job = match job::Job::with(&child) {
        Some(job) => job,
        None => {
            let _ = child.kill();
            return None;
        }
    };
    Some(SpawnedServer {
        child,
        #[cfg(windows)]
        _job: job,
    })
}

#[cfg(windows)]
mod job {
    use std::os::windows::io::AsRawHandle;
    use std::process::Child;
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation, SetInformationJobObject,
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    /// A job object that kills every process in it when the last handle closes.
    pub struct Job(HANDLE);

    // SAFETY: a job handle is only closed in Drop; the kernel serialises everything else.
    unsafe impl Send for Job {}

    impl Job {
        pub fn with(child: &Child) -> Option<Job> {
            // SAFETY: plain Win32 calls with valid arguments; the handle is owned by `Job`, so it closes on every path.
            unsafe {
                let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
                if handle as usize == 0 {
                    return None;
                }
                let job = Job(handle);
                let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                let set = SetInformationJobObject(
                    handle,
                    JobObjectExtendedLimitInformation,
                    &info as *const _ as *const _,
                    std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                );
                if set == 0 || AssignProcessToJobObject(handle, child.as_raw_handle() as HANDLE) == 0 {
                    return None;
                }
                Some(job)
            }
        }
    }

    impl Drop for Job {
        fn drop(&mut self) {
            // SAFETY: the handle was created in `with` and is closed once.
            unsafe { CloseHandle(self.0) };
        }
    }
}

pub fn run() {
    tauri::Builder::default()
        // Must be first: a second launch focuses the running window and exits.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .setup(|app| {
            let root = find_root();
            let port = ui_port(root.as_deref());

            // The window comes first: the page paints from embedded assets while the API server may still start.
            let config = app
                .config()
                .app
                .windows
                .first()
                .cloned()
                .ok_or("tauri.conf.json defines no window")?;
            WebviewWindowBuilder::from_config(app.handle(), &config)?
                .initialization_script(format!("window.__PERFMON_API__ = \"http://127.0.0.1:{port}\";"))
                .build()?;

            let spawned = match &root {
                Some(root) if !port_open(port) => spawn_server(root, port),
                _ => None,
            };
            app.manage(ApiServer(Mutex::new(spawned)));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building PerfMonitor")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                let server = app.state::<ApiServer>().0.lock().ok().and_then(|mut s| s.take());
                if let Some(mut server) = server {
                    let _ = server.child.kill();
                }
            }
        });
}
