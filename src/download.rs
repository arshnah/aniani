use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};

fn safe(name: &str) -> String {
    name.chars().map(|c| if "<>:\"/\\|?*".contains(c) { '_' } else { c }).collect::<String>().trim().to_string()
}

fn download_root() -> PathBuf {
    dirs::home_dir().unwrap_or_default().join("Videos/aniani")
}

pub fn dest_path(anime_title: &str, ep_no: &str) -> PathBuf {
    let show_dir = download_root().join(safe(anime_title));
    let _ = std::fs::create_dir_all(&show_dir);
    show_dir.join(format!("Episode {ep_no}.mp4"))
}

pub fn is_downloaded(anime_title: &str, ep_no: &str) -> bool {
    dest_path(anime_title, ep_no).exists()
}

#[derive(Clone)]
pub struct DownloadedShow {
    pub title: String,
    pub episodes: Vec<String>,
}

pub fn list_downloaded() -> Vec<DownloadedShow> {
    let root = download_root();
    let Ok(entries) = std::fs::read_dir(&root) else { return vec![] };
    let ep_re = regex::Regex::new(r"^Episode (.+)\.\w+$").unwrap();

    let mut library: Vec<DownloadedShow> = entries
        .flatten()
        .filter(|e| e.path().is_dir())
        .filter_map(|e| {
            let title = e.file_name().to_string_lossy().to_string();
            let mut episodes: Vec<String> = std::fs::read_dir(e.path())
                .ok()?
                .flatten()
                .filter_map(|f| ep_re.captures(&f.file_name().to_string_lossy()).map(|c| c[1].to_string()))
                .collect();
            if episodes.is_empty() {
                return None;
            }
            episodes.sort_by_key(|e| (e.len(), e.clone()));
            Some(DownloadedShow { title, episodes })
        })
        .collect();
    library.sort_by(|a, b| a.title.cmp(&b.title));
    library
}

pub fn delete_episode(anime_title: &str, ep_no: &str) {
    let show_dir = download_root().join(safe(anime_title));
    let prefix = format!("Episode {ep_no}.");
    if let Ok(entries) = std::fs::read_dir(&show_dir) {
        for entry in entries.flatten() {
            if entry.file_name().to_string_lossy().starts_with(&prefix) {
                let _ = std::fs::remove_file(entry.path());
            }
        }
    }
    if std::fs::read_dir(&show_dir).map(|mut d| d.next().is_none()).unwrap_or(false) {
        let _ = std::fs::remove_dir(&show_dir);
    }
}

#[derive(Clone, Copy, PartialEq, Debug)]
pub enum JobStatus {
    Queued,
    Downloading,
    Done,
    Failed,
    Cancelled,
}

pub struct DownloadJob {
    pub anime_title: String,
    pub ep_no: String,
    pub url: String,
    pub referer: Option<String>,
    pub dest: PathBuf,
    pub status: Mutex<JobStatus>,
    pub progress: Mutex<f64>,
    proc: Mutex<Option<Child>>,
}

impl DownloadJob {
    pub fn new(anime_title: &str, ep_no: &str, url: &str, referer: Option<String>) -> Arc<Self> {
        Arc::new(DownloadJob {
            anime_title: anime_title.to_string(),
            ep_no: ep_no.to_string(),
            url: url.to_string(),
            referer,
            dest: dest_path(anime_title, ep_no),
            status: Mutex::new(JobStatus::Queued),
            progress: Mutex::new(0.0),
            proc: Mutex::new(None),
        })
    }

    pub fn cancel(&self) {
        let mut status = self.status.lock().unwrap();
        if !matches!(*status, JobStatus::Queued | JobStatus::Downloading) {
            return;
        }
        *status = JobStatus::Cancelled;
        if let Some(proc) = self.proc.lock().unwrap().as_mut() {
            let _ = proc.kill();
        }
    }
}

pub fn run_job(job: &Arc<DownloadJob>) {
    *job.status.lock().unwrap() = JobStatus::Downloading;

    let ffmpeg = which::which("ffmpeg").map(|p| p.to_string_lossy().to_string()).unwrap_or_else(|_| "ffmpeg".into());
    let mut cmd = Command::new(ffmpeg);
    cmd.args(["-y", "-loglevel", "info", "-stats"]);
    if let Some(referer) = &job.referer {
        cmd.args(["-headers", &format!("Referer: {referer}\r\n")]);
    }
    cmd.args(["-extension_picky", "0", "-allowed_segment_extensions", "ALL"]);
    cmd.args(["-i", &job.url, "-c", "copy"]).arg(&job.dest);
    cmd.stdout(Stdio::piped()).stderr(Stdio::piped());

    let mut proc = match cmd.spawn() {
        Ok(p) => p,
        Err(_) => {
            *job.status.lock().unwrap() = JobStatus::Failed;
            return;
        }
    };
    let stdout = proc.stdout.take();
    *job.proc.lock().unwrap() = Some(proc);

    if let Some(stdout) = stdout {
        let mut duration: Option<i64> = None;
        let dur_re = regex::Regex::new(r"Duration: (\d+):(\d+):(\d+)").unwrap();
        let time_re = regex::Regex::new(r"time=(\d+):(\d+):(\d+)").unwrap();
        for line in BufReader::new(stdout).lines().flatten() {
            if duration.is_none() {
                if let Some(c) = dur_re.captures(&line) {
                    duration = Some(hms(&c));
                }
            }
            if let (Some(d), Some(c)) = (duration, time_re.captures(&line)) {
                if d > 0 {
                    *job.progress.lock().unwrap() = (hms(&c) as f64 / d as f64).min(1.0);
                }
            }
        }
    }

    let mut guard = job.proc.lock().unwrap();
    let status = guard.as_mut().map(|p| p.wait()).transpose().ok().flatten();
    drop(guard);

    let mut job_status = job.status.lock().unwrap();
    if *job_status == JobStatus::Cancelled {
        let _ = std::fs::remove_file(&job.dest);
    } else if status.map(|s| s.success()).unwrap_or(false) && job.dest.exists() {
        *job_status = JobStatus::Done;
        *job.progress.lock().unwrap() = 1.0;
    } else {
        *job_status = JobStatus::Failed;
        let _ = std::fs::remove_file(&job.dest);
    }
}

fn hms(c: &regex::Captures) -> i64 {
    let h: i64 = c[1].parse().unwrap_or(0);
    let m: i64 = c[2].parse().unwrap_or(0);
    let s: i64 = c[3].parse().unwrap_or(0);
    h * 3600 + m * 60 + s
}
