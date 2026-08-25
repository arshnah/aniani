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
const MY_LIST_QUERY: &str = r#"
query ($userId: Int, $statuses: [MediaListStatus]) {
  MediaListCollection(userId: $userId, type: ANIME, status_in: $statuses) {
    lists {
      entries {
        progress
        status
        media {
          id
          episodes
          title { romaji english }
          coverImage { large }
        }
      }
    }
  }
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

pub async fn viewer_id(client: &reqwest::Client) -> Option<i64> {
    let data = query(client, WHOAMI_QUERY, json!({})).await?;
    data.get("Viewer")?.get("id")?.as_i64()
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

#[derive(Clone)]
pub struct ListEntry {
    pub title: String,
    pub progress: i64,
    pub episodes: Option<i64>,
    pub status: String,
    pub cover: Option<String>,
}

pub async fn my_list(client: &reqwest::Client) -> Vec<ListEntry> {
    let Some(user_id) = viewer_id(client).await else { return vec![] };
    let Some(data) = query(
        client,
        MY_LIST_QUERY,
        json!({"userId": user_id, "statuses": ["CURRENT", "REPEATING"]}),
    )
    .await
    else {
        return vec![];
    };

    let mut out = vec![];
    let lists = data
        .get("MediaListCollection")
        .and_then(|c| c.get("lists"))
        .and_then(|l| l.as_array())
        .cloned()
        .unwrap_or_default();
    for list in lists {
        let entries = list.get("entries").and_then(|e| e.as_array()).cloned().unwrap_or_default();
        for entry in entries {
            let media = entry.get("media");
            let title = media
                .and_then(|m| m.get("title"))
                .and_then(|t| t.get("english").or_else(|| t.get("romaji")))
                .and_then(|v| v.as_str())
                .map(|s| s.to_string());
            let Some(title) = title else { continue };
            out.push(ListEntry {
                title,
                progress: entry.get("progress").and_then(|v| v.as_i64()).unwrap_or(0),
                episodes: media.and_then(|m| m.get("episodes")).and_then(|v| v.as_i64()),
                status: entry.get("status").and_then(|v| v.as_str()).unwrap_or("CURRENT").to_string(),
                cover: media.and_then(|m| m.get("coverImage")).and_then(|c| c.get("large")).and_then(|v| v.as_str()).map(|s| s.to_string()),
            });
        }
    }
    out
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
