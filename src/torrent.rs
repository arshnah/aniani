use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use crate::platform;

const NYAA_BASE: &str = "https://nyaa.si";
const TRACKERS: &[&str] = &[
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
];

#[derive(Clone)]
pub struct TorrentResult {
    pub title: String,
    pub size: String,
    pub seeders: i64,
    pub leechers: i64,
    pub magnet: String,
}

pub async fn nyaa_search(client: &reqwest::Client, query: &str) -> anyhow::Result<Vec<TorrentResult>> {
    let q = urlencoding::encode(query.trim());
    if q.is_empty() {
        return Ok(vec![]);
    }
    let url = format!("{NYAA_BASE}/?page=rss&q={q}&c=1_2&f=0");
    let body = client.get(&url).send().await?.text().await?;

    let title_re = regex::Regex::new(r"<title>(.*?)</title>")?;
    let hash_re = regex::Regex::new(r"nyaa:infoHash>([a-fA-F0-9]+)<")?;
    let seeders_re = regex::Regex::new(r"nyaa:seeders>(\d+)<")?;
    let leechers_re = regex::Regex::new(r"nyaa:leechers>(\d+)<")?;
    let size_re = regex::Regex::new(r"nyaa:size>([^<]+)<")?;

    let mut results = vec![];
    for item in body.split("<item>").skip(1) {
        let Some(title) = title_re.captures(item).map(|c| c[1].to_string()) else { continue };
        let Some(hash) = hash_re.captures(item).map(|c| c[1].to_string()) else { continue };
        let seeders = seeders_re.captures(item).and_then(|c| c[1].parse().ok()).unwrap_or(0);
        let leechers = leechers_re.captures(item).and_then(|c| c[1].parse().ok()).unwrap_or(0);
        let size = size_re.captures(item).map(|c| c[1].to_string()).unwrap_or_default();
        let trackers: String = TRACKERS.iter().map(|t| format!("&tr={}", urlencoding::encode(t))).collect();
        let magnet = format!("magnet:?xt=urn:btih:{hash}&dn={}{trackers}", urlencoding::encode(&title));
        results.push(TorrentResult { title, size, seeders, leechers, magnet });
    }
    results.sort_by(|a, b| b.seeders.cmp(&a.seeders));
    Ok(results)
}

const WEBUI_PORT: u16 = 8095;

pub struct TorrentEngine {
    proc: Option<Child>,
    client: reqwest::blocking::Client,
    base_url: String,
    download_dir: std::path::PathBuf,
}

impl TorrentEngine {
    pub fn new() -> Self {
        TorrentEngine {
            proc: None,
            client: reqwest::blocking::Client::builder().cookie_store(true).build().unwrap(),
            base_url: format!("http://127.0.0.1:{WEBUI_PORT}"),
            download_dir: dirs::home_dir().unwrap_or_default().join("Videos/aniani-torrents"),
        }
    }

    fn ping(&self) -> bool {
        self.client
            .get(format!("{}/api/v2/app/version", self.base_url))
            .timeout(Duration::from_secs(1))
            .send()
            .map(|r| r.status().is_success())
            .unwrap_or(false)
    }

    pub fn ensure_running(&mut self) -> bool {
        if self.ping() {
            return true;
        }
        let bin = platform::find_qbittorrent().unwrap_or_else(|| "qbittorrent-nox".into());
        let profile_dir = platform::state_dir("aniani").join("qbt-profile");
        let _ = std::fs::create_dir_all(&self.download_dir);

        let mut cmd = Command::new(bin);
        cmd.arg(format!("--webui-port={WEBUI_PORT}"))
            .arg(format!("--profile={}", profile_dir.display()))
            .arg("--confirm-legal-notice")
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        let mut child = match cmd.spawn() {
            Ok(c) => c,
            Err(_) => return false,
        };

        let stdout = child.stdout.take();
        self.proc = Some(child);

        let password = stdout.and_then(|out| {
            use std::io::{BufRead, BufReader};
            let re = regex::Regex::new(r"temporary password is provided for this session:\s*(\S+)").ok()?;
            for line in BufReader::new(out).lines().flatten() {
                if let Some(c) = re.captures(&line) {
                    return Some(c[1].to_string());
                }
            }
            None
        });
        let Some(password) = password else { return false };

        let deadline = Instant::now() + Duration::from_secs(15);
        while Instant::now() < deadline {
            let resp = self
                .client
                .post(format!("{}/api/v2/auth/login", self.base_url))
                .form(&[("username", "admin"), ("password", password.as_str())])
                .header("Referer", &self.base_url)
                .send();
            if resp.map(|r| r.status().is_success()).unwrap_or(false) {
                let _ = self.client.post(format!("{}/api/v2/app/setPreferences", self.base_url))
                    .header("Referer", &self.base_url)
                    .form(&[("json", format!(r#"{{"save_path": "{}"}}"#, self.download_dir.display()))])
                    .send();
                return true;
            }
            std::thread::sleep(Duration::from_millis(300));
        }
        false
    }

    pub fn add_magnet(&self, magnet: &str) -> Option<String> {
        self.client
            .post(format!("{}/api/v2/torrents/add", self.base_url))
            .header("Referer", &self.base_url)
            .form(&[("urls", magnet), ("sequentialDownload", "true"), ("firstLastPiecePrio", "true")])
            .send()
            .ok()?;
        regex::Regex::new(r"btih:([a-fA-F0-9]+)").ok()?.captures(magnet).map(|c| c[1].to_lowercase())
    }

    fn info(&self, info_hash: &str) -> Option<serde_json::Value> {
        let resp: Vec<serde_json::Value> = self
            .client
            .get(format!("{}/api/v2/torrents/info", self.base_url))
            .query(&[("hashes", info_hash)])
            .send()
            .ok()?
            .json()
            .ok()?;
        resp.into_iter().next()
    }

    fn files(&self, info_hash: &str) -> Vec<serde_json::Value> {
        self.client
            .get(format!("{}/api/v2/torrents/files", self.base_url))
            .query(&[("hash", info_hash)])
            .send()
            .ok()
            .and_then(|r| r.json().ok())
            .unwrap_or_default()
    }

    fn largest_video_path(&self, info_hash: &str, save_path: &str) -> Option<std::path::PathBuf> {
        const VIDEO_EXTS: &[&str] = &[".mkv", ".mp4", ".avi", ".webm"];
        self.files(info_hash)
            .into_iter()
            .filter(|f| VIDEO_EXTS.iter().any(|e| f["name"].as_str().unwrap_or("").to_lowercase().ends_with(e)))
            .max_by_key(|f| f["size"].as_i64().unwrap_or(0))
            .map(|f| std::path::Path::new(save_path).join(f["name"].as_str().unwrap_or("")))
    }

    pub fn wait_for_buffer(&self, info_hash: &str, percent: f64, timeout: Duration) -> Option<std::path::PathBuf> {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            if let Some(info) = self.info(info_hash) {
                let progress = info["progress"].as_f64().unwrap_or(0.0) * 100.0;
                if progress >= percent {
                    return self.largest_video_path(info_hash, info["save_path"].as_str().unwrap_or(""));
                }
            }
            std::thread::sleep(Duration::from_secs(1));
        }
        None
    }
}

impl Drop for TorrentEngine {
    fn drop(&mut self) {
        if let Some(mut p) = self.proc.take() {
            let _ = p.kill();
        }
    }
}
