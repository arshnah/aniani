use discord_rich_presence::activity::{Activity, ActivityType, Assets, Button, Timestamps};
use discord_rich_presence::{DiscordIpc, DiscordIpcClient};
use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

const CLIENT_ID: &str = "1539979776322965555";
const BROWSING_ICON: &str = "https://cdn.myanimelist.net/img/sp/icon/apple-touch-icon-256.png";
const REPO_URL: &str = "https://ani.arshnah.in";

#[derive(Clone)]
pub struct MediaInfo {
    pub image: Option<String>,
    pub url: Option<String>,
    pub episode_titles: HashMap<String, String>,
}

pub struct DiscordPresence {
    client: Option<DiscordIpcClient>,
    cache: HashMap<String, MediaInfo>,
    last_watching_log: Option<(String, String, bool)>,
}

impl DiscordPresence {
    pub fn new() -> Self {
        DiscordPresence { client: None, cache: HashMap::new(), last_watching_log: None }
    }

    fn ensure_connected(&mut self) -> bool {
        if self.client.is_some() {
            return true;
        }
        match DiscordIpcClient::new(CLIENT_ID) {
            Ok(mut c) => match c.connect() {
                Ok(_) => {
                    crate::platform::debug_log("discord: connected");
                    self.client = Some(c);
                    // Discord's IPC handshake isn't necessarily done the instant connect()
                    // returns -- an activity pushed immediately after can silently get
                    // dropped. A short pause before the first set_activity call avoids that.
                    std::thread::sleep(std::time::Duration::from_millis(500));
                    true
                }
                Err(e) => {
                    crate::platform::debug_log(&format!("discord: connect failed: {e:?}"));
                    false
                }
            },
            Err(e) => {
                crate::platform::debug_log(&format!("discord: client init failed: {e:?}"));
                false
            }
        }
    }

    pub fn cache_media(&mut self, title: &str, info: MediaInfo) {
        self.cache.insert(title.to_string(), info);
    }

    pub fn browsing(&mut self, detail: &str) {
        if !self.ensure_connected() {
            crate::platform::debug_log(&format!("discord: browsing({detail}) skipped -- not connected"));
            return;
        }
        let assets = Assets::new().large_image(BROWSING_ICON).large_text("aniani");
        let activity = Activity::new().details("Browsing aniani").state(detail).assets(assets);
        if let Some(c) = self.client.as_mut() {
            match c.set_activity(activity) {
                Ok(_) => crate::platform::debug_log(&format!("discord: browsing({detail}) set_activity ok")),
                Err(e) => {
                    crate::platform::debug_log(&format!("discord: browsing({detail}) set_activity failed: {e:?}"));
                    self.client = None;
                }
            }
        }
    }

    pub fn reading(&mut self, detail: &str, cover: Option<&str>) {
        if !self.ensure_connected() {
            crate::platform::debug_log(&format!("discord: reading({detail}) skipped -- not connected"));
            return;
        }
        let mut assets = Assets::new();
        if let Some(cover) = cover {
            assets = assets.large_image(cover).large_text("aniani");
        } else {
            assets = assets.large_image(BROWSING_ICON).large_text("aniani");
        }
        let activity = Activity::new().details(&detail[..detail.len().min(128)]).assets(assets);
        if let Some(c) = self.client.as_mut() {
            match c.set_activity(activity) {
                Ok(_) => crate::platform::debug_log(&format!("discord: reading({detail}) set_activity ok, cover={}", cover.is_some())),
                Err(e) => {
                    crate::platform::debug_log(&format!("discord: reading({detail}) set_activity failed: {e:?}"));
                    self.client = None;
                }
            }
        }
    }

    pub fn watching(&mut self, show: &str, ep_no: &str, pos_seconds: f64, duration_seconds: f64, paused: bool) {
        if !self.ensure_connected() {
            crate::platform::debug_log(&format!("discord: watching({show}, ep {ep_no}) skipped -- not connected"));
            return;
        }
        let state_key = (show.to_string(), ep_no.to_string(), paused);
        let state_changed = self.last_watching_log.as_ref() != Some(&state_key);
        let info = self.cache.get(show);
        // AniList exposes per-episode subtitles (e.g. "Emperor Dragon") for shows a
        // streaming partner has published episode titles for; fall back to the plain
        // episode number when it doesn't have one.
        let episode_label = info
            .and_then(|i| i.episode_titles.get(ep_no))
            .map(|title| title.to_string())
            .unwrap_or_else(|| format!("Episode {ep_no}"));
        let state = if paused { format!("Paused · {episode_label}") } else { episode_label };

        let mut assets = Assets::new();
        if let Some(i) = info.and_then(|i| i.image.as_deref()) {
            assets = assets.large_image(i).large_text(show);
        }

        let mut activity = Activity::new()
            .activity_type(ActivityType::Watching)
            .details(&show[..show.len().min(128)])
            .state(&state)
            .assets(assets);

        if !paused {
            let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs() as i64;
            let start = now - pos_seconds as i64;
            let mut ts = Timestamps::new().start(start);
            if duration_seconds > 0.0 {
                ts = ts.end(start + duration_seconds as i64);
            }
            activity = activity.timestamps(ts);
        }

        let mut buttons_holder = Vec::new();
        if let Some(url) = info.and_then(|i| i.url.as_deref()) {
            buttons_holder.push(Button::new("Watch on AniList", url));
        }
        buttons_holder.push(Button::new("aniani", REPO_URL));
        activity = activity.buttons(buttons_holder);

        if let Some(c) = self.client.as_mut() {
            match c.set_activity(activity) {
                Ok(_) => {
                    if state_changed {
                        crate::platform::debug_log(&format!("discord: watching({show}, ep {ep_no}, paused={paused}) set_activity ok"));
                        self.last_watching_log = Some(state_key);
                    }
                }
                Err(e) => {
                    crate::platform::debug_log(&format!("discord: watching({show}, ep {ep_no}) set_activity failed: {e:?}"));
                    self.client = None;
                }
            }
        }
    }

    pub fn watching_show(&mut self, title: &str, detail: &str, cover: Option<&str>, url: Option<&str>, pos_seconds: f64, duration_seconds: f64, paused: bool, live: bool) {
        if !self.ensure_connected() {
            crate::platform::debug_log(&format!("discord: watching_show({title}) skipped -- not connected"));
            return;
        }
        let mut assets = Assets::new();
        if let Some(cover) = cover {
            assets = assets.large_image(cover).large_text(title);
        } else {
            assets = assets.large_image(BROWSING_ICON).large_text(title);
        }
        let state = if detail.is_empty() {
            if paused { "TV · Paused".to_string() } else { "TV · Watching".to_string() }
        } else if paused {
            format!("TV · Paused · {detail}")
        } else {
            format!("TV · Watching · {detail}")
        };
        let mut buttons_holder = Vec::new();
        if let Some(url) = url {
            buttons_holder.push(Button::new("Watch", url));
        }
        buttons_holder.push(Button::new("aniani", REPO_URL));

        let mut activity = Activity::new()
            .activity_type(ActivityType::Watching)
            .details(&title[..title.len().min(128)])
            .state(&state)
            .assets(assets)
            .buttons(buttons_holder);
        if live && !paused {
            let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs() as i64;
            let start = now - pos_seconds as i64;
            let mut ts = Timestamps::new().start(start);
            if duration_seconds > 0.0 {
                ts = ts.end(start + duration_seconds as i64);
            }
            activity = activity.timestamps(ts);
        }
        if let Some(c) = self.client.as_mut() {
            match c.set_activity(activity) {
                Ok(_) => crate::platform::debug_log(&format!("discord: watching_show({title}, {detail}, paused={paused}, live={live}) set_activity ok")),
                Err(e) => {
                    crate::platform::debug_log(&format!("discord: watching_show({title}) set_activity failed: {e:?}"));
                    self.client = None;
                }
            }
        }
    }

    pub fn disconnect(&mut self) {
        if let Some(mut c) = self.client.take() {
            let _ = c.clear_activity();
            let _ = c.close();
        }
    }
}

impl Drop for DiscordPresence {
    fn drop(&mut self) {
        self.disconnect();
    }
}
