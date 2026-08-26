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
pub struct Prefs {
    pub source: String,
    pub player: String,
    pub volume: i32,
    pub speed: f64,
    pub anilist_client_id: String,
    pub anilist_sync: bool,
    pub compact_mode: bool,
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
        }
    }
}

pub fn load_prefs() -> Prefs {
    load(&state_dir().join("prefs.json")).unwrap_or_default()
}

pub fn save_prefs(prefs: &Prefs) {
    save(&state_dir().join("prefs.json"), prefs);
}
