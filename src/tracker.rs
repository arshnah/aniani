use serde::Deserialize;
use serde_json::json;
use std::fs;
use std::path::PathBuf;

use crate::platform;

fn token_path() -> PathBuf {
    platform::state_dir("aniani").join("anilist_token.json")
}

pub fn authorize_url(client_id: &str) -> String {
    format!("https://anilist.co/api/v2/oauth/authorize?client_id={client_id}&response_type=token")
}

pub fn load_token() -> Option<String> {
    let data = fs::read_to_string(token_path()).ok()?;
    let v: serde_json::Value = serde_json::from_str(&data).ok()?;
    v.get("token")?.as_str().map(|s| s.to_string())
}

pub fn save_token(token: &str) {
    let _ = fs::create_dir_all(platform::state_dir("aniani"));
    let _ = fs::write(token_path(), json!({"token": token}).to_string());
}

pub fn clear_token() {
    let _ = fs::remove_file(token_path());
}

const API_URL: &str = "https://graphql.anilist.co";
const WHOAMI_QUERY: &str = "query { Viewer { id name } }";
const SEARCH_QUERY: &str = r#"
query ($search: String) {
  Media(search: $search, type: ANIME) {
    id
    episodes
    title { romaji english }
  }
}
"#;
const SAVE_MUTATION: &str = r#"
mutation ($mediaId: Int, $progress: Int, $status: MediaListStatus) {
  SaveMediaListEntry(mediaId: $mediaId, progress: $progress, status: $status) { id }
}
"#;

#[derive(Deserialize)]
struct Envelope {
    data: Option<serde_json::Value>,
}

async fn query(client: &reqwest::Client, q: &str, variables: serde_json::Value) -> Option<serde_json::Value> {
    let token = load_token()?;
    let resp = client
        .post(API_URL)
        .bearer_auth(token)
        .json(&json!({"query": q, "variables": variables}))
        .send()
        .await
        .ok()?;
    if resp.status().as_u16() == 401 {
        return None;
    }
    let env: Envelope = resp.json().await.ok()?;
    env.data
}

pub async fn whoami(client: &reqwest::Client) -> Option<String> {
    let data = query(client, WHOAMI_QUERY, json!({})).await?;
    data.get("Viewer")?.get("name")?.as_str().map(|s| s.to_string())
}

pub struct MediaRef {
    pub id: i64,
    pub episodes: Option<i64>,
}

pub async fn find_media(client: &reqwest::Client, title: &str) -> Option<MediaRef> {
    let data = query(client, SEARCH_QUERY, json!({"search": title})).await?;
    let media = data.get("Media")?;
    Some(MediaRef {
        id: media.get("id")?.as_i64()?,
        episodes: media.get("episodes").and_then(|v| v.as_i64()),
    })
}

pub async fn update_progress(client: &reqwest::Client, anime_title: &str, ep_no: &str) {
    if load_token().is_none() {
        return;
    }
    let Ok(progress) = ep_no.parse::<f64>() else { return };
    let progress = progress as i64;
    let Some(media) = find_media(client, anime_title).await else { return };
    let status = if media.episodes.map(|e| progress >= e).unwrap_or(false) { "COMPLETED" } else { "CURRENT" };
    let _ = query(
        client,
        SAVE_MUTATION,
        json!({"mediaId": media.id, "progress": progress, "status": status}),
    )
    .await;
}
