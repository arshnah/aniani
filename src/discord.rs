use discord_rich_presence::activity::{Activity, Assets, Button, Timestamps};
use discord_rich_presence::{DiscordIpc, DiscordIpcClient};
use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

const CLIENT_ID: &str = "1539979776322965555";
const BROWSING_ICON: &str = "https://cdn.myanimelist.net/img/sp/icon/apple-touch-icon-256.png";

#[derive(Clone)]
pub struct MediaInfo {
    pub image: Option<String>,
    pub url: Option<String>,
}

pub struct DiscordPresence {
    client: Option<DiscordIpcClient>,
    cache: HashMap<String, MediaInfo>,
}

impl DiscordPresence {
    pub fn new() -> Self {
        DiscordPresence { client: None, cache: HashMap::new() }
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
            return;
        }
        let assets = Assets::new().large_image(BROWSING_ICON).large_text("aniani");
        let activity = Activity::new().details("Browsing aniani").state(detail).assets(assets);
        if let Some(c) = self.client.as_mut() {
            if let Err(e) = c.set_activity(activity) {
                crate::platform::debug_log(&format!("discord: browsing() set_activity failed: {e:?}"));
                self.client = None;
            }
        }
    }

    pub fn watching(&mut self, show: &str, ep_no: &str, pos_seconds: f64, duration_seconds: f64, paused: bool) {
        if !self.ensure_connected() {
            return;
        }
        let info = self.cache.get(show);
        let state = if paused { format!("Paused · Episode {ep_no}") } else { format!("Watching · Episode {ep_no}") };

        let mut assets = Assets::new();
        if let Some(i) = info.and_then(|i| i.image.as_deref()) {
            assets = assets.large_image(i).large_text(show);
        }

        let mut activity = Activity::new().details(&show[..show.len().min(128)]).state(&state).assets(assets);

        if !paused {
            let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs() as i64;
            let start = now - pos_seconds as i64;
            let mut ts = Timestamps::new().start(start);
            if duration_seconds > 0.0 {
                ts = ts.end(start + duration_seconds as i64);
            }
            activity = activity.timestamps(ts);
        }

        let buttons_holder;
        if let Some(url) = info.and_then(|i| i.url.as_deref()) {
            buttons_holder = vec![Button::new("View on AniList", url)];
            activity = activity.buttons(buttons_holder);
        }

        if let Some(c) = self.client.as_mut() {
            if let Err(e) = c.set_activity(activity) {
                crate::platform::debug_log(&format!("discord: watching() set_activity failed: {e:?}"));
                self.client = None;
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
