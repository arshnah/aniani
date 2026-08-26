use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::Instant;

const VIDEO_EXTS: [&str; 7] = ["mp4", "mkv", "avi", "webm", "mov", "m4v", "ts"];

fn is_video_file(name: &str) -> bool {
    Path::new(name)
        .extension()
        .and_then(|e| e.to_str())
        .map(|e| VIDEO_EXTS.iter().any(|ext| ext.eq_ignore_ascii_case(e)))
        .unwrap_or(false)
}

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

pub fn import_file(anime_title: &str, ep_label: &str, src: &Path) -> std::io::Result<()> {
    let ext = src.extension().and_then(|e| e.to_str()).unwrap_or("mp4");
    let show_dir = download_root().join(safe(anime_title));
    std::fs::create_dir_all(&show_dir)?;
    let dest = show_dir.join(format!("{}.{ext}", safe(ep_label)));
    std::fs::copy(src, dest)?;
    Ok(())
}

fn locate_episode_file(show_dir: &Path, ep_no: &str) -> Option<PathBuf> {
    let prefix = format!("Episode {ep_no}.");
    let mut fallback = None;
    for entry in std::fs::read_dir(show_dir).ok()?.flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        if name.starts_with(&prefix) {
            return Some(entry.path());
        }
        if is_video_file(&name) && Path::new(&name).file_stem().map(|s| s.to_string_lossy()) == Some(ep_no.into()) {
            fallback = Some(entry.path());
        }
    }
    fallback
}

pub fn find_episode_file(anime_title: &str, ep_no: &str) -> Option<PathBuf> {
    locate_episode_file(&download_root().join(safe(anime_title)), ep_no)
}

pub fn is_downloaded(anime_title: &str, ep_no: &str) -> bool {
    find_episode_file(anime_title, ep_no).is_some()
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
                .filter_map(|f| {
                    let name = f.file_name().to_string_lossy().to_string();
                    if let Some(c) = ep_re.captures(&name) {
                        return Some(c[1].to_string());
                    }
                    if is_video_file(&name) {
                        return Some(Path::new(&name).file_stem()?.to_string_lossy().to_string());
                    }
                    None
                })
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
    if let Some(path) = locate_episode_file(&show_dir, ep_no) {
        let _ = std::fs::remove_file(path);
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
    pub ep_ref: String,
    pub dest: PathBuf,
    pub status: Mutex<JobStatus>,
    pub progress: Mutex<f64>,
    pub indeterminate: Mutex<bool>,
    pub started: Mutex<Option<Instant>>,
    proc: Mutex<Option<Child>>,
}

impl DownloadJob {
    pub fn new(anime_title: &str, ep_no: &str, ep_ref: &str) -> Arc<Self> {
        Arc::new(DownloadJob {
            anime_title: anime_title.to_string(),
            ep_no: ep_no.to_string(),
            ep_ref: ep_ref.to_string(),
            dest: dest_path(anime_title, ep_no),
            status: Mutex::new(JobStatus::Queued),
            progress: Mutex::new(0.0),
            indeterminate: Mutex::new(true),
            started: Mutex::new(None),
            proc: Mutex::new(None),
        })
    }

    pub fn eta(&self) -> Option<std::time::Duration> {
        let started = (*self.started.lock().unwrap())?;
        let progress = *self.progress.lock().unwrap();
        if progress <= 0.01 {
            return None;
        }
        let elapsed = started.elapsed().as_secs_f64();
        let total = elapsed / progress;
        Some(std::time::Duration::from_secs_f64((total - elapsed).max(0.0)))
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

fn tool_path(name: &str) -> String {
    which::which(name).map(|p| p.to_string_lossy().to_string()).unwrap_or_else(|_| name.into())
}

fn probe_duration(url: &str, referer: &Option<String>) -> Option<f64> {
    let mut cmd = Command::new(tool_path("ffprobe"));
    cmd.args(["-v", "error"]);
    if let Some(referer) = referer {
        cmd.args(["-headers", &format!("Referer: {referer}\r\n")]);
    }
    cmd.args(["-extension_picky", "0", "-allowed_segment_extensions", "ALL"]);
    cmd.args(["-show_entries", "format=duration", "-of", "csv=p=0", url]);
    let out = cmd.output().ok()?;
    String::from_utf8_lossy(&out.stdout).trim().parse::<f64>().ok().filter(|d| *d > 0.0)
}

pub fn run_job(job: &Arc<DownloadJob>) {
    *job.status.lock().unwrap() = JobStatus::Downloading;

    let Ok(Some(link)) = crate::sources::anidb_watch(&job.ep_ref, false) else {
        *job.status.lock().unwrap() = JobStatus::Failed;
        return;
    };

    let duration = probe_duration(&link.url, &link.referer);
    *job.indeterminate.lock().unwrap() = duration.is_none();

    let mut cmd = Command::new(tool_path("ffmpeg"));
    cmd.args(["-y", "-loglevel", "error"]);
    if let Some(referer) = &link.referer {
        cmd.args(["-headers", &format!("Referer: {referer}\r\n")]);
    }
    cmd.args(["-extension_picky", "0", "-allowed_segment_extensions", "ALL"]);
    cmd.args(["-i", &link.url, "-c", "copy", "-progress", "pipe:1", "-nostats"]).arg(&job.dest);
    cmd.stdout(Stdio::piped()).stderr(Stdio::piped());

    let mut proc = match cmd.spawn() {
        Ok(p) => p,
        Err(_) => {
            *job.status.lock().unwrap() = JobStatus::Failed;
            return;
        }
    };
    let stdout = proc.stdout.take();
    let stderr = proc.stderr.take();
    *job.proc.lock().unwrap() = Some(proc);
    *job.started.lock().unwrap() = Some(Instant::now());

    if let Some(stderr) = stderr {
        std::thread::spawn(move || {
            let mut discard = String::new();
            let _ = BufReader::new(stderr).read_to_string(&mut discard);
        });
    }

    if let Some(stdout) = stdout {
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            if let Some(us) = line.strip_prefix("out_time_us=").and_then(|v| v.trim().parse::<i64>().ok()) {
                if let Some(d) = duration {
                    *job.progress.lock().unwrap() = ((us as f64 / 1_000_000.0) / d).clamp(0.0, 1.0);
                }
            } else if line.trim() == "progress=end" {
                *job.indeterminate.lock().unwrap() = false;
                if duration.is_some() {
                    *job.progress.lock().unwrap() = 1.0;
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
        *job.indeterminate.lock().unwrap() = false;
        *job.progress.lock().unwrap() = 1.0;
    } else {
        *job_status = JobStatus::Failed;
        let _ = std::fs::remove_file(&job.dest);
    }
}
