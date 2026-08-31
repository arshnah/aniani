use std::env;
use std::path::PathBuf;

pub fn state_dir(app_name: &str) -> PathBuf {
    if cfg!(target_os = "windows") {
        let base = env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|| dirs::home_dir().unwrap_or_default().join("AppData/Local"));
        return base.join(app_name);
    }
    if cfg!(target_os = "macos") {
        return dirs::home_dir()
            .unwrap_or_default()
            .join("Library/Application Support")
            .join(app_name);
    }
    let base = env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| dirs::home_dir().unwrap_or_default().join(".local/state"));
    base.join(app_name)
}

pub fn temp_path(filename: &str) -> PathBuf {
    env::temp_dir().join(filename)
}

pub fn mpv_ipc_path(name: &str) -> String {
    if cfg!(target_os = "windows") {
        format!("\\\\.\\pipe\\{name}")
    } else {
        temp_path(&format!("{name}.sock")).to_string_lossy().to_string()
    }
}

fn find_binary(names: &[&str]) -> Option<String> {
    for name in names {
        if let Ok(path) = which::which(name) {
            return Some(path.to_string_lossy().to_string());
        }
    }
    None
}

pub fn find_mpv() -> Option<String> {
    if let Some(found) = find_binary(&["mpv", "mpv.exe"]) {
        return Some(found);
    }
    if cfg!(target_os = "windows") {
        for candidate in [
            dirs::home_dir().unwrap_or_default().join(r"scoop\apps\mpv\current\mpv.exe"),
            PathBuf::from(r"C:\Program Files\mpv\mpv.exe"),
        ] {
            if candidate.exists() {
                return Some(candidate.to_string_lossy().to_string());
            }
        }
    }
    None
}

pub fn find_vlc() -> Option<String> {
    if let Some(found) = find_binary(&["vlc", "vlc.exe"]) {
        return Some(found);
    }
    if cfg!(target_os = "windows") {
        for candidate in [
            r"C:\Program Files\VideoLAN\VLC\vlc.exe",
            r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
        ] {
            if PathBuf::from(candidate).exists() {
                return Some(candidate.to_string());
            }
        }
    }
    None
}

pub fn find_qbittorrent() -> Option<String> {
    find_binary(&["qbittorrent-nox", "qbittorrent-nox.exe", "qbittorrent.exe"])
}

pub fn find_ani_skip() -> Option<String> {
    find_binary(&["ani-skip", "ani-skip.exe"])
}

pub fn debug_log(msg: &str) {
    use std::io::Write;
    let path = state_dir("aniani").join("debug.log");
    let _ = std::fs::create_dir_all(state_dir("aniani"));
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(f, "{msg}");
    }
}

pub fn acquire_single_instance_lock() -> bool {
    let _ = std::fs::create_dir_all(state_dir("aniani"));
    let path = state_dir("aniani").join("aniani.lock");
    let Ok(file) = std::fs::OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(false) // lock file only needs to exist; contents are irrelevant
        .open(path)
    else {
        return false;
    };
    let lock: &'static mut fd_lock::RwLock<std::fs::File> = Box::leak(Box::new(fd_lock::RwLock::new(file)));
    match lock.try_write() {
        Ok(guard) => {
            std::mem::forget(guard);
            true
        }
        Err(_) => false,
    }
}

fn show_signal_path() -> PathBuf {
    state_dir("aniani").join("aniani.show")
}

pub fn request_show_running_instance() {
    let _ = std::fs::create_dir_all(state_dir("aniani"));
    let _ = std::fs::write(show_signal_path(), b"");
}

pub fn consume_show_request() -> bool {
    let path = show_signal_path();
    if path.exists() {
        let _ = std::fs::remove_file(&path);
        true
    } else {
        false
    }
}

/// True when a client whose pid matches `pid` (or, when no pid is known, whose class
/// matches) exists. Matching by class alone is a fallback: any unrelated window of the
/// same app would otherwise mask or fake the answer.
pub fn hyprland_window_exists(window_class: &str, pid: Option<u32>) -> Option<bool> {
    let hyprctl = which::which("hyprctl").ok()?;
    let out = std::process::Command::new(hyprctl).args(["clients", "-j"]).output().ok()?;
    let clients: serde_json::Value = serde_json::from_slice(&out.stdout).ok()?;
    let arr = clients.as_array()?;
    Some(arr.iter().any(|c| match pid {
        Some(p) => c.get("pid").and_then(|v| v.as_u64()) == Some(p as u64),
        None => c
            .get("class")
            .and_then(|v| v.as_str())
            .map(|s| s.eq_ignore_ascii_case(window_class))
            .unwrap_or(false),
    }))
}

/// Writes a file readable only by the current user on Unix; plain write elsewhere.
pub fn write_private(path: &std::path::Path, contents: &str) {
    #[cfg(unix)]
    {
        use std::io::Write;
        use std::os::unix::fs::OpenOptionsExt;
        let _ = std::fs::OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .mode(0o600)
            .open(path)
            .and_then(|mut f| f.write_all(contents.as_bytes()));
    }
    #[cfg(not(unix))]
    {
        let _ = std::fs::write(path, contents);
    }
}

/// Linux only: true if /proc/{pid} exists and its cmdline actually mentions `needle`.
/// Guards against PID reuse making a dead player look alive.
#[cfg(target_os = "linux")]
pub fn pid_cmdline_contains(pid: u32, needle: &str) -> bool {
    std::fs::read_to_string(format!("/proc/{pid}/cmdline"))
        .map(|cmdline| cmdline.replace('\0', " ").contains(needle))
        .unwrap_or(false)
}

/// Discord only shows one local Rich Presence activity at a time. mpd-discord-rpc
/// (a systemd user service some setups run) holds a competing connection that would
/// otherwise silently stomp aniani's presence. Best-effort, silent no-op if the
/// service isn't installed/running -- this must never block startup or shutdown.
#[cfg(target_os = "linux")]
pub fn pause_mpd_discord_rpc() {
    let _ = std::process::Command::new("systemctl").args(["--user", "stop", "mpd-discord-rpc"]).output();
}

#[cfg(not(target_os = "linux"))]
pub fn pause_mpd_discord_rpc() {}

#[cfg(target_os = "linux")]
pub fn resume_mpd_discord_rpc() {
    let _ = std::process::Command::new("systemctl").args(["--user", "start", "mpd-discord-rpc"]).output();
}

#[cfg(not(target_os = "linux"))]
pub fn resume_mpd_discord_rpc() {}

pub fn find_curl_impersonate() -> Option<String> {
    let names = ["curl_chrome136", "curl_chrome136.exe", "curl-impersonate-chrome.exe"];
    if let Some(found) = find_binary(&names) {
        return Some(found);
    }
    let exe_dir = std::env::current_exe().ok().and_then(|p| p.parent().map(|p| p.to_path_buf()));
    if let Some(dir) = exe_dir {
        for name in names {
            let candidate = dir.join(name);
            if candidate.exists() {
                return Some(candidate.to_string_lossy().to_string());
            }
        }
    }
    None
}
