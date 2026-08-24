mod discord;
mod download;
mod ipc;
mod platform;
mod player;
mod sources;
mod state;
mod torrent;
mod tracker;
mod worker;
mod yuma;

use eframe::egui;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

#[derive(PartialEq, Clone, Copy)]
enum Tab {
    Discover,
    Search,
    Downloads,
}

#[derive(PartialEq, Clone, Copy)]
enum SearchSource {
    AniDb,
    Yuma,
    Nyaa,
}

#[derive(Clone, Copy)]
enum StreamSource {
    AniDb,
    Yuma,
}

struct SelectedAnime {
    title: String,
    id: String,
    source: StreamSource,
    episodes: Arc<Mutex<Vec<sources::Episode>>>,
}

struct App {
    rt: tokio::runtime::Runtime,
    http: reqwest::Client,
    tab: Tab,

    trending: Arc<Mutex<Vec<sources::Anime>>>,
    popular: Arc<Mutex<Vec<sources::Anime>>>,
    continue_watching: Arc<Mutex<Vec<sources::Anime>>>,

    search_query: String,
    search_source: SearchSource,
    anidb_results: Arc<Mutex<Vec<sources::SearchResult>>>,
    yuma_results: Arc<Mutex<Vec<sources::SearchResult>>>,
    nyaa_results: Arc<Mutex<Vec<torrent::TorrentResult>>>,

    selected: Option<SelectedAnime>,

    player: worker::PlayerHandle,
    torrent_engine: Arc<Mutex<torrent::TorrentEngine>>,
    prefs: state::Prefs,

    anilist_username: Arc<Mutex<Option<String>>>,
    anilist_client_id_input: String,
    anilist_pin_input: String,

    media_cache: Arc<Mutex<HashMap<String, discord::MediaInfo>>>,
    pending_torrent_playback: Arc<Mutex<Option<(std::path::PathBuf, String)>>>,

    downloads: Vec<Arc<download::DownloadJob>>,
    downloaded_library: Arc<Mutex<Vec<download::DownloadedShow>>>,
    seek_drag: Option<f64>,
}

impl App {
    fn new() -> Self {
        let rt = tokio::runtime::Runtime::new().unwrap();
        let prefs = state::load_prefs();
        let media_cache = Arc::new(Mutex::new(HashMap::new()));
        let player = worker::PlayerHandle::spawn(prefs.player.clone(), media_cache.clone());

        let app = App {
            rt,
            http: reqwest::Client::builder()
                .connect_timeout(Duration::from_secs(5))
                .timeout(Duration::from_secs(10))
                .build()
                .unwrap(),
            tab: Tab::Discover,
            trending: Arc::new(Mutex::new(vec![])),
            popular: Arc::new(Mutex::new(vec![])),
            continue_watching: Arc::new(Mutex::new(vec![])),
            search_query: String::new(),
            search_source: SearchSource::AniDb,
            anidb_results: Arc::new(Mutex::new(vec![])),
            yuma_results: Arc::new(Mutex::new(vec![])),
            nyaa_results: Arc::new(Mutex::new(vec![])),
            selected: None,
            player,
            torrent_engine: Arc::new(Mutex::new(torrent::TorrentEngine::new())),
            prefs,
            anilist_username: Arc::new(Mutex::new(None)),
            anilist_client_id_input: String::new(),
            anilist_pin_input: String::new(),
            media_cache,
            pending_torrent_playback: Arc::new(Mutex::new(None)),
            downloads: vec![],
            downloaded_library: Arc::new(Mutex::new(vec![])),
            seek_drag: None,
        };
        app.refresh_discover();
        app.refresh_anilist_username();
        app.refresh_downloaded_library();
        app.refresh_continue_watching();
        app.player.browsing("idle");
        app
    }

    fn refresh_downloaded_library(&self) {
        let out = self.downloaded_library.clone();
        std::thread::spawn(move || {
            *out.lock().unwrap() = download::list_downloaded();
        });
    }

    fn refresh_discover(&self) {
        let client = self.http.clone();
        let trending = self.trending.clone();
        self.rt.spawn(async move {
            if let Ok(v) = sources::discover_trending(&client).await {
                *trending.lock().unwrap() = v;
            }
        });
        let client = self.http.clone();
        let popular = self.popular.clone();
        self.rt.spawn(async move {
            if let Ok(v) = sources::discover_popular(&client).await {
                *popular.lock().unwrap() = v;
            }
        });
    }

    fn refresh_anilist_username(&self) {
        let client = self.http.clone();
        let out = self.anilist_username.clone();
        self.rt.spawn(async move {
            let name = tracker::whoami(&client).await;
            *out.lock().unwrap() = name;
        });
    }

    fn run_search(&self) {
        match self.search_source {
            SearchSource::AniDb => {
                let q = self.search_query.clone();
                let out = self.anidb_results.clone();
                std::thread::spawn(move || {
                    if let Ok(v) = sources::anidb_search(&q) {
                        *out.lock().unwrap() = v;
                    }
                });
            }
            SearchSource::Yuma => {
                let q = self.search_query.clone();
                let out = self.yuma_results.clone();
                std::thread::spawn(move || {
                    if let Ok(v) = yuma::search(&q) {
                        *out.lock().unwrap() = v;
                    }
                });
            }
            SearchSource::Nyaa => {
                let q = self.search_query.clone();
                let client = self.http.clone();
                let out = self.nyaa_results.clone();
                self.rt.spawn(async move {
                    if let Ok(v) = torrent::nyaa_search(&client, &q).await {
                        *out.lock().unwrap() = v;
                    }
                });
            }
        }
    }

    fn select_anime(&mut self, id: &str, title: &str, source: StreamSource) {
        let episodes = Arc::new(Mutex::new(vec![]));
        self.selected = Some(SelectedAnime {
            title: title.to_string(),
            id: id.to_string(),
            source,
            episodes: episodes.clone(),
        });
        let id = id.to_string();
        match source {
            StreamSource::AniDb => {
                std::thread::spawn(move || {
                    if let Ok(eps) = sources::anidb_episodes(&id) {
                        *episodes.lock().unwrap() = eps;
                    }
                });
            }
            StreamSource::Yuma => {
                std::thread::spawn(move || {
                    if let Ok(eps) = yuma::episodes(&id) {
                        *episodes.lock().unwrap() = eps;
                    }
                });
            }
        }
        self.player.browsing(title);
    }

    fn resolve_link(sel: &SelectedAnime, ep_ref: &str) -> Option<sources::WatchLink> {
        match sel.source {
            StreamSource::AniDb => sources::anidb_watch(ep_ref, false).ok().flatten(),
            StreamSource::Yuma => {
                eprintln!("[yuma] stream resolution not implemented yet, see TODO.txt");
                None
            }
        }
    }

    fn play_episode(&mut self, ep_ref: &str, ep_no: &str) {
        let Some(sel) = &self.selected else { return };
        let title = sel.title.clone();
        let source_name = match sel.source {
            StreamSource::AniDb => "anidb",
            StreamSource::Yuma => "yuma",
        };
        let key = state::position_key(source_name, &title, ep_no);
        let resume_at = state::load_positions().get(&key).copied();

        if let Some(link) = Self::resolve_link(sel, ep_ref) {
            self.player.play(&link.url, &title, ep_no, link.referer, resume_at, source_name, &sel.id);
            self.fetch_discord_cover(&title);
            self.player.set_skip_times(None, None);

            if matches!(sel.source, StreamSource::AniDb) {
                let anime_id = sel.id.clone();
                let canonical = sel.episodes.lock().unwrap().iter().position(|e| e.ep_no == ep_no).map(|i| i as i64 + 1).unwrap_or(1);
                let player = self.player.clone();
                std::thread::spawn(move || {
                    let Ok(Some(mal_id)) = sources::anidb_mal_id(&anime_id) else { return };
                    let Ok(times) = sources::ani_skip_times(&mal_id, canonical) else { return };
                    player.set_skip_times(times.op, times.ed);
                });
            }

            if self.prefs.anilist_sync {
                let client = self.http.clone();
                let t = title.clone();
                let e = ep_no.to_string();
                self.rt.spawn(async move {
                    tracker::update_progress(&client, &t, &e).await;
                });
            }
        }
    }

    fn download_episode(&mut self, ep_ref: &str, ep_no: &str) {
        let Some(sel) = &self.selected else { return };
        let title = sel.title.clone();
        let ep_no = ep_no.to_string();
        let Some(link) = Self::resolve_link(sel, ep_ref) else { return };
        let job = download::DownloadJob::new(&title, &ep_no, &link.url, link.referer);
        self.downloads.push(job.clone());
        let library = self.downloaded_library.clone();
        std::thread::spawn(move || {
            download::run_job(&job);
            *library.lock().unwrap() = download::list_downloaded();
        });
    }

    fn fetch_discord_cover(&self, title: &str) {
        if self.media_cache.lock().unwrap().contains_key(title) {
            return;
        }
        let client = self.http.clone();
        let cache = self.media_cache.clone();
        let title = title.to_string();
        self.rt.spawn(async move {
            let info = sources::discord_media_for(&client, &title).await;
            cache.lock().unwrap().insert(title, info);
        });
    }

    fn play_torrent(&mut self, magnet: &str, title: &str) {
        let engine = self.torrent_engine.clone();
        let magnet = magnet.to_string();
        let title = title.to_string();
        let pending = self.pending_torrent_playback.clone();
        std::thread::spawn(move || {
            let mut engine = engine.lock().unwrap();
            if !engine.ensure_running() {
                eprintln!("[torrent] qbittorrent-nox failed to start (is it installed?)");
                return;
            }
            let Some(hash) = engine.add_magnet(&magnet) else { return };
            if let Some(path) = engine.wait_for_buffer(&hash, 3.0, Duration::from_secs(120)) {
                *pending.lock().unwrap() = Some((path, title));
            } else {
                eprintln!("[torrent] timed out waiting for enough of the torrent to buffer");
            }
        });
    }

    fn poll_torrent_handoff(&mut self) {
        if let Some((path, title)) = self.pending_torrent_playback.lock().unwrap().take() {
            let url = format!("file://{}", path.display());
            self.player.play(&url, &title, "1", None, None, "nyaa", &title);
            self.fetch_discord_cover(&title);
        }
    }

    fn anime_grid(&self, ui: &mut egui::Ui, id_salt: &str, list: &[sources::Anime]) -> Option<String> {
        let mut clicked = None;
        egui::ScrollArea::horizontal().id_salt(id_salt).show(ui, |ui| {
            ui.horizontal(|ui| {
                for anime in list {
                    let frame = egui::Frame::none()
                        .fill(theme::SURFACE)
                        .rounding(10.0)
                        .inner_margin(10.0)
                        .show(ui, |ui| {
                            ui.vertical(|ui| {
                                ui.set_width(150.0);
                                let (rect, _) = ui.allocate_exact_size(egui::vec2(150.0, 210.0), egui::Sense::hover());
                                if ui.is_rect_visible(rect) {
                                    if let Some(cover) = &anime.cover {
                                        egui::Image::new(cover).rounding(6.0).paint_at(ui, rect);
                                    } else {
                                        ui.painter().rect_filled(rect, 6.0, theme::SURFACE_HOVER);
                                    }
                                } else {
                                    ui.painter().rect_filled(rect, 6.0, theme::SURFACE_HOVER);
                                }
                                ui.add_space(4.0);
                                ui.label(egui::RichText::new(&anime.title).strong().size(14.0));
                                if let Some(score) = anime.score {
                                    ui.label(
                                        egui::RichText::new(format!("★ {score}% rated")).size(12.0).color(theme::MUTED),
                                    );
                                }
                            });
                        });
                    let resp = ui.interact(frame.response.rect, ui.id().with(&anime.title), egui::Sense::click());
                    let resp = resp.on_hover_cursor(egui::CursorIcon::PointingHand).on_hover_text(if anime.description.is_empty() {
                        anime.title.clone()
                    } else {
                        anime.description.clone()
                    });
                    if resp.clicked() {
                        clicked = Some(anime.title.clone());
                    }
                }
            });
        });
        clicked
    }

    fn open_in_search(&mut self, title: &str) {
        self.tab = Tab::Search;
        self.search_source = SearchSource::AniDb;
        self.search_query = title.to_string();
        self.selected = None;
        self.run_search();
    }

    fn resume_continue_watching(&mut self, title: &str) {
        let ep_no = self
            .continue_watching
            .lock()
            .unwrap()
            .iter()
            .find(|a| a.title == title)
            .and_then(|a| a.description.strip_prefix("continue from episode "))
            .map(|s| s.to_string());

        if let Some(ep_no) = &ep_no {
            let already_downloaded = download::list_downloaded()
                .into_iter()
                .find(|s| s.title == title)
                .map(|s| s.episodes.contains(ep_no))
                .unwrap_or(false);
            if already_downloaded {
                let path = download::dest_path(title, ep_no);
                self.player.play(&format!("file://{}", path.display()), title, ep_no, None, None, "downloaded", title);
                self.fetch_discord_cover(title);
                return;
            }
        }
        self.open_in_search(title);
    }

    fn refresh_continue_watching(&self) {
        let history = state::read_history();
        let client = self.http.clone();
        let out = self.continue_watching.clone();
        self.rt.spawn(async move {
            let mut list = vec![];
            for entry in history.iter().rev().take(20) {
                let cover = sources::cover_for_title(&client, &entry.anime_title).await;
                tokio::time::sleep(Duration::from_millis(350)).await;
                list.push(sources::Anime {
                    title: entry.anime_title.clone(),
                    episodes: None,
                    score: None,
                    status: None,
                    genres: vec![],
                    description: format!("continue from episode {}", entry.ep_no),
                    cover,
                });
            }
            *out.lock().unwrap() = list;
        });
    }
}

mod theme {
    use eframe::egui::Color32;
    pub const BG: Color32 = Color32::from_rgb(18, 19, 21);
    pub const SURFACE: Color32 = Color32::from_rgb(30, 31, 34);
    pub const SURFACE_HOVER: Color32 = Color32::from_rgb(42, 43, 47);
    pub const TEXT: Color32 = Color32::from_rgb(230, 228, 222);
    pub const MUTED: Color32 = Color32::from_rgb(158, 156, 150);
    pub const ACCENT: Color32 = Color32::from_rgb(232, 158, 90);
    pub const ACCENT_DIM: Color32 = Color32::from_rgb(107, 74, 43);
}

fn apply_theme(ctx: &egui::Context) {
    let mut style = (*ctx.style()).clone();
    style.visuals.dark_mode = true;
    style.visuals.override_text_color = Some(theme::TEXT);
    style.visuals.panel_fill = theme::BG;
    style.visuals.window_fill = theme::SURFACE;
    style.visuals.extreme_bg_color = egui::Color32::from_rgb(10, 10, 11);
    style.visuals.widgets.noninteractive.bg_fill = theme::SURFACE;
    style.visuals.widgets.inactive.bg_fill = theme::SURFACE;
    style.visuals.widgets.hovered.bg_fill = theme::SURFACE_HOVER;
    style.visuals.widgets.active.bg_fill = theme::ACCENT_DIM;
    style.visuals.selection.bg_fill = theme::ACCENT.gamma_multiply(0.4);
    style.visuals.hyperlink_color = theme::ACCENT;

    style.visuals.widgets.hovered.fg_stroke = egui::Stroke::new(1.5, theme::ACCENT);
    style.visuals.widgets.active.fg_stroke = egui::Stroke::new(1.5, theme::TEXT);
    style.visuals.selection.stroke = egui::Stroke::new(2.0, theme::ACCENT);

    style.spacing.item_spacing = egui::vec2(10.0, 10.0);
    style.spacing.button_padding = egui::vec2(14.0, 8.0);
    style.spacing.interact_size.y = 32.0;

    for rounding in [
        &mut style.visuals.widgets.noninteractive.rounding,
        &mut style.visuals.widgets.inactive.rounding,
        &mut style.visuals.widgets.hovered.rounding,
        &mut style.visuals.widgets.active.rounding,
    ] {
        *rounding = egui::Rounding::same(8.0);
    }

    for (_, font_id) in style.text_styles.iter_mut() {
        font_id.size *= 1.15;
    }

    ctx.set_style(style);
}

impl eframe::App for App {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        let frame_start = std::time::Instant::now();
        self.poll_torrent_handoff();
        ctx.request_repaint();

        egui::TopBottomPanel::top("top").show(ctx, |ui| {
            ui.horizontal(|ui| {
                ui.heading("aniani");
                ui.separator();
                ui.selectable_value(&mut self.tab, Tab::Discover, "discover");
                ui.selectable_value(&mut self.tab, Tab::Search, "search");
                ui.selectable_value(&mut self.tab, Tab::Downloads, "downloads");
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    egui::ComboBox::from_id_salt("player_backend")
                        .selected_text(&self.prefs.player)
                        .show_ui(ui, |ui| {
                            if ui.selectable_label(self.prefs.player == "mpv", "mpv").clicked() {
                                self.prefs.player = "mpv".into();
                                self.player.set_backend("mpv");
                            }
                            if ui.selectable_label(self.prefs.player == "vlc", "vlc").clicked() {
                                self.prefs.player = "vlc".into();
                                self.player.set_backend("vlc");
                            }
                        });
                });
            });
        });

        let now_playing = self.player.now_playing.lock().unwrap().clone();
        if let Some((show, ep)) = now_playing {
            egui::TopBottomPanel::bottom("player").show(ctx, |ui| {
                ui.horizontal(|ui| {
                    ui.label(format!("{show} · episode {ep}"));
                    let status = self.player.status.lock().unwrap().clone();
                    if let Some(status) = &status {
                        if ui.button(if status.paused { "▶" } else { "⏸" }).clicked() {
                            self.player.toggle_pause();
                        }
                        let mut pos = self.seek_drag.unwrap_or(status.time);
                        let slider = ui.add(
                            egui::Slider::new(&mut pos, 0.0..=status.duration.max(1.0))
                                .show_value(false)
                                .custom_formatter(|_, _| String::new()),
                        );
                        if slider.dragged() {
                            self.seek_drag = Some(pos);
                        }
                        if slider.drag_stopped() {
                            self.player.seek(pos);
                            self.seek_drag = None;
                        }
                        ui.label(format!("{:.0}s / {:.0}s", status.time, status.duration));
                    }
                    if ui.button("stop").clicked() {
                        self.player.stop();
                    }
                });
                ui.horizontal(|ui| {
                    ui.label("vol");
                    if ui.add(egui::Slider::new(&mut self.prefs.volume, 0..=150).show_value(true)).changed() {
                        self.player.set_volume(self.prefs.volume);
                    }
                    ui.label("speed");
                    if ui.add(egui::Slider::new(&mut self.prefs.speed, 0.5..=2.0).show_value(true)).changed() {
                        self.player.set_speed(self.prefs.speed);
                    }
                    if self.prefs.player == "mpv" {
                        if ui.button("sub").clicked() {
                            self.player.cycle_subtitle();
                        }
                        if ui.button("audio").clicked() {
                            self.player.cycle_audio();
                        }
                    }
                });
            });
        }

        egui::CentralPanel::default().show(ctx, |ui| {
            egui::ScrollArea::vertical().show(ui, |ui| match self.tab {
            Tab::Discover => {
                let mut clicked = None;
                let mut continue_watching_clicked = None;
                let continue_watching = self.continue_watching.lock().unwrap().clone();
                if !continue_watching.is_empty() {
                    ui.heading("continue watching");
                    if let Some(t) = self.anime_grid(ui, "continue_watching", &continue_watching) {
                        continue_watching_clicked = Some(t);
                    }
                    ui.add_space(12.0);
                }
                ui.heading("trending");
                let trending = self.trending.lock().unwrap().clone();
                if let Some(t) = self.anime_grid(ui, "trending", &trending) {
                    clicked = Some(t);
                }
                ui.add_space(12.0);
                ui.heading("popular");
                let popular = self.popular.lock().unwrap().clone();
                if let Some(t) = self.anime_grid(ui, "popular", &popular) {
                    clicked = Some(t);
                }
                ui.add_space(20.0);
                if let Some(title) = continue_watching_clicked {
                    self.resume_continue_watching(&title);
                }
                if let Some(title) = clicked {
                    self.open_in_search(&title);
                }

                ui.separator();
                ui.heading("anilist");
                let username = self.anilist_username.lock().unwrap().clone();
                if let Some(name) = username {
                    ui.label(format!("connected as {name}"));
                    ui.checkbox(&mut self.prefs.anilist_sync, "sync watched episodes");
                    if ui.button("disconnect").clicked() {
                        tracker::clear_token();
                        *self.anilist_username.lock().unwrap() = None;
                    }
                } else {
                    ui.label("paste your AniList client id, open the authorize link, then paste the token back:");
                    ui.horizontal(|ui| {
                        ui.text_edit_singleline(&mut self.anilist_client_id_input);
                        if ui.button("open authorize page").clicked() && !self.anilist_client_id_input.is_empty() {
                            let _ = open::that(tracker::authorize_url(&self.anilist_client_id_input));
                        }
                    });
                    ui.horizontal(|ui| {
                        ui.text_edit_singleline(&mut self.anilist_pin_input);
                        if ui.button("connect").clicked() && !self.anilist_pin_input.is_empty() {
                            tracker::save_token(&self.anilist_pin_input);
                            self.refresh_anilist_username();
                        }
                    });
                }
            }
            Tab::Search => {
                ui.horizontal(|ui| {
                    ui.selectable_value(&mut self.search_source, SearchSource::AniDb, "anidb (stream)");
                    ui.selectable_value(&mut self.search_source, SearchSource::Yuma, "aniwatch (stream, search only)");
                    ui.selectable_value(&mut self.search_source, SearchSource::Nyaa, "nyaa (torrent)");
                });
                ui.horizontal(|ui| {
                    let resp = ui.text_edit_singleline(&mut self.search_query);
                    if (resp.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter))) || ui.button("search").clicked() {
                        self.run_search();
                    }
                });
                ui.separator();

                match self.search_source {
                    SearchSource::AniDb => {
                        let results = self.anidb_results.lock().unwrap().clone();
                        for r in &results {
                            if ui.selectable_label(false, &r.title).clicked() {
                                self.select_anime(&r.id, &r.title, StreamSource::AniDb);
                            }
                        }
                    }
                    SearchSource::Yuma => {
                        let results = self.yuma_results.lock().unwrap().clone();
                        for r in &results {
                            if ui.selectable_label(false, &r.title).clicked() {
                                self.select_anime(&r.id, &r.title, StreamSource::Yuma);
                            }
                        }
                    }
                    SearchSource::Nyaa => {
                        let results = self.nyaa_results.lock().unwrap().clone();
                        for r in &results {
                            ui.horizontal(|ui| {
                                ui.label(format!("↑{} ↓{} {}", r.seeders, r.leechers, r.size));
                                if ui.button(&r.title).clicked() {
                                    self.play_torrent(&r.magnet, &r.title);
                                }
                            });
                        }
                    }
                }

                if let Some(sel) = &self.selected {
                    let sel_title = sel.title.clone();
                    let episodes = sel.episodes.lock().unwrap().clone();
                    ui.separator();
                    ui.heading(&sel_title);
                    let mut to_play: Option<(String, String)> = None;
                    let mut to_download: Option<(String, String)> = None;
                    for ep in &episodes {
                        ui.horizontal(|ui| {
                            if ui.button(format!("episode {}", ep.ep_no)).clicked() {
                                to_play = Some((ep.ep_ref.clone(), ep.ep_no.clone()));
                            }
                            if download::is_downloaded(&sel_title, &ep.ep_no) {
                                ui.label(egui::RichText::new("downloaded").color(theme::MUTED));
                            } else if ui.button("download").clicked() {
                                to_download = Some((ep.ep_ref.clone(), ep.ep_no.clone()));
                            }
                        });
                    }
                    if let Some((ep_ref, ep_no)) = to_play {
                        self.play_episode(&ep_ref, &ep_no);
                    }
                    if let Some((ep_ref, ep_no)) = to_download {
                        self.download_episode(&ep_ref, &ep_no);
                    }
                }
            }
            Tab::Downloads => {
                let active: Vec<Arc<download::DownloadJob>> = self
                    .downloads
                    .iter()
                    .filter(|j| matches!(*j.status.lock().unwrap(), download::JobStatus::Queued | download::JobStatus::Downloading))
                    .cloned()
                    .collect();
                if !active.is_empty() {
                    ui.heading("downloading");
                    for job in &active {
                        ui.horizontal(|ui| {
                            let progress = *job.progress.lock().unwrap();
                            ui.label(format!("{} · episode {}", job.anime_title, job.ep_no));
                            ui.add(egui::ProgressBar::new(progress as f32).desired_width(120.0));
                            if ui.button("cancel").clicked() {
                                job.cancel();
                            }
                        });
                    }
                    ui.separator();
                }

                ui.heading("downloaded");
                let library = self.downloaded_library.lock().unwrap().clone();
                if library.is_empty() {
                    ui.label(egui::RichText::new("nothing downloaded yet").color(theme::MUTED));
                }
                for show in &library {
                    ui.collapsing(&show.title, |ui| {
                        for ep in &show.episodes {
                            ui.horizontal(|ui| {
                                ui.label(format!("episode {ep}"));
                                if ui.button("play").clicked() {
                                    let path = download::dest_path(&show.title, ep);
                                    self.player.play(&format!("file://{}", path.display()), &show.title, ep, None, None, "downloaded", &show.title);
                                }
                                if ui.button("delete").clicked() {
                                    download::delete_episode(&show.title, ep);
                                    self.refresh_downloaded_library();
                                }
                            });
                        }
                    });
                }
            }
            });
        });

        let elapsed = frame_start.elapsed();
        if elapsed > Duration::from_millis(100) {
            platform::debug_log(&format!("slow frame: {:.0}ms, tab={:?}", elapsed.as_secs_f64() * 1000.0, self.tab as i32));
        }
    }

    fn on_exit(&mut self) {
        state::save_prefs(&self.prefs);
    }
}

fn main() -> eframe::Result<()> {
    eframe::run_native(
        "aniani",
        eframe::NativeOptions::default(),
        Box::new(|cc| {
            egui_extras::install_image_loaders(&cc.egui_ctx);
            apply_theme(&cc.egui_ctx);
            Ok(Box::new(App::new()))
        }),
    )
}
