use eframe::egui;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use crate::state;
use crate::theme;
use crate::worker;

#[derive(Clone, Default)]
struct VlcStatus {
    time: f64,
    duration: f64,
    paused: bool,
}

#[derive(Clone, Default)]
struct Shared {
    title: String,
    detail: String,
    cover: Option<String>,
    enabled: bool,
    vlc_host: String,
    vlc_port: u16,
    vlc_password: String,
}

fn vlc_status(client: &reqwest::blocking::Client, host: &str, port: u16, password: &str) -> Option<VlcStatus> {
    let url = format!("http://{host}:{port}/requests/status.json");
    let resp = client.get(&url).basic_auth("", Some(password)).send().ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let data: serde_json::Value = resp.json().ok()?;
    Some(VlcStatus {
        time: data.get("time").and_then(|v| v.as_f64()).unwrap_or(0.0),
        duration: data.get("length").and_then(|v| v.as_f64()).unwrap_or(0.0),
        paused: data.get("state").and_then(|v| v.as_str()) != Some("playing"),
    })
}

async fn tvmaze_search(client: &reqwest::Client, query: &str) -> Option<(String, Option<String>)> {
    let url = "https://api.tvmaze.com/singlesearch/shows";
    let resp = client.get(url).query(&[("q", query)]).send().await.ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let data: serde_json::Value = resp.json().await.ok()?;
    let name = data.get("name")?.as_str()?.to_string();
    let cover = data
        .get("image")
        .and_then(|i| i.get("original").or_else(|| i.get("medium")))
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());
    Some((name, cover))
}

async fn wikipedia_search(client: &reqwest::Client, query: &str) -> Option<(String, Option<String>)> {
    let search_url = "https://en.wikipedia.org/w/api.php";
    let resp = client
        .get(search_url)
        .query(&[
            ("action", "query"),
            ("list", "search"),
            ("srsearch", &format!("{query} film")),
            ("format", "json"),
            ("srlimit", "1"),
        ])
        .send()
        .await
        .ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let data: serde_json::Value = resp.json().await.ok()?;
    let title = data.get("query")?.get("search")?.as_array()?.first()?.get("title")?.as_str()?.to_string();

    let summary_url = format!("https://en.wikipedia.org/api/rest_v1/page/summary/{}", urlencoding::encode(&title));
    let resp = client.get(&summary_url).send().await.ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let data: serde_json::Value = resp.json().await.ok()?;
    let name = data.get("title")?.as_str()?.to_string();
    let cover = data.get("thumbnail").and_then(|t| t.get("source")).and_then(|v| v.as_str()).map(|s| s.to_string());
    Some((name, cover))
}

pub struct ShowsState {
    prefs: state::ShowsPrefs,
    season: String,
    episode: String,
    cover_url_input: String,
    http: reqwest::Client,
    rt: tokio::runtime::Handle,
    searching: Arc<Mutex<bool>>,
    search_error: Arc<Mutex<Option<String>>>,
    matched_name: Arc<Mutex<Option<String>>>,
    live_status: Arc<Mutex<Option<(f64, f64, bool)>>>,
    shared: Arc<Mutex<Shared>>,
    search_generation: Arc<AtomicU64>,
}

fn season_episode_label(season: &str, episode: &str) -> String {
    match (season.trim(), episode.trim()) {
        ("", "") => String::new(),
        (s, "") => format!("Season {s}"),
        ("", e) => format!("Episode {e}"),
        (s, e) => format!("S{s}E{e}"),
    }
}

impl ShowsState {
    pub fn new(http: reqwest::Client, rt: tokio::runtime::Handle, player: worker::PlayerHandle) -> Self {
        let prefs = state::load_shows_prefs();
        let detail = season_episode_label(&prefs.season, &prefs.episode);

        let shared = Arc::new(Mutex::new(Shared {
            title: prefs.title.clone(),
            detail,
            cover: prefs.cover.clone(),
            enabled: prefs.enabled,
            vlc_host: prefs.vlc_host.clone(),
            vlc_port: prefs.vlc_port.parse().unwrap_or(9091),
            vlc_password: prefs.vlc_password.clone(),
        }));
        let live_status = Arc::new(Mutex::new(None));

        {
            let shared = shared.clone();
            let live_status = live_status.clone();
            std::thread::spawn(move || {
                let client = reqwest::blocking::Client::builder().timeout(Duration::from_millis(1500)).build().unwrap();
                let mut was_enabled = false;
                loop {
                    let s = shared.lock().unwrap().clone();
                    let status = if s.vlc_port != 0 && !s.vlc_host.is_empty() {
                        vlc_status(&client, &s.vlc_host, s.vlc_port, &s.vlc_password)
                    } else {
                        None
                    };
                    *live_status.lock().unwrap() = status.as_ref().map(|st| (st.time, st.duration, st.paused));

                    if s.enabled && !s.title.trim().is_empty() {
                        let (pos, dur, paused, live) = match &status {
                            Some(st) => (st.time, st.duration, st.paused, true),
                            None => (0.0, 0.0, false, false),
                        };
                        player.watching_show(&s.title, &s.detail, s.cover.clone(), pos, dur, paused, live);
                        was_enabled = true;
                    } else if was_enabled {
                        player.browsing("idle");
                        was_enabled = false;
                    }
                    std::thread::sleep(Duration::from_secs(3));
                }
            });
        }

        ShowsState {
            season: prefs.season.clone(),
            episode: prefs.episode.clone(),
            cover_url_input: String::new(),
            matched_name: Arc::new(Mutex::new(prefs.matched_name.clone())),
            prefs,
            http,
            rt,
            searching: Arc::new(Mutex::new(false)),
            search_error: Arc::new(Mutex::new(None)),
            live_status,
            shared,
            search_generation: Arc::new(AtomicU64::new(0)),
        }
    }

    fn sync_shared(&self) {
        let detail = if self.prefs.is_movie {
            String::new()
        } else {
            season_episode_label(&self.season, &self.episode)
        };
        *self.shared.lock().unwrap() = Shared {
            title: self.prefs.title.clone(),
            detail,
            cover: self.prefs.cover.clone(),
            enabled: self.prefs.enabled,
            vlc_host: self.prefs.vlc_host.clone(),
            vlc_port: self.prefs.vlc_port.parse().unwrap_or(0),
            vlc_password: self.prefs.vlc_password.clone(),
        };
    }

    fn persist(&mut self) {
        self.prefs.season = self.season.clone();
        self.prefs.episode = self.episode.clone();
        self.prefs.matched_name = self.matched_name.lock().unwrap().clone();
        state::save_shows_prefs(&self.prefs);
    }

    fn search_cover(&self) {
        if self.prefs.title.trim().is_empty() {
            return;
        }
        *self.searching.lock().unwrap() = true;
        *self.search_error.lock().unwrap() = None;
        let client = self.http.clone();
        let title = self.prefs.title.clone();
        let is_movie = self.prefs.is_movie;
        let searching = self.searching.clone();
        let error = self.search_error.clone();
        let matched_name = self.matched_name.clone();
        let shared = self.shared.clone();
        let generation = self.search_generation.clone();
        let this_generation = generation.fetch_add(1, Ordering::SeqCst) + 1;
        self.rt.spawn(async move {
            let result = if is_movie { wikipedia_search(&client, &title).await } else { tvmaze_search(&client, &title).await };
            if generation.load(Ordering::SeqCst) != this_generation {
                return;
            }
            match result {
                Some((name, cover)) => {
                    *matched_name.lock().unwrap() = Some(name);
                    shared.lock().unwrap().cover = cover;
                }
                None => {
                    let source = if is_movie { "wikipedia" } else { "tvmaze" };
                    *error.lock().unwrap() = Some(format!("no match found on {source}"))
                }
            }
            *searching.lock().unwrap() = false;
        });
    }

    pub fn ui(&mut self, ui: &mut egui::Ui) {
        ui.label(
            egui::RichText::new("track a movie/tv show you're watching outside aniani -- manual title/season/episode, cover from tvmaze or wikipedia, discord presence with live position if vlc's http interface is reachable")
                .weak(),
        );
        ui.separator();

        let mut changed = false;
        ui.horizontal(|ui| {
            if ui.selectable_label(!self.prefs.is_movie, "show").clicked() && self.prefs.is_movie {
                self.prefs.is_movie = false;
                changed = true;
            }
            if ui.selectable_label(self.prefs.is_movie, "movie").clicked() && !self.prefs.is_movie {
                self.prefs.is_movie = true;
                self.season.clear();
                self.episode.clear();
                changed = true;
            }
        });
        ui.horizontal(|ui| {
            ui.label("title");
            if ui.text_edit_singleline(&mut self.prefs.title).changed() {
                changed = true;
            }
        });
        ui.horizontal(|ui| {
            if !self.prefs.is_movie {
                ui.label("season");
                if ui.add(egui::TextEdit::singleline(&mut self.season).desired_width(50.0)).changed() {
                    changed = true;
                }
                ui.label("episode");
                if ui.add(egui::TextEdit::singleline(&mut self.episode).desired_width(50.0)).changed() {
                    changed = true;
                }
            }
            if ui.button("search cover").clicked() {
                self.search_cover();
            }
        });

        if *self.searching.lock().unwrap() {
            let source = if self.prefs.is_movie { "wikipedia" } else { "tvmaze" };
            ui.label(egui::RichText::new(format!("searching {source}...")).color(theme::MUTED));
        }
        if let Some(err) = self.search_error.lock().unwrap().clone() {
            ui.colored_label(theme::ERROR, err);
        }
        if let Some(name) = self.matched_name.lock().unwrap().clone() {
            ui.label(egui::RichText::new(format!("matched: {name}")).weak());
        }
        if let Some(cover) = self.prefs.cover.clone() {
            ui.add(egui::Image::from_uri(&cover).max_height(160.0).rounding(4.0));
            if ui.small_button("clear cover").clicked() {
                self.prefs.cover = None;
                *self.matched_name.lock().unwrap() = None;
                changed = true;
            }
        }

        ui.horizontal(|ui| {
            ui.label("cover url");
            ui.text_edit_singleline(&mut self.cover_url_input);
            if ui.button("set cover").clicked() && !self.cover_url_input.trim().is_empty() {
                self.prefs.cover = Some(self.cover_url_input.trim().to_string());
                self.shared.lock().unwrap().cover = self.prefs.cover.clone();
                self.cover_url_input.clear();
                changed = true;
            }
            if ui.button("browse...").clicked() {
                if let Some(path) = rfd::FileDialog::new()
                    .add_filter("image", &["png", "jpg", "jpeg", "gif", "webp", "bmp"])
                    .pick_file()
                {
                    self.prefs.cover = Some(format!("file://{}", path.display()));
                    self.shared.lock().unwrap().cover = self.prefs.cover.clone();
                    changed = true;
                }
            }
        });

        ui.separator();
        if ui.checkbox(&mut self.prefs.enabled, "set discord presence").clicked() {
            changed = true;
        }
        ui.label(if self.prefs.enabled { "presence on -- updates every few seconds" } else { "presence off" });

        ui.separator();
        ui.collapsing("vlc connection", |ui| {
            ui.horizontal(|ui| {
                ui.label("host");
                if ui.text_edit_singleline(&mut self.prefs.vlc_host).changed() {
                    changed = true;
                }
            });
            ui.horizontal(|ui| {
                ui.label("port");
                if ui.text_edit_singleline(&mut self.prefs.vlc_port).changed() {
                    changed = true;
                }
            });
            ui.horizontal(|ui| {
                ui.label("password");
                if ui.add(egui::TextEdit::singleline(&mut self.prefs.vlc_password).password(true)).changed() {
                    changed = true;
                }
            });
            match *self.live_status.lock().unwrap() {
                Some((time, duration, paused)) => {
                    ui.label(format!("connected -- {time:.0}s / {duration:.0}s, paused={paused}"));
                }
                None => {
                    ui.label(egui::RichText::new("not connected -- vlc must be running with the http interface enabled").weak());
                }
            }
        });

        if changed {
            self.sync_shared();
            self.persist();
        }
    }
}
