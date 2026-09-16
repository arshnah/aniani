use serde::{Deserialize, Serialize};
use std::fs;
use std::path::PathBuf;

use crate::platform;

fn state_dir() -> PathBuf {
    platform::state_dir("aniani")
}

fn load<T: for<'de> Deserialize<'de>>(path: &PathBuf) -> Option<T> {
    let data = fs::read_to_string(path).ok()?;
    serde_json::from_str(&data).ok()
}

fn save<T: Serialize>(path: &PathBuf, value: &T) {
    let _ = fs::create_dir_all(state_dir());
    if let Ok(data) = serde_json::to_string(value) {
        let _ = fs::write(path, data);
    }
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct HistoryEntry {
    pub source: String,
    pub anime_id: String,
    pub anime_title: String,
    pub ep_no: String,
}

pub fn read_history() -> Vec<HistoryEntry> {
    load(&state_dir().join("history.json")).unwrap_or_default()
}

pub fn update_history(source: &str, anime_id: &str, anime_title: &str, ep_no: &str) {
    let mut entries = read_history();
    if let Some(e) = entries
        .iter_mut()
        .find(|e| e.source == source && e.anime_id == anime_id)
    {
        e.ep_no = ep_no.to_string();
    } else {
        entries.push(HistoryEntry {
            source: source.to_string(),
            anime_id: anime_id.to_string(),
            anime_title: anime_title.to_string(),
            ep_no: ep_no.to_string(),
        });
    }
    save(&state_dir().join("history.json"), &entries);
}

pub fn remove_history(anime_title: &str) {
    let mut entries = read_history();
    entries.retain(|e| e.anime_title != anime_title);
    save(&state_dir().join("history.json"), &entries);
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct LibraryEntry {
    pub title: String,
    pub cover: Option<String>,
    pub category: String,
}

pub fn read_library() -> Vec<LibraryEntry> {
    load(&state_dir().join("library.json")).unwrap_or_default()
}

pub fn add_to_library(title: &str, cover: Option<String>, category: &str) {
    let mut entries = read_library();
    if let Some(e) = entries.iter_mut().find(|e| e.title == title) {
        e.category = category.to_string();
    } else {
        entries.push(LibraryEntry { title: title.to_string(), cover, category: category.to_string() });
    }
    save(&state_dir().join("library.json"), &entries);
}

pub fn remove_from_library(title: &str) {
    let mut entries = read_library();
    entries.retain(|e| e.title != title);
    save(&state_dir().join("library.json"), &entries);
}

pub fn position_key(source: &str, anime_title: &str, ep_no: &str) -> String {
    format!("{source}::{anime_title}::{ep_no}")
}

pub fn load_positions() -> std::collections::HashMap<String, f64> {
    load(&state_dir().join("positions.json")).unwrap_or_default()
}

pub fn save_positions(positions: &std::collections::HashMap<String, f64>) {
    save(&state_dir().join("positions.json"), positions);
}

pub const RESUME_MIN_SECONDS: f64 = 15.0;
pub const RESUME_END_MARGIN: f64 = 30.0;

#[derive(Serialize, Deserialize, Clone, Debug)]
#[serde(default)]
pub struct ShowsPrefs {
    pub title: String,
    pub is_movie: bool,
    pub season: String,
    pub episode: String,
    pub cover: Option<String>,
    pub matched_name: Option<String>,
    pub matched_url: Option<String>,
    pub tmdb_id: Option<i64>,
    pub enabled: bool,
    pub vlc_host: String,
    pub vlc_port: String,
    pub vlc_password: String,
}

impl Default for ShowsPrefs {
    fn default() -> Self {
        ShowsPrefs {
            title: String::new(),
            is_movie: false,
            season: String::new(),
            episode: String::new(),
            cover: None,
            matched_name: None,
            matched_url: None,
            tmdb_id: None,
            enabled: false,
            vlc_host: "127.0.0.1".to_string(),
            vlc_port: "9091".to_string(),
            vlc_password: "68041633b7ab0bbf716011c7c7344889".to_string(),
        }
    }
}

pub fn load_shows_prefs() -> ShowsPrefs {
    load(&state_dir().join("shows_prefs.json")).unwrap_or_default()
}

pub fn save_shows_prefs(prefs: &ShowsPrefs) {
    save(&state_dir().join("shows_prefs.json"), prefs);
}

#[derive(Serialize, Deserialize, Clone, Debug)]
#[serde(default)]
pub struct Prefs {
    pub source: String,
    pub player: String,
    pub volume: i32,
    pub speed: f64,
    pub anilist_client_id: String,
    pub anilist_sync: bool,
    pub compact_mode: bool,
    pub mal_client_id: String,
    pub mal_client_secret: String,
    pub mal_sync: bool,
    pub tmdb_api_key: String,
}

impl Default for Prefs {
    fn default() -> Self {
        Prefs {
            source: "anidb".into(),
            player: "mpv".into(),
            volume: 100,
            speed: 1.0,
            anilist_client_id: String::new(),
            anilist_sync: false,
            compact_mode: false,
            mal_client_id: String::new(),
            mal_client_secret: String::new(),
            mal_sync: false,
            tmdb_api_key: String::new(),
        }
    }
}

pub fn load_prefs() -> Prefs {
    load(&state_dir().join("prefs.json")).unwrap_or_default()
}

pub fn save_prefs(prefs: &Prefs) {
    save(&state_dir().join("prefs.json"), prefs);
}

#[derive(Serialize, Deserialize, Clone, Debug)]
#[serde(default)]
pub struct ReaderPrefs {
    pub library_dir: String,
    pub right_to_left: bool,
    pub scroll_mode: bool,
    pub font_size: f32,
}

impl Default for ReaderPrefs {
    fn default() -> Self {
        ReaderPrefs { library_dir: String::new(), right_to_left: false, scroll_mode: false, font_size: 18.0 }
    }
}

pub fn load_reader_prefs() -> ReaderPrefs {
    load(&state_dir().join("reader_prefs.json")).unwrap_or_default()
}

pub fn save_reader_prefs(prefs: &ReaderPrefs) {
    save(&state_dir().join("reader_prefs.json"), prefs);
}

#[derive(Serialize, Deserialize, Clone, Copy, Debug, Default)]
pub struct ReaderPosition {
    pub chapter: usize,
    pub page: usize,
}

pub fn load_reader_positions() -> std::collections::HashMap<String, ReaderPosition> {
    load(&state_dir().join("reader_positions.json")).unwrap_or_default()
}

pub fn save_reader_position(book_path: &str, pos: ReaderPosition) {
    let mut positions = load_reader_positions();
    positions.insert(book_path.to_string(), pos);
    save(&state_dir().join("reader_positions.json"), &positions);
}

pub fn today_bucket() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64 / 86400).unwrap_or(0)
}

pub fn log_watch_activity() {
    let mut log: std::collections::HashMap<i64, u32> = load(&state_dir().join("watch_log.json")).unwrap_or_default();
    *log.entry(today_bucket()).or_insert(0) += 1;
    save(&state_dir().join("watch_log.json"), &log);
}

pub fn read_watch_activity() -> std::collections::HashMap<i64, u32> {
    load(&state_dir().join("watch_log.json")).unwrap_or_default()
}
