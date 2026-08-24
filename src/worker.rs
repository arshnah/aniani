use std::collections::{HashMap, HashSet};
use std::sync::mpsc::{channel, Sender};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use crate::{discord, platform, player, state};

pub enum Cmd {
    Play { url: String, title: String, ep_no: String, referer: Option<String>, start: Option<f64>, source: String, anime_id: String },
    TogglePause,
    Stop,
    SetBackend(String),
    Browsing(String),
    Seek(f64),
    SetVolume(i32),
    SetSpeed(f64),
    CycleSubtitle,
    CycleAudio,
    SetSkipTimes(Option<(f64, f64)>, Option<(f64, f64)>),
}

#[derive(Clone)]
pub struct PlayerHandle {
    tx: Sender<Cmd>,
    pub status: Arc<Mutex<Option<player::Status>>>,
    pub now_playing: Arc<Mutex<Option<(String, String)>>>,
}

impl PlayerHandle {
    pub fn spawn(initial_backend: String, media_cache: Arc<Mutex<HashMap<String, discord::MediaInfo>>>) -> Self {
        let (tx, rx) = channel::<Cmd>();
        let status = Arc::new(Mutex::new(None));
        let now_playing = Arc::new(Mutex::new(None));

        let thread_status = status.clone();
        let thread_now_playing = now_playing.clone();

        std::thread::spawn(move || {
            let mut backend = if initial_backend == "vlc" {
                player::Backend::Vlc(player::VlcPlayer::new())
            } else {
                player::Backend::Mpv(player::MpvPlayer::new())
            };
            let mut discord = discord::DiscordPresence::new();
            let mut skip_op: Option<(f64, f64)> = None;
            let mut skip_ed: Option<(f64, f64)> = None;
            let mut skipped: HashSet<&'static str> = HashSet::new();

            loop {
                match rx.recv_timeout(Duration::from_millis(800)) {
                    Ok(Cmd::Play { url, title, ep_no, referer, start, source, anime_id }) => {
                        backend.play(&url, Some(&title), referer.as_deref(), start);
                        *thread_now_playing.lock().unwrap() = Some((title.clone(), ep_no.clone()));
                        state::update_history(&source, &anime_id, &title, &ep_no);
                        skip_op = None;
                        skip_ed = None;
                        skipped.clear();
                    }
                    Ok(Cmd::TogglePause) => backend.toggle_pause(),
                    Ok(Cmd::Stop) => {
                        backend.stop();
                        *thread_now_playing.lock().unwrap() = None;
                        *thread_status.lock().unwrap() = None;
                        discord.browsing("idle");
                    }
                    Ok(Cmd::SetBackend(name)) => {
                        backend.stop();
                        backend = if name == "vlc" {
                            player::Backend::Vlc(player::VlcPlayer::new())
                        } else {
                            player::Backend::Mpv(player::MpvPlayer::new())
                        };
                    }
                    Ok(Cmd::Browsing(detail)) => discord.browsing(&detail),
                    Ok(Cmd::Seek(seconds)) => backend.seek(seconds),
                    Ok(Cmd::SetVolume(percent)) => backend.set_volume(percent),
                    Ok(Cmd::SetSpeed(rate)) => backend.set_speed(rate),
                    Ok(Cmd::CycleSubtitle) => backend.cycle_subtitle(),
                    Ok(Cmd::CycleAudio) => backend.cycle_audio(),
                    Ok(Cmd::SetSkipTimes(op, ed)) => {
                        skip_op = op;
                        skip_ed = ed;
                        skipped.clear();
                    }
                    Err(_) => {}
                }

                let now_playing = thread_now_playing.lock().unwrap().clone();
                if let Some((show, ep)) = &now_playing {
                    let t0 = std::time::Instant::now();
                    let status = backend.get_status();
                    let poll_ms = t0.elapsed().as_secs_f64() * 1000.0;
                    if poll_ms > 200.0 {
                        platform::debug_log(&format!("worker: get_status() took {poll_ms:.0}ms"));
                    }
                    *thread_status.lock().unwrap() = status;
                    if let Some(status) = &*thread_status.lock().unwrap() {
                        let info = media_cache.lock().unwrap().get(show).cloned();
                        if let Some(info) = info {
                            discord.cache_media(show, info);
                        }
                        let t0 = std::time::Instant::now();
                        discord.watching(show, ep, status.time, status.duration, status.paused);
                        let ms = t0.elapsed().as_secs_f64() * 1000.0;
                        if ms > 200.0 {
                            platform::debug_log(&format!("worker: discord.watching() took {ms:.0}ms"));
                        }

                        for (name, seg) in [("op", skip_op), ("ed", skip_ed)] {
                            if skipped.contains(name) {
                                continue;
                            }
                            if let Some((start, end)) = seg {
                                if status.time >= start && status.time < end {
                                    backend.seek(end);
                                    skipped.insert(name);
                                }
                            }
                        }

                        let key = state::position_key("anidb", show, ep);
                        if status.time > state::RESUME_MIN_SECONDS
                            && (status.duration == 0.0 || status.time < status.duration - state::RESUME_END_MARGIN)
                        {
                            let mut positions = state::load_positions();
                            positions.insert(key, status.time);
                            state::save_positions(&positions);
                        }
                    }
                }
            }
        });

        PlayerHandle { tx, status, now_playing }
    }

    pub fn play(&self, url: &str, title: &str, ep_no: &str, referer: Option<String>, start: Option<f64>, source: &str, anime_id: &str) {
        let _ = self.tx.send(Cmd::Play {
            url: url.to_string(),
            title: title.to_string(),
            ep_no: ep_no.to_string(),
            referer,
            start,
            source: source.to_string(),
            anime_id: anime_id.to_string(),
        });
    }

    pub fn toggle_pause(&self) {
        let _ = self.tx.send(Cmd::TogglePause);
    }

    pub fn stop(&self) {
        let _ = self.tx.send(Cmd::Stop);
    }

    pub fn set_backend(&self, name: &str) {
        let _ = self.tx.send(Cmd::SetBackend(name.to_string()));
    }

    pub fn browsing(&self, detail: &str) {
        let _ = self.tx.send(Cmd::Browsing(detail.to_string()));
    }

    pub fn seek(&self, seconds: f64) {
        let _ = self.tx.send(Cmd::Seek(seconds));
    }

    pub fn set_volume(&self, percent: i32) {
        let _ = self.tx.send(Cmd::SetVolume(percent));
    }

    pub fn set_speed(&self, rate: f64) {
        let _ = self.tx.send(Cmd::SetSpeed(rate));
    }

    pub fn cycle_subtitle(&self) {
        let _ = self.tx.send(Cmd::CycleSubtitle);
    }

    pub fn cycle_audio(&self) {
        let _ = self.tx.send(Cmd::CycleAudio);
    }

    pub fn set_skip_times(&self, op: Option<(f64, f64)>, ed: Option<(f64, f64)>) {
        let _ = self.tx.send(Cmd::SetSkipTimes(op, ed));
    }
}
