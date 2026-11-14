//! PerfMonitor window: serves the static UI (from the checkout on disk) and talks to the Python API over HTTP.
//! The collector, analytics and CLI stay in Python; this crate only owns the window and its API server process.

use std::borrow::Cow;
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};

use tauri::utils::assets::{AssetKey, AssetsIter, CspHash};
use tauri::{Assets, Manager, RunEvent, Runtime, WebviewWindowBuilder};

/// Serves the UI from `<root>\perfmon\ui\static` at runtime, so after a `git pull` the window shows the same JS/CSS as
/// the Python on disk without a rebuild. Tauri applies the CSP from tauri.conf.json to these files like to embedded ones.
struct DiskAssets {
    dir: PathBuf, // canonical
}

impl DiskAssets {
    fn new(root: &Path) -> Option<Self> {
        let dir = root
            .join("perfmon")
            .join("ui")
            .join("static")
            .canonicalize()
            .ok()?;
        dir.join("index.html").is_file().then_some(Self { dir })
    }

    /// The file behind an asset key, or None when the key names no file inside the directory.
    fn file(&self, key: &AssetKey) -> Option<PathBuf> {
        let mut path = self.dir.clone();
        for part in key.as_ref().split('/').filter(|part| !part.is_empty()) {
            // AssetKey keeps "..", drive prefixes and backslashes: allow plain names only.
            if part == "." || part == ".." || part.contains([':', '\\']) {
                return None;
            }
            path.push(part);
        }
        // Canonical again, so a link inside the directory cannot lead out of it either.
        let path = path.canonicalize().ok()?;
        (path.starts_with(&self.dir) && path.is_file()).then_some(path)
    }
}

impl<R: Runtime> Assets<R> for DiskAssets {
    fn get(&self, key: &AssetKey) -> Option<Cow<'_, [u8]>> {
        std::fs::read(self.file(key)?).ok().map(Cow::Owned)
    }

    fn iter(&self) -> Box<AssetsIter<'_>> {
        let mut keys = Vec::new();
        let mut dirs = vec![self.dir.clone()];
        while let Some(dir) = dirs.pop() {
            for entry in std::fs::read_dir(dir).into_iter().flatten().flatten() {
                match entry.file_type() {
                    Ok(kind) if kind.is_dir() => dirs.push(entry.path()),
                    Ok(kind) if kind.is_file() => {
                        if let Ok(rel) = entry.path().strip_prefix(&self.dir) {
                            keys.push(format!("/{}", rel.to_string_lossy().replace('\\', "/")));
                        }
                    }
                    _ => {}
                }
            }
        }
        Box::new(keys.into_iter().filter_map(move |key| {
            let bytes = std::fs::read(self.file(&AssetKey::from(key.as_str()))?).ok()?;
            Some((Cow::Owned(key), Cow::Owned(bytes)))
        }))
    }

    fn csp_hashes(&self, _html_path: &AssetKey) -> Box<dyn Iterator<Item = CspHash<'_>> + '_> {
        // The embedded build adds hashes of inline <script>s and of every .js file. index.html has no inline script
        // and script-src already allows 'self', so the policy is the same without them. An inline <script> added to
        // index.html would be blocked here: keep scripts in files.
        Box::new(std::iter::empty())
    }
}

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
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
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
                if set == 0
                    || AssignProcessToJobObject(handle, child.as_raw_handle() as HANDLE) == 0
                {
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

    // The copy embedded at build time is only the fallback for an exe without a checkout around it.
    let mut context = tauri::generate_context!();
    if let Some(assets) = root.as_deref().and_then(DiskAssets::new) {
        context.set_assets(Box::new(assets));
    }

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
        .build(context)
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn disk_assets_stay_inside_the_static_directory() {
        let root = std::env::temp_dir().join(format!("perfmon-assets-{}", std::process::id()));
        let static_dir = root.join("perfmon").join("ui").join("static");
        std::fs::create_dir_all(static_dir.join("pages")).unwrap();
        std::fs::write(static_dir.join("index.html"), "<!doctype html>").unwrap();
        std::fs::write(static_dir.join("pages").join("games.js"), "1").unwrap();
        std::fs::write(root.join("secret.txt"), "no").unwrap();

        let assets = DiskAssets::new(&root).unwrap();
        let get = |key: &str| {
            <DiskAssets as Assets<tauri::Wry>>::get(&assets, &AssetKey::from(key))
                .map(|b| b.into_owned())
        };
        assert_eq!(get("/index.html").as_deref(), Some(&b"<!doctype html>"[..]));
        assert_eq!(get("/pages/games.js").as_deref(), Some(&b"1"[..]));
        for key in [
            "/../../../secret.txt",
            "../../../secret.txt",
            "/pages/../../../../secret.txt",
            r"/pages\..\index.html",
            "C:/Windows/win.ini",
            "/index.html:stream",
            "/pages",
            "/missing.js",
        ] {
            assert_eq!(get(key), None, "{key}");
        }
        assert!(DiskAssets::new(&root.join("perfmon")).is_none()); // no index.html there: embedded fallback

        std::fs::remove_dir_all(&root).unwrap();
    }
}
