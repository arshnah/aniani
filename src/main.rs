#![windows_subsystem = "windows"]

mod discord;
mod download;
mod ipc;
mod mal;
mod mangadex;
mod platform;
mod player;
mod reader;
mod shows;
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
    History,
    Library,
    Reader,
    Shows,
    Settings,
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

struct PendingPlay {
    link: sources::WatchLink,
    sel: SelectedAnime,
    ep_no: String,
    canonical: i64,
}

#[derive(Default)]
struct GridActions {
    clicked: Option<sources::Anime>,
    browse: Option<sources::Anime>,
    remove: Option<sources::Anime>,
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
    searching: Arc<Mutex<bool>>,
    search_dirty_at: Option<std::time::Instant>,
    /// When set, the next anidb search results should be matched against this title and
    /// the best match auto-selected, skipping the manual result pick (discover -> watch).
    auto_select: Option<String>,
    yuma_results: Arc<Mutex<Vec<sources::SearchResult>>>,
    nyaa_results: Arc<Mutex<Vec<torrent::TorrentResult>>>,

    selected: Option<SelectedAnime>,

    player: worker::PlayerHandle,
    torrent_engine: Arc<Mutex<torrent::TorrentEngine>>,
    prefs: state::Prefs,

    anilist_username: Arc<Mutex<Option<String>>>,
    anilist_client_id_input: String,
    anilist_pin_input: String,
    anilist_list: Arc<Mutex<Vec<tracker::ListEntry>>>,

    mal_username: Arc<Mutex<Option<String>>>,
    mal_code_input: String,
    mal_code_verifier: String,

    media_cache: Arc<Mutex<HashMap<String, discord::MediaInfo>>>,
    pending_torrent_playback: Arc<Mutex<Option<(std::path::PathBuf, String)>>>,
    pending_play: Arc<Mutex<Option<PendingPlay>>>,

    downloads: Vec<Arc<download::DownloadJob>>,
    downloaded_library: Arc<Mutex<Vec<download::DownloadedShow>>>,
    pending_import: Arc<Mutex<Vec<std::path::PathBuf>>>,
    import_title: String,
    import_labels: Vec<String>,
    editing_episode: Option<(String, String)>,
    edit_label_input: String,
    editing_show: Option<String>,
    edit_show_input: String,
    library: Arc<Mutex<Vec<state::LibraryEntry>>>,
    library_category_input: String,
    seek_drag: Option<f64>,
    update_available: Arc<Mutex<Option<String>>>,
    anime_detail: Option<sources::Anime>,
    selected_episodes: std::collections::HashSet<String>,
    discover_genre: Option<String>,
    discover_sort: String,
    filtered_results: Arc<Mutex<Vec<sources::Anime>>>,
    reader: reader::ReaderState,
    shows: shows::ShowsState,
}

impl App {
    fn new() -> Self {
        let rt = tokio::runtime::Runtime::new().unwrap();
        let prefs = state::load_prefs();
        let media_cache = Arc::new(Mutex::new(HashMap::new()));
        let player = worker::PlayerHandle::spawn(prefs.player.clone(), media_cache.clone());
        let http = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(10))
            .build()
            .unwrap();
        let reader = reader::ReaderState::new(http.clone(), rt.handle().clone(), player.clone());
        let shows = shows::ShowsState::new(http.clone(), rt.handle().clone(), player.clone());

        let app = App {
            rt,
            http,
            tab: Tab::Discover,
            trending: Arc::new(Mutex::new(vec![])),
            popular: Arc::new(Mutex::new(vec![])),
            continue_watching: Arc::new(Mutex::new(vec![])),
            search_query: String::new(),
            search_source: SearchSource::AniDb,
            anidb_results: Arc::new(Mutex::new(vec![])),
            searching: Arc::new(Mutex::new(false)),
            search_dirty_at: None,
            auto_select: None,
            yuma_results: Arc::new(Mutex::new(vec![])),
            nyaa_results: Arc::new(Mutex::new(vec![])),
            selected: None,
            player,
            torrent_engine: Arc::new(Mutex::new(torrent::TorrentEngine::new())),
            prefs,
            anilist_username: Arc::new(Mutex::new(None)),
            anilist_client_id_input: String::new(),
            anilist_pin_input: String::new(),
            anilist_list: Arc::new(Mutex::new(vec![])),
            mal_username: Arc::new(Mutex::new(None)),
            mal_code_input: String::new(),
            mal_code_verifier: String::new(),
            media_cache,
            pending_torrent_playback: Arc::new(Mutex::new(None)),
            pending_play: Arc::new(Mutex::new(None)),
            downloads: vec![],
            downloaded_library: Arc::new(Mutex::new(vec![])),
            pending_import: Arc::new(Mutex::new(vec![])),
            import_title: String::new(),
            import_labels: vec![],
            editing_episode: None,
            edit_label_input: String::new(),
            editing_show: None,
            edit_show_input: String::new(),
            library: Arc::new(Mutex::new(state::read_library())),
            library_category_input: String::new(),
            seek_drag: None,
            update_available: Arc::new(Mutex::new(None)),
            anime_detail: None,
            selected_episodes: std::collections::HashSet::new(),
            discover_genre: None,
            discover_sort: "TRENDING_DESC".to_string(),
            filtered_results: Arc::new(Mutex::new(vec![])),
            reader,
            shows,
        };
        app.refresh_discover();
        app.refresh_anilist_username();
        app.refresh_anilist_list();
        app.refresh_mal_username();
        download::cleanup_orphaned_downloads();
        app.refresh_downloaded_library();
        app.refresh_continue_watching();
        app.check_for_update();
        app.player.browsing("idle");
        app
    }

    fn refresh_filtered(&self) {
        let client = self.http.clone();
        let out = self.filtered_results.clone();
        let sort = self.discover_sort.clone();
        let genre = self.discover_genre.clone();
        self.rt.spawn(async move {
            if let Ok(v) = sources::discover_filtered(&client, &sort, genre.as_deref()).await {
                *out.lock().unwrap() = v;
            }
        });
    }

    fn check_for_update(&self) {
        let client = self.http.clone();
        let out = self.update_available.clone();
        self.rt.spawn(async move {
            if let Some(latest) = sources::latest_release_tag(&client).await {
                if sources::is_newer_version(&latest, env!("CARGO_PKG_VERSION")) {
                    *out.lock().unwrap() = Some(latest);
                }
            }
        });
    }

    fn refresh_downloaded_library(&self) {
        *self.downloaded_library.lock().unwrap() = download::list_downloaded();
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

    fn refresh_mal_username(&self) {
        if !mal::has_token() {
            *self.mal_username.lock().unwrap() = None;
            return;
        }
        let client = self.http.clone();
        let client_id = self.prefs.mal_client_id.clone();
        let client_secret = self.prefs.mal_client_secret.clone();
        let out = self.mal_username.clone();
        self.rt.spawn(async move {
            let name = mal::whoami(&client, &client_id, &client_secret).await;
            *out.lock().unwrap() = name;
        });
    }

    fn refresh_anilist_list(&self) {
        let client = self.http.clone();
        let out = self.anilist_list.clone();
        self.rt.spawn(async move {
            let list = tracker::my_list(&client).await;
            *out.lock().unwrap() = list;
        });
    }

    fn poll_debounced_search(&mut self) {
        let Some(dirty_at) = self.search_dirty_at else { return };
        if dirty_at.elapsed() < Duration::from_millis(400) {
            return;
        }
        self.search_dirty_at = None;
        if !self.search_query.trim().is_empty() {
            self.run_search();
        }
    }

    fn run_search(&mut self) {
        // A fresh search cancels any pending auto-select from a previous one, and drops
        // stale results up front so a slow new search can't be mistaken for the previous
        // one's output (also what poll_auto_select waits on).
        self.auto_select = None;
        match self.search_source {
            SearchSource::AniDb => self.anidb_results.lock().unwrap().clear(),
            SearchSource::Yuma => self.yuma_results.lock().unwrap().clear(),
            SearchSource::Nyaa => self.nyaa_results.lock().unwrap().clear(),
        }
        *self.searching.lock().unwrap() = true;
        match self.search_source {
            SearchSource::AniDb => {
                let q = self.search_query.clone();
                let out = self.anidb_results.clone();
                let searching = self.searching.clone();
                std::thread::spawn(move || {
                    match sources::anidb_search(&q) {
                        Ok(v) => *out.lock().unwrap() = v,
                        Err(e) => platform::debug_log(&format!("anidb_search({q}) failed: {e}")),
                    }
                    *searching.lock().unwrap() = false;
                });
            }
            SearchSource::Yuma => {
                let q = self.search_query.clone();
                let out = self.yuma_results.clone();
                let searching = self.searching.clone();
                std::thread::spawn(move || {
                    match yuma::search(&q) {
                        Ok(v) => *out.lock().unwrap() = v,
                        Err(e) => platform::debug_log(&format!("yuma::search({q}) failed: {e}")),
                    }
                    *searching.lock().unwrap() = false;
                });
            }
            SearchSource::Nyaa => {
                let q = self.search_query.clone();
                let client = self.http.clone();
                let out = self.nyaa_results.clone();
                let searching = self.searching.clone();
                self.rt.spawn(async move {
                    if let Ok(v) = torrent::nyaa_search(&client, &q).await {
                        *out.lock().unwrap() = v;
                    }
                    *searching.lock().unwrap() = false;
                });
            }
        }
    }

    fn select_anime(&mut self, id: &str, title: &str, source: StreamSource) {
        self.selected_episodes.clear();
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
            StreamSource::Yuma => match yuma::watch(ep_ref, false) {
                Ok(link) => link,
                Err(e) => {
                    eprintln!("[yuma] {e}");
                    None
                }
            },
        }
    }

    fn play_episode(&mut self, ep_ref: &str, ep_no: &str) {
        let Some(sel) = &self.selected else { return };
        let title = sel.title.clone();
        let ep_ref = ep_ref.to_string();
        let ep_no = ep_no.to_string();
        let source = sel.source;
        let anime_id = sel.id.clone();
        let canonical = sel.episodes.lock().unwrap().iter().position(|e| e.ep_no == ep_no).map(|i| i as i64 + 1).unwrap_or(1);
        let pending = self.pending_play.clone();
        std::thread::spawn(move || {
            let sel = SelectedAnime { title, id: anime_id, source, episodes: Arc::new(Mutex::new(vec![])) };
            if let Some(link) = Self::resolve_link(&sel, &ep_ref) {
                *pending.lock().unwrap() = Some(PendingPlay { link, sel, ep_no, canonical });
            }
        });
    }

    fn poll_pending_play(&mut self) {
        let Some(p) = self.pending_play.lock().unwrap().take() else { return };
        let source_name = match p.sel.source {
            StreamSource::AniDb => "anidb",
            StreamSource::Yuma => "yuma",
        };
        let key = state::position_key(source_name, &p.sel.title, &p.ep_no);
        let resume_at = state::load_positions().get(&key).copied();

        self.player.play(&p.link.url, &p.sel.title, &p.ep_no, p.link.referer, resume_at, source_name, &p.sel.id);
        self.fetch_discord_cover(&p.sel.title);
        self.player.set_skip_times(None, None);

        if matches!(p.sel.source, StreamSource::AniDb) {
            let anime_id = p.sel.id.clone();
            let canonical = p.canonical;
            let player = self.player.clone();
            std::thread::spawn(move || {
                let Ok(Some(mal_id)) = sources::anidb_mal_id(&anime_id) else { return };
                let Ok(times) = sources::ani_skip_times(&mal_id, canonical) else { return };
                player.set_skip_times(times.op, times.ed);
            });
        }

        if self.prefs.anilist_sync {
            let client = self.http.clone();
            let t = p.sel.title.clone();
            let e = p.ep_no.clone();
            self.rt.spawn(async move {
                tracker::update_progress(&client, &t, &e).await;
            });
        }

        if self.prefs.mal_sync {
            let client = self.http.clone();
            let client_id = self.prefs.mal_client_id.clone();
            let client_secret = self.prefs.mal_client_secret.clone();
            let t = p.sel.title.clone();
            let e = p.ep_no.clone();
            self.rt.spawn(async move {
                mal::update_progress(&client, &client_id, &client_secret, &t, &e).await;
            });
        }
    }

    fn download_episode(&mut self, ep_ref: &str, ep_no: &str) {
        let Some(sel) = &self.selected else { return };
        if !matches!(sel.source, StreamSource::AniDb) {
            eprintln!("[yuma] download not supported yet, see TODO.txt");
            return;
        }
        let title = sel.title.clone();
        let ep_no = ep_no.to_string();
        let job = download::DownloadJob::new(&title, &ep_no, ep_ref);
        self.downloads.push(job.clone());
        let library = self.downloaded_library.clone();
        std::thread::spawn(move || {
            download::run_job(&job);
            *library.lock().unwrap() = download::list_downloaded();
        });
    }

    fn retry_download(&mut self, failed: &Arc<download::DownloadJob>) {
        let job = download::DownloadJob::new(&failed.anime_title, &failed.ep_no, &failed.ep_ref);
        self.downloads.retain(|j| !Arc::ptr_eq(j, failed));
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

    fn anime_grid(&self, ui: &mut egui::Ui, id_salt: &str, list: &[sources::Anime]) -> Option<sources::Anime> {
        self.anime_grid_with_actions(ui, id_salt, list, false).clicked
    }

    fn watch_activity_heatmap(&self, ui: &mut egui::Ui) {
        ui.heading("activity");
        let log = state::read_watch_activity();
        let today = state::today_bucket();
        const WEEKS: i64 = 18;
        const CELL: f32 = 12.0;
        const GAP: f32 = 3.0;
        let (rect, _) = ui.allocate_exact_size(egui::vec2((CELL + GAP) * WEEKS as f32, (CELL + GAP) * 7.0), egui::Sense::hover());
        let painter = ui.painter();
        for week in 0..WEEKS {
            for weekday in 0..7 {
                let days_ago = (WEEKS - 1 - week) * 7 + (6 - weekday);
                let day = today - days_ago;
                let count = log.get(&day).copied().unwrap_or(0);
                let color = match count {
                    0 => theme::SURFACE_HOVER,
                    1 => theme::ACCENT_DIM,
                    2..=3 => egui::Color32::from_rgb(180, 122, 66),
                    _ => theme::ACCENT,
                };
                let x = rect.left() + week as f32 * (CELL + GAP);
                let y = rect.top() + weekday as f32 * (CELL + GAP);
                let cell_rect = egui::Rect::from_min_size(egui::pos2(x, y), egui::vec2(CELL, CELL));
                painter.rect_filled(cell_rect, 2.0, color);
                if days_ago >= 0 {
                    let resp = ui.interact(cell_rect, ui.id().with(("activity_cell", day)), egui::Sense::hover());
                    let label = if days_ago == 0 { "today".to_string() } else { format!("{days_ago} days ago") };
                    resp.on_hover_text(format!("{label}: {count} episode{}", if count == 1 { "" } else { "s" }));
                }
            }
        }
    }

    fn anime_grid_with_actions(&self, ui: &mut egui::Ui, id_salt: &str, list: &[sources::Anime], show_actions: bool) -> GridActions {
        let mut actions = GridActions::default();
        egui::ScrollArea::horizontal().id_salt(id_salt).show(ui, |ui| {
            ui.horizontal(|ui| {
                for anime in list {
                    let mut card_resp = None;
                    egui::Frame::none().fill(theme::SURFACE).rounding(10.0).inner_margin(10.0).show(ui, |ui| {
                        ui.vertical(|ui| {
                            ui.set_width(150.0);
                            let (rect, resp) = ui.allocate_exact_size(egui::vec2(150.0, 210.0), egui::Sense::click());
                            if ui.is_rect_visible(rect) {
                                if let Some(cover) = &anime.cover {
                                    egui::Image::new(cover).rounding(6.0).paint_at(ui, rect);
                                } else {
                                    ui.painter().rect_filled(rect, 6.0, theme::SURFACE_HOVER);
                                }
                            } else {
                                ui.painter().rect_filled(rect, 6.0, theme::SURFACE_HOVER);
                            }
                            card_resp = Some(resp);
                            ui.add_space(4.0);
                            ui.label(egui::RichText::new(&anime.title).strong().size(14.0));
                            if let Some(score) = anime.score {
                                ui.label(egui::RichText::new(format!("★ {score}% rated")).size(12.0).color(theme::MUTED));
                            }
                            if show_actions {
                                ui.horizontal(|ui| {
                                    if ui.small_button("episodes").clicked() {
                                        actions.browse = Some(anime.clone());
                                    }
                                    if ui.small_button("remove").clicked() {
                                        actions.remove = Some(anime.clone());
                                    }
                                });
                            }
                        });
                    });
                    let Some(resp) = card_resp else { continue };
                    let resp = resp.on_hover_cursor(egui::CursorIcon::PointingHand).on_hover_text(if anime.description.is_empty() {
                        anime.title.clone()
                    } else {
                        anime.description.clone()
                    });
                    if resp.clicked() {
                        actions.clicked = Some(anime.clone());
                    }
                }
            });
        });
        actions
    }

    fn open_anime_detail(&mut self, anime: sources::Anime) {
        self.library_category_input =
            self.library.lock().unwrap().iter().find(|e| e.title == anime.title).map(|e| e.category.clone()).unwrap_or_default();
        self.anime_detail = Some(anime);
    }

    fn refresh_library(&self) {
        *self.library.lock().unwrap() = state::read_library();
    }

    fn open_in_search(&mut self, title: &str) {
        self.tab = Tab::Search;
        self.search_source = SearchSource::AniDb;
        self.search_query = title.to_string();
        self.selected = None;
        self.run_search();
        // After the results land, jump straight into the best title match's episodes.
        self.auto_select = Some(title.to_string());
    }

    /// Picks the anidb result that best matches `want`, or None when nothing is close
    /// enough to auto-select and the user should pick manually.
    ///
    /// Season-aware: sequels are separate entries on both catalogs but named
    /// inconsistently ("Season 2", "2nd season", "II"). A result is only eligible when
    /// its season agrees with the wanted one -- wanting Season 2 never auto-picks the
    /// unmarked first season, and a marker-less want never auto-picks an explicit
    /// sequel.
    fn best_result_match<'a>(want: &str, results: &'a [sources::SearchResult]) -> Option<&'a sources::SearchResult> {
        /// Splits "Mushoku Tensei II" into ("Mushoku Tensei", Some(2)). Understands
        /// "season N", "Nth season", standalone roman numerals II-IV, and a trailing
        /// standalone number.
        fn split_season(title: &str) -> (String, Option<u32>) {
            let patterns = [
                regex::Regex::new(r"(?i)\bseason\s*(\d+)\b").unwrap(),
                regex::Regex::new(r"(?i)\b(\d+)(?:nd|rd|th)\s+season\b").unwrap(),
                regex::Regex::new(r"\b(II|III|IV)\b").unwrap(),
                regex::Regex::new(r"\s(\d+)$").unwrap(),
            ];
            let romans = [("II", 2u32), ("III", 3), ("IV", 4)];
            for (i, re) in patterns.iter().enumerate() {
                if let Some(c) = re.captures(title) {
                    let num = c.get(1).unwrap();
                    let whole = c.get(0).unwrap();
                    let n = match i {
                        2 => romans.iter().find(|(r, _)| r.eq_ignore_ascii_case(num.as_str())).map(|(_, v)| *v),
                        _ => num.as_str().parse().ok(),
                    };
                    if let Some(n) = n {
                        // Strip the whole matched span ("2nd Season", not just "2"),
                        // otherwise the base keeps fragments like "nd Season".
                        let mut base = String::with_capacity(title.len());
                        base.push_str(&title[..whole.start()]);
                        base.push_str(&title[whole.end()..]);
                        return (base, Some(n));
                    }
                }
            }
            (title.to_string(), None)
        }
        fn norm(s: &str) -> String {
            s.to_lowercase()
                .chars()
                .map(|c| if c.is_alphanumeric() { c } else { ' ' })
                .collect::<String>()
                .split_whitespace()
                .collect::<Vec<_>>()
                .join(" ")
        }

        let (w_raw, w_season) = split_season(want);
        let w = norm(&w_raw);

        let mut best: Option<(u8, &sources::SearchResult)> = None;
        for r in results {
            let (t_raw, t_season) = split_season(&r.title);
            if w_season != t_season {
                continue;
            }
            let t = norm(&t_raw);
            let score: u8 = if t == w {
                3
            } else if t.starts_with(&w) || w.starts_with(&t) {
                2
            } else if t.contains(&w) || w.contains(&t) {
                1
            } else {
                continue;
            };
            if best.map(|(b, _)| score > b).unwrap_or(true) {
                best = Some((score, r));
            }
        }
        best.map(|(_, r)| r)
    }

    /// Fires the pending auto-select once anidb results are on screen. Only touches
    /// anidb results while anidb is the active source, and gives up (leaving the manual
    /// list) when nothing matches well enough.
    fn poll_auto_select(&mut self) {
        let Some(want) = self.auto_select.clone() else { return };
        if self.search_source != SearchSource::AniDb || self.selected.is_some() {
            self.auto_select = None;
            return;
        }
        let results = self.anidb_results.lock().unwrap();
        if results.is_empty() {
            return; // search still in flight, keep waiting
        }
        if let Some(best) = Self::best_result_match(&want, &results) {
            let id = best.id.clone();
            let title = best.title.clone();
            drop(results);
            self.select_anime(&id, &title, StreamSource::AniDb);
        }
        self.auto_select = None;
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
            if let Some(path) = download::find_episode_file(title, ep_no) {
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
    pub const ERROR: Color32 = Color32::from_rgb(214, 112, 100);
}

fn fmt_eta(d: std::time::Duration) -> String {
    let secs = d.as_secs();
    if secs >= 3600 {
        format!("{}h{}m", secs / 3600, (secs % 3600) / 60)
    } else if secs >= 60 {
        format!("{}m{}s", secs / 60, secs % 60)
    } else {
        format!("{secs}s")
    }
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

    style.visuals.widgets.hovered.fg_stroke = egui::Stroke::new(1.5_f32, theme::ACCENT);
    style.visuals.widgets.active.fg_stroke = egui::Stroke::new(1.5_f32, theme::TEXT);
    style.visuals.selection.stroke = egui::Stroke::new(2.0_f32, theme::ACCENT);

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
        self.poll_pending_play();
        self.poll_auto_select();
        self.poll_debounced_search();
        ctx.request_repaint();

        if ctx.input(|i| i.viewport().close_requested()) {
            // Close means quit, full stop: stop playback (kills mpv/vlc), save prefs,
            // then hard-exit so nothing can linger -- no tokio drain waits, no detached
            // worker, no wgpu teardown limbo. The old hide-instead-of-close trick
            // (CancelClose + Visible(false)) was unrecoverable: no tray icon, and the
            // unhide path silently did nothing on some setups.
            self.player.stop();
            self.player.browsing("idle");
            state::save_prefs(&self.prefs);
            platform::resume_mpd_discord_rpc();
            std::process::exit(0);
        }
        if platform::consume_show_request() {
            ctx.send_viewport_cmd(egui::ViewportCommand::Visible(true));
            ctx.send_viewport_cmd(egui::ViewportCommand::Focus);
        }

        if !ctx.wants_keyboard_input() && self.player.now_playing.lock().unwrap().is_some() {
            let status = self.player.status.lock().unwrap().clone();
            ctx.input(|i| {
                if i.key_pressed(egui::Key::Space) {
                    self.player.toggle_pause();
                }
                if let Some(status) = &status {
                    if i.key_pressed(egui::Key::ArrowRight) {
                        self.player.seek((status.time + 10.0).max(0.0));
                    }
                    if i.key_pressed(egui::Key::ArrowLeft) {
                        self.player.seek((status.time - 10.0).max(0.0));
                    }
                }
            });
        }
        if !ctx.wants_keyboard_input() && ctx.input(|i| i.key_pressed(egui::Key::Slash) || (i.modifiers.command && i.key_pressed(egui::Key::F))) {
            self.tab = Tab::Search;
        }

        egui::TopBottomPanel::top("top").show(ctx, |ui| {
            ui.horizontal(|ui| {
                ui.heading("aniani");
                if !self.prefs.compact_mode {
                    ui.separator();
                    ui.selectable_value(&mut self.tab, Tab::Discover, "discover");
                    ui.selectable_value(&mut self.tab, Tab::Search, "search");
                    ui.selectable_value(&mut self.tab, Tab::Downloads, "downloads");
                    ui.selectable_value(&mut self.tab, Tab::History, "history");
                    ui.selectable_value(&mut self.tab, Tab::Library, "library");
                    ui.selectable_value(&mut self.tab, Tab::Reader, "books");
                    ui.selectable_value(&mut self.tab, Tab::Shows, "shows");
                    ui.selectable_value(&mut self.tab, Tab::Settings, "settings");
                }
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    if ui
                        .button(if self.prefs.compact_mode { "⛶" } else { "▭" })
                        .on_hover_text(if self.prefs.compact_mode { "expand" } else { "compact mode" })
                        .clicked()
                    {
                        self.prefs.compact_mode = !self.prefs.compact_mode;
                        let size = if self.prefs.compact_mode { egui::vec2(420.0, 680.0) } else { egui::vec2(1280.0, 800.0) };
                        ctx.send_viewport_cmd(egui::ViewportCommand::InnerSize(size));
                        if self.prefs.compact_mode {
                            self.tab = Tab::Search;
                        }
                    }
                    if let Some(latest) = self.update_available.lock().unwrap().clone() {
                        if ui.button(format!("v{latest} available")).on_hover_text("open the releases page").clicked() {
                            let _ = open::that("https://github.com/arshnah/aniani/releases/latest");
                        }
                    }
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
                ui.horizontal(|ui| {
                    ui.label("browse:");
                    let sorts = [("TRENDING_DESC", "trending"), ("POPULARITY_DESC", "popular"), ("SCORE_DESC", "top rated"), ("START_DATE_DESC", "newest")];
                    let mut sort_changed = false;
                    egui::ComboBox::from_id_salt("discover_sort")
                        .selected_text(sorts.iter().find(|(k, _)| *k == self.discover_sort).map(|(_, l)| *l).unwrap_or("trending"))
                        .show_ui(ui, |ui| {
                            for (key, label) in sorts {
                                if ui.selectable_label(self.discover_sort == key, label).clicked() {
                                    self.discover_sort = key.to_string();
                                    sort_changed = true;
                                }
                            }
                        });
                    let genres = ["Action", "Adventure", "Comedy", "Drama", "Fantasy", "Horror", "Mystery", "Romance", "Sci-Fi", "Slice of Life", "Sports", "Supernatural", "Thriller"];
                    egui::ComboBox::from_id_salt("discover_genre")
                        .selected_text(self.discover_genre.as_deref().unwrap_or("any genre"))
                        .show_ui(ui, |ui| {
                            if ui.selectable_label(self.discover_genre.is_none(), "any genre").clicked() {
                                self.discover_genre = None;
                                sort_changed = true;
                            }
                            for genre in genres {
                                if ui.selectable_label(self.discover_genre.as_deref() == Some(genre), genre).clicked() {
                                    self.discover_genre = Some(genre.to_string());
                                    sort_changed = true;
                                }
                            }
                        });
                    if sort_changed || (self.discover_sort != "TRENDING_DESC" || self.discover_genre.is_some()) && self.filtered_results.lock().unwrap().is_empty() {
                        self.refresh_filtered();
                    }
                });

                let custom_browse = self.discover_sort != "TRENDING_DESC" || self.discover_genre.is_some();
                if custom_browse {
                    let filtered = self.filtered_results.lock().unwrap().clone();
                    ui.heading("browse results");
                    if let Some(a) = self.anime_grid(ui, "filtered", &filtered) {
                        self.open_anime_detail(a);
                    }
                    ui.add_space(20.0);
                    return;
                }

                let mut clicked = None;
                let mut continue_watching_actions = GridActions::default();
                let continue_watching = self.continue_watching.lock().unwrap().clone();
                if !continue_watching.is_empty() {
                    ui.heading("continue watching");
                    continue_watching_actions = self.anime_grid_with_actions(ui, "continue_watching", &continue_watching, true);
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
                if let Some(anime) = continue_watching_actions.clicked {
                    self.resume_continue_watching(&anime.title);
                }
                if let Some(anime) = continue_watching_actions.browse {
                    self.open_in_search(&anime.title);
                }
                if let Some(anime) = continue_watching_actions.remove {
                    state::remove_history(&anime.title);
                    self.continue_watching.lock().unwrap().retain(|a| a.title != anime.title);
                    self.refresh_continue_watching();
                }
                if let Some(anime) = clicked {
                    self.open_anime_detail(anime);
                }

                let anilist_list = self.anilist_list.lock().unwrap().clone();
                if !anilist_list.is_empty() {
                    ui.separator();
                    ui.heading("watching, from your AniList list");
                    let as_anime: Vec<sources::Anime> = anilist_list
                        .iter()
                        .map(|e| sources::Anime {
                            title: e.title.clone(),
                            episodes: e.episodes,
                            score: None,
                            status: Some(e.status.clone()),
                            genres: vec![],
                            description: match e.episodes {
                                Some(total) => format!("episode {} of {total} on AniList", e.progress),
                                None => format!("episode {} on AniList", e.progress),
                            },
                            cover: e.cover.clone(),
                        })
                        .collect();
                    if let Some(anime) = self.anime_grid(ui, "anilist_list", &as_anime) {
                        self.open_anime_detail(anime);
                    }
                }
            }
            Tab::Search => {
                ui.horizontal(|ui| {
                    let source_before = self.search_source;
                    ui.selectable_value(&mut self.search_source, SearchSource::AniDb, "anidb (stream)");
                    ui.selectable_value(&mut self.search_source, SearchSource::Yuma, "aniwatch (stream, search only)");
                    ui.selectable_value(&mut self.search_source, SearchSource::Nyaa, "nyaa (torrent)");
                    if self.search_source != source_before && !self.search_query.trim().is_empty() {
                        self.run_search();
                    }
                });
                ui.horizontal(|ui| {
                    let resp = ui.text_edit_singleline(&mut self.search_query);
                    if resp.changed() {
                        self.search_dirty_at = Some(std::time::Instant::now());
                    }
                    let enter_pressed = resp.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter));
                    if enter_pressed || ui.button("search").clicked() {
                        self.run_search();
                        self.search_dirty_at = None;
                    }
                });
                ui.separator();

                if *self.searching.lock().unwrap() {
                    ui.label(egui::RichText::new("searching…").color(theme::MUTED));
                }

                match self.search_source {
                    SearchSource::AniDb => {
                        let results = self.anidb_results.lock().unwrap().clone();
                        if results.is_empty() && !*self.searching.lock().unwrap() && !self.search_query.trim().is_empty() {
                            ui.label(egui::RichText::new("no results. check debug.log if this keeps happening.").color(theme::MUTED));
                        }
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
                    let selected_count = self.selected_episodes.len();
                    ui.horizontal(|ui| {
                        if ui.button("select all").clicked() {
                            self.selected_episodes = episodes.iter().map(|e| e.ep_no.clone()).collect();
                        }
                        if ui.button("select none").clicked() {
                            self.selected_episodes.clear();
                        }
                        if selected_count > 0 && ui.button(format!("download {selected_count} selected")).clicked() {
                            for ep in &episodes {
                                if self.selected_episodes.contains(&ep.ep_no) {
                                    to_download = Some((ep.ep_ref.clone(), ep.ep_no.clone()));
                                    if let Some((ep_ref, ep_no)) = to_download.take() {
                                        self.download_episode(&ep_ref, &ep_no);
                                    }
                                }
                            }
                            self.selected_episodes.clear();
                        }
                    });
                    for ep in &episodes {
                        ui.horizontal(|ui| {
                            let mut checked = self.selected_episodes.contains(&ep.ep_no);
                            if ui.checkbox(&mut checked, "").changed() {
                                if checked {
                                    self.selected_episodes.insert(ep.ep_no.clone());
                                } else {
                                    self.selected_episodes.remove(&ep.ep_no);
                                }
                            }
                            if ui.button(format!("episode {}", ep.ep_no)).clicked() {
                                to_play = Some((ep.ep_ref.clone(), ep.ep_no.clone()));
                            }
                            if download::is_downloaded(&sel_title, &ep.ep_no) {
                                ui.add(egui::ProgressBar::new(1.0).desired_width(160.0).text("downloaded"));
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
                if ui.button("import existing files…").clicked() {
                    let pending = self.pending_import.clone();
                    std::thread::spawn(move || {
                        let paths = rfd::FileDialog::new().add_filter("video", &["mp4", "mkv", "avi", "webm", "mov", "m4v", "ts"]).pick_files();
                        if let Some(paths) = paths {
                            *pending.lock().unwrap() = paths;
                        }
                    });
                }
                let picked = self.pending_import.lock().unwrap().clone();
                if !picked.is_empty() {
                    if self.import_labels.len() != picked.len() {
                        self.import_labels = picked
                            .iter()
                            .map(|p| download::guess_episode_label(&p.file_name().unwrap_or_default().to_string_lossy()))
                            .collect();
                    }
                    ui.group(|ui| {
                        ui.label(format!("importing {} file(s) -- episode numbers guessed from filenames, edit any that are wrong", picked.len()));
                        ui.horizontal(|ui| {
                            ui.label("show title");
                            ui.text_edit_singleline(&mut self.import_title);
                        });
                        egui::ScrollArea::vertical().max_height(240.0).show(ui, |ui| {
                            for (path, label) in picked.iter().zip(self.import_labels.iter_mut()) {
                                ui.horizontal(|ui| {
                                    ui.label(egui::RichText::new(path.file_name().unwrap_or_default().to_string_lossy()).weak().size(11.0));
                                    ui.label("ep");
                                    ui.add(egui::TextEdit::singleline(label).desired_width(60.0));
                                });
                            }
                        });
                        ui.horizontal(|ui| {
                            let ready = !self.import_title.trim().is_empty() && self.import_labels.iter().all(|l| !l.trim().is_empty());
                            if ui.add_enabled(ready, egui::Button::new(format!("add all {} to library", picked.len()))).clicked() {
                                let mut ok = true;
                                for (path, label) in picked.iter().zip(self.import_labels.iter()) {
                                    ok &= download::import_file(self.import_title.trim(), label.trim(), path).is_ok();
                                }
                                if ok {
                                    self.refresh_downloaded_library();
                                }
                                *self.pending_import.lock().unwrap() = vec![];
                                self.import_title.clear();
                                self.import_labels.clear();
                            }
                            if ui.button("cancel").clicked() {
                                *self.pending_import.lock().unwrap() = vec![];
                                self.import_title.clear();
                                self.import_labels.clear();
                            }
                        });
                    });
                }
                ui.separator();

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
                            let indeterminate = *job.indeterminate.lock().unwrap();
                            ui.label(format!("{} · episode {}", job.anime_title, job.ep_no));
                            let bar = egui::ProgressBar::new(progress as f32).desired_width(160.0).animate(indeterminate);
                            let bar = if indeterminate {
                                bar.text("locating stream…")
                            } else {
                                bar.text(format!("{:.0}%", progress * 100.0))
                            };
                            ui.add(bar);
                            if let Some(eta) = job.eta() {
                                ui.label(egui::RichText::new(format!("eta {}", fmt_eta(eta))).color(theme::MUTED));
                            }
                            if ui.button("cancel").clicked() {
                                job.cancel();
                            }
                        });
                    }
                    ui.separator();
                }

                let failed: Vec<Arc<download::DownloadJob>> = self
                    .downloads
                    .iter()
                    .filter(|j| matches!(*j.status.lock().unwrap(), download::JobStatus::Failed | download::JobStatus::Cancelled))
                    .cloned()
                    .collect();
                if !failed.is_empty() {
                    ui.heading("failed");
                    let mut retry: Option<Arc<download::DownloadJob>> = None;
                    let mut dismiss: Option<Arc<download::DownloadJob>> = None;
                    for job in &failed {
                        let cancelled = *job.status.lock().unwrap() == download::JobStatus::Cancelled;
                        ui.horizontal(|ui| {
                            let label = if cancelled {
                                format!("{} · episode {} (cancelled)", job.anime_title, job.ep_no)
                            } else {
                                format!("{} · episode {}", job.anime_title, job.ep_no)
                            };
                            ui.label(egui::RichText::new(label).color(theme::MUTED));
                            if ui.button("retry").clicked() {
                                retry = Some(job.clone());
                            }
                            if ui.button("dismiss").clicked() {
                                dismiss = Some(job.clone());
                            }
                        });
                    }
                    if let Some(job) = retry {
                        self.retry_download(&job);
                    }
                    if let Some(job) = dismiss {
                        self.downloads.retain(|j| !Arc::ptr_eq(j, &job));
                    }
                    ui.separator();
                }

                ui.heading("downloaded");
                let library = self.downloaded_library.lock().unwrap().clone();
                if library.is_empty() {
                    ui.label(egui::RichText::new("nothing downloaded yet").color(theme::MUTED));
                }
                let mut rename_committed = false;
                for show in &library {
                    ui.collapsing(&show.title, |ui| {
                        let editing_show = self.editing_show.as_deref() == Some(show.title.as_str());
                        ui.horizontal(|ui| {
                            if editing_show {
                                ui.add(egui::TextEdit::singleline(&mut self.edit_show_input).desired_width(200.0));
                                if ui.button("save").clicked() {
                                    if download::rename_show(&show.title, self.edit_show_input.trim()).is_ok() {
                                        rename_committed = true;
                                    }
                                    self.editing_show = None;
                                }
                                if ui.button("cancel").clicked() {
                                    self.editing_show = None;
                                }
                            } else if ui.small_button("rename show").clicked() {
                                self.editing_show = Some(show.title.clone());
                                self.edit_show_input = show.title.clone();
                            }
                        });
                        ui.separator();
                        for ep in &show.episodes {
                            let editing = self.editing_episode.as_ref() == Some(&(show.title.clone(), ep.clone()));
                            ui.horizontal(|ui| {
                                if editing {
                                    ui.add(egui::TextEdit::singleline(&mut self.edit_label_input).desired_width(120.0));
                                    if ui.button("save").clicked() {
                                        if download::rename_episode(&show.title, ep, self.edit_label_input.trim()).is_ok() {
                                            rename_committed = true;
                                        }
                                        self.editing_episode = None;
                                    }
                                    if ui.button("cancel").clicked() {
                                        self.editing_episode = None;
                                    }
                                    return;
                                }
                                let label = if ep.chars().all(|c| c.is_ascii_digit() || c == '.') {
                                    format!("episode {ep}")
                                } else {
                                    ep.clone()
                                };
                                ui.label(label);
                                if ui.button("play").clicked() {
                                    if let Some(path) = download::find_episode_file(&show.title, ep) {
                                        self.player.play(&format!("file://{}", path.display()), &show.title, ep, None, None, "downloaded", &show.title);
                                        self.fetch_discord_cover(&show.title);
                                    }
                                }
                                if ui.button("edit").clicked() {
                                    self.editing_episode = Some((show.title.clone(), ep.clone()));
                                    self.edit_label_input = ep.clone();
                                }
                                if ui.button("delete").clicked() {
                                    download::delete_episode(&show.title, ep);
                                    self.refresh_downloaded_library();
                                }
                            });
                        }
                    });
                }
                if rename_committed {
                    self.refresh_downloaded_library();
                }
            }
            Tab::History => {
                self.watch_activity_heatmap(ui);
                ui.separator();
                let history = state::read_history();
                if history.is_empty() {
                    ui.label(egui::RichText::new("nothing watched yet").color(theme::MUTED));
                }
                for entry in history.iter().rev() {
                    ui.horizontal(|ui| {
                        ui.label(format!("{} · episode {} · {}", entry.anime_title, entry.ep_no, entry.source));
                        if ui.button("open in search").clicked() {
                            self.open_in_search(&entry.anime_title);
                        }
                    });
                }
            }
            Tab::Library => {
                let library = self.library.lock().unwrap().clone();
                if library.is_empty() {
                    ui.label(egui::RichText::new("nothing in your library yet -- add one from its detail popup").color(theme::MUTED));
                }
                let mut categories: Vec<String> = library.iter().map(|e| e.category.clone()).collect();
                categories.sort();
                categories.dedup();
                let mut remove_clicked: Option<String> = None;
                for category in &categories {
                    let heading = if category.is_empty() { "uncategorized" } else { category.as_str() };
                    ui.heading(heading);
                    for entry in library.iter().filter(|e| &e.category == category) {
                        ui.horizontal(|ui| {
                            ui.label(&entry.title);
                            if ui.button("open in search").clicked() {
                                self.open_in_search(&entry.title);
                            }
                            if ui.button("remove").clicked() {
                                remove_clicked = Some(entry.title.clone());
                            }
                        });
                    }
                    ui.add_space(8.0);
                }
                if let Some(title) = remove_clicked {
                    state::remove_from_library(&title);
                    self.refresh_library();
                }
            }
            Tab::Reader => {
                self.reader.ui(ui);
            }
            Tab::Shows => {
                self.shows.ui(ui);
            }
            Tab::Settings => {
                ui.heading("player");
                ui.horizontal(|ui| {
                    if ui.selectable_label(self.prefs.player == "mpv", "mpv").clicked() {
                        self.prefs.player = "mpv".into();
                        self.player.set_backend("mpv");
                    }
                    if ui.selectable_label(self.prefs.player == "vlc", "vlc").clicked() {
                        self.prefs.player = "vlc".into();
                        self.player.set_backend("vlc");
                    }
                });

                ui.separator();
                ui.heading("anilist");
                let username = self.anilist_username.lock().unwrap().clone();
                if let Some(name) = username {
                    ui.label(format!("connected as {name}"));
                    ui.checkbox(&mut self.prefs.anilist_sync, "sync watched episodes");
                    if ui.button("disconnect").clicked() {
                        tracker::clear_token();
                        *self.anilist_username.lock().unwrap() = None;
                        self.anilist_list.lock().unwrap().clear();
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
                            self.refresh_anilist_list();
                        }
                    });
                }

                ui.separator();
                ui.heading("myanimelist");
                let mal_username = self.mal_username.lock().unwrap().clone();
                if let Some(name) = mal_username {
                    ui.label(format!("connected as {name}"));
                    ui.checkbox(&mut self.prefs.mal_sync, "sync watched episodes");
                    if ui.button("disconnect").clicked() {
                        mal::clear_token();
                        *self.mal_username.lock().unwrap() = None;
                    }
                } else {
                    ui.label("paste your MAL API client id and secret (from myanimelist.net/apiconfig), open the authorize link, sign in, then paste the \"code\" value from the resulting URL back:");
                    ui.horizontal(|ui| {
                        ui.label("client id");
                        ui.text_edit_singleline(&mut self.prefs.mal_client_id);
                    });
                    ui.horizontal(|ui| {
                        ui.label("client secret");
                        ui.text_edit_singleline(&mut self.prefs.mal_client_secret);
                    });
                    ui.horizontal(|ui| {
                        if ui.button("open authorize page").clicked() && !self.prefs.mal_client_id.is_empty() {
                            self.mal_code_verifier = mal::gen_code_verifier();
                            let _ = open::that(mal::authorize_url(&self.prefs.mal_client_id, &self.mal_code_verifier));
                        }
                    });
                    ui.horizontal(|ui| {
                        ui.text_edit_singleline(&mut self.mal_code_input);
                        if ui.button("connect").clicked() && !self.mal_code_input.is_empty() && !self.mal_code_verifier.is_empty() {
                            let client = self.http.clone();
                            let client_id = self.prefs.mal_client_id.clone();
                            let client_secret = self.prefs.mal_client_secret.clone();
                            let code = self.mal_code_input.clone();
                            let code_verifier = self.mal_code_verifier.clone();
                            let out = self.mal_username.clone();
                            self.rt.spawn(async move {
                                if mal::exchange_code(&client, &client_id, &client_secret, &code, &code_verifier).await.is_some() {
                                    *out.lock().unwrap() = mal::whoami(&client, &client_id, &client_secret).await;
                                }
                            });
                            self.mal_code_input.clear();
                        }
                    });
                }

                ui.separator();
                ui.heading("about");
                ui.label(format!("aniani v{}", env!("CARGO_PKG_VERSION")));
                if ui.button("check for updates").clicked() {
                    self.check_for_update();
                }
            }
            });
        });

        if let Some(anime) = self.anime_detail.clone() {
            let mut open = true;
            let mut watch = false;
            let mut close_clicked = false;
            egui::Window::new(&anime.title)
                .open(&mut open)
                .collapsible(false)
                .resizable(false)
                .anchor(egui::Align2::CENTER_CENTER, egui::vec2(0.0, 0.0))
                .show(ctx, |ui| {
                    ui.set_max_width(420.0);
                    ui.horizontal(|ui| {
                        if let Some(cover) = &anime.cover {
                            ui.add(egui::Image::new(cover).fit_to_exact_size(egui::vec2(140.0, 200.0)).rounding(6.0));
                        }
                        ui.vertical(|ui| {
                            if let Some(score) = anime.score {
                                ui.label(format!("★ {score}% rated"));
                            }
                            if let Some(status) = &anime.status {
                                ui.label(format!("status: {status}"));
                            }
                            if let Some(eps) = anime.episodes {
                                ui.label(format!("{eps} episodes"));
                            }
                            if !anime.genres.is_empty() {
                                ui.label(anime.genres.join(", "));
                            }
                        });
                    });
                    ui.add_space(8.0);
                    if !anime.description.is_empty() {
                        ui.label(&anime.description);
                    }
                    ui.add_space(8.0);
                    let in_library = self.library.lock().unwrap().iter().any(|e| e.title == anime.title);
                    ui.horizontal(|ui| {
                        ui.label("category");
                        ui.text_edit_singleline(&mut self.library_category_input);
                        if ui.button(if in_library { "update" } else { "add to library" }).clicked() {
                            state::add_to_library(&anime.title, anime.cover.clone(), self.library_category_input.trim());
                            self.refresh_library();
                        }
                        if in_library && ui.button("remove from library").clicked() {
                            state::remove_from_library(&anime.title);
                            self.refresh_library();
                        }
                    });
                    ui.add_space(12.0);
                    ui.horizontal(|ui| {
                        if ui.button("watch").clicked() {
                            watch = true;
                        }
                        if ui.button("close").clicked() {
                            close_clicked = true;
                        }
                    });
                });
            if watch {
                self.open_in_search(&anime.title);
                self.anime_detail = None;
            } else if !open || close_clicked {
                self.anime_detail = None;
            }
        }

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
    std::panic::set_hook(Box::new(|info| {
        platform::debug_log(&format!("panic: {info}"));
    }));

    if !platform::acquire_single_instance_lock() {
        platform::request_show_running_instance();
        return Ok(());
    }
    platform::pause_mpd_discord_rpc();

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

#[cfg(test)]
mod tests {
    use super::*;

    fn results(titles: &[&str]) -> Vec<sources::SearchResult> {
        titles
            .iter()
            .enumerate()
            .map(|(i, t)| sources::SearchResult { id: i.to_string(), title: t.to_string() })
            .collect()
    }

    #[test]
    fn season_two_wants_season_two_not_the_unmarked_first_season() {
        let r = results(&[
            "Frieren: Beyond Journey's End",
            "Frieren: Beyond Journey's End Season 2",
            "Frieren: Beyond Journey's End Mini Anime",
        ]);
        let pick = App::best_result_match("Frieren: Beyond Journey's End Season 2", &r);
        assert_eq!(pick.map(|p| p.title.as_str()), Some("Frieren: Beyond Journey's End Season 2"));
    }

    #[test]
    fn markerless_want_prefers_the_unmarked_entry() {
        let r = results(&[
            "Frieren: Beyond Journey's End Season 2",
            "Frieren: Beyond Journey's End",
        ]);
        let pick = App::best_result_match("Frieren: Beyond Journey's End", &r);
        assert_eq!(pick.map(|p| p.title.as_str()), Some("Frieren: Beyond Journey's End"));
    }

    #[test]
    fn roman_numeral_naming_still_matches_a_season_want() {
        let r = results(&["Mushoku Tensei: Jobless Reincarnation", "Mushoku Tensei: Jobless Reincarnation II"]);
        let pick = App::best_result_match("Mushoku Tensei: Jobless Reincarnation Season 2", &r);
        assert_eq!(pick.map(|p| p.title.as_str()), Some("Mushoku Tensei: Jobless Reincarnation II"));
    }

    #[test]
    fn wrong_season_is_never_auto_picked() {
        let r = results(&["Frieren: Beyond Journey's End", "Frieren: Beyond Journey's End Mini Anime"]);
        assert!(App::best_result_match("Frieren: Beyond Journey's End Season 2", &r).is_none());
    }

    #[test]
    fn nothing_close_means_no_pick() {
        let r = results(&["Jujutsu Kaisen", "Chainsaw Man"]);
        assert!(App::best_result_match("Frieren: Beyond Journey's End", &r).is_none());
    }

    #[test]
    fn nth_season_wording_is_understood() {
        let r = results(&["Overlord", "Overlord 2nd Season"]);
        let pick = App::best_result_match("Overlord Season 2", &r);
        assert_eq!(pick.map(|p| p.title.as_str()), Some("Overlord 2nd Season"));
    }
}
