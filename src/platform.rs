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
