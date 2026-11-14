//! PerfMonitor window: serves the static UI and talks to the Python API over HTTP.
//! The collector, analytics and CLI stay in Python; this crate only owns the window and its API server process.

use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};

use tauri::{Manager, RunEvent, WebviewWindowBuilder};

/// The API server of this window. It is never shared: an old pywebview server or a foreign process on `ui_port`
/// would lack the CORS setup or die with its own window. Dropped (and killed) on exit.
struct SpawnedServer {
    child: Child,
    #[cfg(windows)]
    _job: job::Job,
}

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

/// A free loopback port for the API server: bound and released here, taken by Python a moment later.
fn free_port() -> std::io::Result<u16> {
    Ok(TcpListener::bind(("127.0.0.1", 0))?.local_addr()?.port())
}

/// Per-launch secret (128 bits from the OS RNG) shared by the page and its server: every Tauri app on Windows has the
/// origin http://tauri.localhost, so the server accepts writes from that origin only with this token.
fn new_token() -> String {
    let mut bytes = [0u8; 16];
    getrandom::fill(&mut bytes).expect("OS random number generator unavailable");
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Starts `<root>\.venv\Scripts\pythonw.exe -m perfmon.ui.server <port>` hidden, when the venv exists.
fn spawn_server(root: &Path, port: u16, token: &str) -> Option<SpawnedServer> {
    let python = root.join(".venv").join("Scripts").join("pythonw.exe");
    if !python.is_file() {
        return None;
    }
    let mut cmd = Command::new(python);
    cmd.args(["-m", "perfmon.ui.server", &port.to_string()])
        .env("PERFMON_UI_TOKEN", token)
        .current_dir(root)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // Suspended until it is in the job: the venv's pythonw.exe is a launcher that starts the real interpreter as
        // its child, and that child must not escape the job.
        cmd.creation_flags(0x0800_0000 | 0x0000_0004); // CREATE_NO_WINDOW | CREATE_SUSPENDED
    }
    let mut child = cmd.spawn().ok()?;
    #[cfg(windows)]
    let job = match job::Job::with(&child).filter(|_| job::resume(child.id())) {
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
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Thread32First, Thread32Next, TH32CS_SNAPTHREAD, THREADENTRY32,
    };
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation, SetInformationJobObject,
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    use windows_sys::Win32::System::Threading::{OpenThread, ResumeThread, THREAD_SUSPEND_RESUME};

    /// A job object that kills every process in it when the last handle closes, also when this process is killed.
    pub struct Job(HANDLE);

    // SAFETY: a job handle is only closed in Drop; the kernel serialises everything else.
    unsafe impl Send for Job {}

    impl Job {
        pub fn with(child: &Child) -> Option<Job> {
            // SAFETY: plain Win32 calls with valid arguments; the handle is owned by `Job`, so it closes on every path.
            unsafe {
                let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
                if handle.is_null() {
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

    /// Resumes the threads of a process created suspended (std does not expose its primary thread handle).
    pub fn resume(pid: u32) -> bool {
        // SAFETY: plain Win32 calls; every handle opened here is closed here.
        unsafe {
            let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
            if snapshot == INVALID_HANDLE_VALUE {
                return false;
            }
            let mut entry: THREADENTRY32 = std::mem::zeroed();
            entry.dwSize = std::mem::size_of::<THREADENTRY32>() as u32;
            let mut resumed = false;
            let mut more = Thread32First(snapshot, &mut entry) != 0;
            while more {
                if entry.th32OwnerProcessID == pid {
                    let thread = OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID);
                    if !thread.is_null() {
                        resumed |= ResumeThread(thread) != u32::MAX;
                        CloseHandle(thread);
                    }
                }
                more = Thread32Next(snapshot, &mut entry) != 0;
            }
            CloseHandle(snapshot);
            resumed
        }
    }
}

pub fn run() {
    let root = find_root();
    let server: Arc<Mutex<Option<SpawnedServer>>> = Arc::default();
    let spawned = server.clone();

    tauri::Builder::default()
        // Must be first: a second launch focuses the running window and exits.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .setup(move |app| {
            let port = free_port()?;
            let token = new_token();

            // The window comes first: the page paints while the API server starts in the background.
            let config = app
                .config()
                .app
                .windows
                .first()
                .cloned()
                .ok_or("tauri.conf.json defines no window")?;
            WebviewWindowBuilder::from_config(app.handle(), &config)?
                .initialization_script(format!(
                    "window.__PERFMON_API__ = \"http://127.0.0.1:{port}\"; window.__PERFMON_TOKEN__ = \"{token}\";"
                ))
                .build()?;

            if let Some(root) = root {
                std::thread::spawn(move || {
                    let server = spawn_server(&root, port, &token);
                    if let Ok(mut slot) = spawned.lock() {
                        *slot = server;
                    }
                });
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building PerfMonitor")
        .run(move |_app, event| {
            if let RunEvent::Exit = event {
                // Killing the launcher is not enough (the interpreter is its child): dropping the job ends both.
                let spawned = server.lock().ok().and_then(|mut slot| slot.take());
                if let Some(mut spawned) = spawned {
                    let _ = spawned.child.kill();
                }
            }
        });
}
