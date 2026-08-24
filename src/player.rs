use serde_json::{json, Value};
use std::process::{Child, Command, Stdio};
use std::time::Duration;

use crate::ipc::Conn;
use crate::platform;

pub struct MpvPlayer {
    proc: Option<Child>,
    conn: Option<Conn>,
    pipe_path: String,
    request_id: i64,
}

#[derive(Clone)]
pub struct Status {
    pub time: f64,
    pub duration: f64,
    pub paused: bool,
}

impl MpvPlayer {
    pub fn new() -> Self {
        MpvPlayer {
            proc: None,
            conn: None,
            pipe_path: platform::mpv_ipc_path("aniani-mpv"),
            request_id: 1,
        }
    }

    pub fn is_running(&mut self) -> bool {
        match &mut self.proc {
            Some(p) => matches!(p.try_wait(), Ok(None)),
            None => false,
        }
    }

    pub fn play(
        &mut self,
        url: &str,
        title: Option<&str>,
        referer: Option<&str>,
        start_seconds: Option<f64>,
    ) {
        self.stop();
        let _ = std::fs::remove_file(&self.pipe_path);

        let mpv_bin = platform::find_mpv().unwrap_or_else(|| "mpv".to_string());
        let mut cmd = Command::new(mpv_bin);
        cmd.arg(format!("--input-ipc-server={}", self.pipe_path));
        if let Some(t) = title {
            cmd.arg(format!("--force-media-title={t}"));
        }
        if let Some(r) = referer {
            cmd.arg(format!("--http-header-fields=Referer: {r}"));
        }
        if let Some(s) = start_seconds {
            if s > 0.0 {
                cmd.arg(format!("--start={s}"));
            }
        }
        cmd.arg(url);
        cmd.stdout(Stdio::null()).stderr(Stdio::null());

        self.proc = cmd.spawn().ok();
        std::thread::sleep(Duration::from_secs(1));
        self.connect(6);
    }

    fn connect(&mut self, attempts: u32) {
        for _ in 0..attempts {
            if let Some(conn) = Conn::connect(&self.pipe_path) {
                self.conn = Some(conn);
                return;
            }
            std::thread::sleep(Duration::from_millis(500));
        }
    }

    fn get(&mut self, name: &str) -> Option<Value> {
        let conn = self.conn.as_mut()?;
        self.request_id += 1;
        let id = self.request_id;
        let req = json!({"command": ["get_property", name], "request_id": id});
        conn.send_line(&req.to_string());
        conn.read_response(id, Duration::from_secs(1))
    }

    fn set(&mut self, name: &str, value: Value) {
        if let Some(conn) = self.conn.as_mut() {
            let req = json!({"command": ["set_property", name, value]});
            conn.send_line(&req.to_string());
        }
    }

    fn command(&mut self, args: &[&str]) {
        if let Some(conn) = self.conn.as_mut() {
            let req = json!({"command": args});
            conn.send_line(&req.to_string());
        }
    }

    pub fn get_status(&mut self) -> Option<Status> {
        if self.conn.is_none() {
            return None;
        }
        let time = self.get("time-pos")?.as_f64()?;
        let duration = self.get("duration").and_then(|v| v.as_f64()).unwrap_or(0.0);
        let paused = self.get("pause").and_then(|v| v.as_bool()).unwrap_or(false);
        Some(Status { time, duration, paused })
    }

    pub fn set_pause(&mut self, paused: bool) {
        self.set("pause", json!(paused));
    }

    pub fn toggle_pause(&mut self) {
        let cur = self.get("pause").and_then(|v| v.as_bool()).unwrap_or(false);
        self.set("pause", json!(!cur));
    }

    pub fn seek(&mut self, seconds: f64) {
        self.set("time-pos", json!(seconds));
    }

    pub fn set_volume(&mut self, percent: i32) {
        self.set("volume", json!(percent));
    }

    pub fn set_speed(&mut self, rate: f64) {
        self.set("speed", json!(rate));
    }

    pub fn cycle_subtitle(&mut self) {
        self.command(&["cycle", "sub"]);
    }

    pub fn cycle_audio(&mut self) {
        self.command(&["cycle", "audio"]);
    }

    pub fn stop(&mut self) {
        self.conn = None;
        if let Some(mut p) = self.proc.take() {
            let _ = p.kill();
            let _ = p.wait();
        }
    }
}

impl Drop for MpvPlayer {
    fn drop(&mut self) {
        self.stop();
    }
}

pub struct VlcPlayer {
    proc: Option<Child>,
    port: u16,
    password: String,
    client: reqwest::blocking::Client,
}

fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap_or(9091)
}

impl VlcPlayer {
    pub fn new() -> Self {
        VlcPlayer {
            proc: None,
            port: 9091,
            password: String::new(),
            client: reqwest::blocking::Client::builder()
                .timeout(Duration::from_millis(1500))
                .build()
                .unwrap(),
        }
    }

    fn base_url(&self) -> String {
        format!("http://127.0.0.1:{}/requests/status.json", self.port)
    }

    pub fn is_running(&mut self) -> bool {
        match &mut self.proc {
            Some(p) => matches!(p.try_wait(), Ok(None)),
            None => false,
        }
    }

    pub fn play(&mut self, url: &str, title: Option<&str>, referer: Option<&str>, start_seconds: Option<f64>) {
        self.stop();
        self.port = free_port();
        self.password = format!("{:032x}", rand::random::<u128>());

        let vlc_bin = platform::find_vlc().unwrap_or_else(|| "vlc".to_string());
        let mut cmd = Command::new(vlc_bin);
        cmd.args([
            "-I", "dummy", "--no-video-title-show",
            "--extraintf", "http", "--http-password", &self.password, "--http-port",
        ]);
        cmd.arg(self.port.to_string());
        if let Some(r) = referer {
            cmd.arg(format!("--http-referrer={r}"));
        }
        if let Some(t) = title {
            cmd.arg(format!("--meta-title={t}"));
        }
        if let Some(s) = start_seconds {
            if s > 0.0 {
                cmd.arg(format!("--start-time={}", s as i64));
            }
        }
        cmd.arg(url);
        cmd.stdout(Stdio::null()).stderr(Stdio::null());
        self.proc = cmd.spawn().ok();
    }

    fn status(&self, command: Option<&str>, extra: &[(&str, &str)]) -> Option<Value> {
        let mut params: Vec<(&str, &str)> = extra.to_vec();
        if let Some(c) = command {
            params.push(("command", c));
        }
        let resp = self
            .client
            .get(self.base_url())
            .basic_auth("", Some(&self.password))
            .query(&params)
            .send()
            .ok()?;
        resp.json().ok()
    }

    pub fn get_status(&self) -> Option<Status> {
        let data = self.status(None, &[])?;
        Some(Status {
            time: data.get("time").and_then(|v| v.as_f64()).unwrap_or(0.0),
            duration: data.get("length").and_then(|v| v.as_f64()).unwrap_or(0.0),
            paused: data.get("state").and_then(|v| v.as_str()) != Some("playing"),
        })
    }

    pub fn toggle_pause(&self) {
        self.status(Some("pl_pause"), &[]);
    }

    pub fn set_pause(&self, paused: bool) {
        let currently_paused = self.get_status().map(|s| s.paused).unwrap_or(true);
        if paused != currently_paused {
            self.toggle_pause();
        }
    }

    pub fn seek(&self, seconds: f64) {
        self.status(Some("seek"), &[("val", &(seconds as i64).to_string())]);
    }

    pub fn set_volume(&self, percent: i32) {
        let raw = ((percent as f64) * 2.56) as i64;
        self.status(Some("volume"), &[("val", &raw.to_string())]);
    }

    pub fn set_speed(&self, rate: f64) {
        self.status(Some("rate"), &[("val", &rate.to_string())]);
    }

    pub fn stop(&mut self) {
        if let Some(mut p) = self.proc.take() {
            let _ = p.kill();
            let _ = p.wait();
        }
    }
}

impl Drop for VlcPlayer {
    fn drop(&mut self) {
        self.stop();
    }
}

pub enum Backend {
    Mpv(MpvPlayer),
    Vlc(VlcPlayer),
}

impl Backend {
    pub fn play(&mut self, url: &str, title: Option<&str>, referer: Option<&str>, start_seconds: Option<f64>) {
        match self {
            Backend::Mpv(p) => p.play(url, title, referer, start_seconds),
            Backend::Vlc(p) => p.play(url, title, referer, start_seconds),
        }
    }
    pub fn get_status(&mut self) -> Option<Status> {
        match self {
            Backend::Mpv(p) => p.get_status(),
            Backend::Vlc(p) => p.get_status(),
        }
    }
    pub fn toggle_pause(&mut self) {
        match self {
            Backend::Mpv(p) => p.toggle_pause(),
            Backend::Vlc(p) => p.toggle_pause(),
        }
    }
    pub fn seek(&mut self, seconds: f64) {
        match self {
            Backend::Mpv(p) => p.seek(seconds),
            Backend::Vlc(p) => p.seek(seconds),
        }
    }
    pub fn set_volume(&mut self, percent: i32) {
        match self {
            Backend::Mpv(p) => p.set_volume(percent),
            Backend::Vlc(p) => p.set_volume(percent),
        }
    }
    pub fn set_speed(&mut self, rate: f64) {
        match self {
            Backend::Mpv(p) => p.set_speed(rate),
            Backend::Vlc(p) => p.set_speed(rate),
        }
    }
    pub fn cycle_subtitle(&mut self) {
        if let Backend::Mpv(p) = self {
            p.cycle_subtitle();
        }
    }
    pub fn cycle_audio(&mut self) {
        if let Backend::Mpv(p) = self {
            p.cycle_audio();
        }
    }
    pub fn stop(&mut self) {
        match self {
            Backend::Mpv(p) => p.stop(),
            Backend::Vlc(p) => p.stop(),
        }
    }
}
