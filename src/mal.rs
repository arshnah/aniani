use serde_json::json;
use std::fs;
use std::path::PathBuf;

use crate::platform;

fn token_path() -> PathBuf {
    platform::state_dir("aniani").join("mal_token.json")
}

pub fn gen_code_verifier() -> String {
    use rand::Rng;
    const CHARS: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~";
    let mut rng = rand::thread_rng();
    (0..96).map(|_| CHARS[rng.gen_range(0..CHARS.len())] as char).collect()
}

pub fn authorize_url(client_id: &str, code_verifier: &str) -> String {
    format!(
        "https://myanimelist.net/v1/oauth2/authorize?response_type=code&client_id={client_id}&code_challenge={code_verifier}&code_challenge_method=plain"
    )
}

struct Tokens {
    access: String,
    refresh: String,
}

fn load_tokens() -> Option<Tokens> {
    let data = fs::read_to_string(token_path()).ok()?;
    let v: serde_json::Value = serde_json::from_str(&data).ok()?;
    Some(Tokens {
        access: v.get("access_token")?.as_str()?.to_string(),
        refresh: v.get("refresh_token")?.as_str()?.to_string(),
    })
}

pub fn has_token() -> bool {
    load_tokens().is_some()
}

fn save_tokens(access: &str, refresh: &str) {
    let _ = fs::create_dir_all(platform::state_dir("aniani"));
    let _ = fs::write(token_path(), json!({"access_token": access, "refresh_token": refresh}).to_string());
}

pub fn clear_token() {
    let _ = fs::remove_file(token_path());
}

async fn token_request(client: &reqwest::Client, client_id: &str, client_secret: &str, form: &[(&str, &str)]) -> Option<()> {
    let mut params: Vec<(&str, &str)> = vec![("client_id", client_id), ("client_secret", client_secret)];
    params.extend_from_slice(form);
    let resp = client.post("https://myanimelist.net/v1/oauth2/token").form(&params).send().await.ok()?;
    let v: serde_json::Value = resp.json().await.ok()?;
    let access = v.get("access_token")?.as_str()?.to_string();
    let refresh = v.get("refresh_token")?.as_str()?.to_string();
    save_tokens(&access, &refresh);
    Some(())
}

pub async fn exchange_code(client: &reqwest::Client, client_id: &str, client_secret: &str, code: &str, code_verifier: &str) -> Option<()> {
    token_request(client, client_id, client_secret, &[("grant_type", "authorization_code"), ("code", code), ("code_verifier", code_verifier)]).await
}

async fn refresh(client: &reqwest::Client, client_id: &str, client_secret: &str) -> Option<()> {
    let refresh_token = load_tokens()?.refresh;
    token_request(client, client_id, client_secret, &[("grant_type", "refresh_token"), ("refresh_token", &refresh_token)]).await
}

async fn authed<T, F, Fut>(client: &reqwest::Client, client_id: &str, client_secret: &str, req: F) -> Option<T>
where
    F: Fn(String) -> Fut,
    Fut: std::future::Future<Output = Option<(u16, T)>>,
{
    let access = load_tokens()?.access;
    if let Some((status, v)) = req(access.clone()).await {
        if status != 401 {
            return Some(v);
        }
    }
    refresh(client, client_id, client_secret).await?;
    let access = load_tokens()?.access;
    req(access).await.and_then(|(status, v)| if status == 401 { None } else { Some(v) })
}

pub async fn whoami(client: &reqwest::Client, client_id: &str, client_secret: &str) -> Option<String> {
    authed(client, client_id, client_secret, |token| async move {
        let resp = client.get("https://api.myanimelist.net/v2/users/@me?fields=name").bearer_auth(token).send().await.ok()?;
        let status = resp.status().as_u16();
        let v: serde_json::Value = resp.json().await.ok()?;
        Some((status, v.get("name")?.as_str()?.to_string()))
    })
    .await
}

pub struct MediaRef {
    pub id: i64,
    pub episodes: Option<i64>,
}

pub async fn find_anime(client: &reqwest::Client, client_id: &str, client_secret: &str, title: &str) -> Option<MediaRef> {
    authed(client, client_id, client_secret, |token| async move {
        let resp = client
            .get("https://api.myanimelist.net/v2/anime")
            .bearer_auth(token)
            .query(&[("q", title), ("limit", "1"), ("fields", "num_episodes")])
            .send()
            .await
            .ok()?;
        let status = resp.status().as_u16();
        let v: serde_json::Value = resp.json().await.ok()?;
        let node = v.get("data")?.as_array()?.first()?.get("node")?;
        Some((
            status,
            MediaRef { id: node.get("id")?.as_i64()?, episodes: node.get("num_episodes").and_then(|e| e.as_i64()) },
        ))
    })
    .await
}

pub async fn update_progress(client: &reqwest::Client, client_id: &str, client_secret: &str, anime_title: &str, ep_no: &str) {
    if !has_token() {
        return;
    }
    let Ok(progress) = ep_no.parse::<f64>() else { return };
    let progress = progress as i64;
    let Some(media) = find_anime(client, client_id, client_secret, anime_title).await else { return };
    let status = if media.episodes.map(|e| e > 0 && progress >= e).unwrap_or(false) { "completed" } else { "watching" };
    let progress_str = progress.to_string();
    let _ = authed(client, client_id, client_secret, |token| {
        let progress_str = progress_str.clone();
        async move {
            let resp = client
                .patch(format!("https://api.myanimelist.net/v2/anime/{}/my_list_status", media.id))
                .bearer_auth(token)
                .form(&[("status", status), ("num_watched_episodes", progress_str.as_str())])
                .send()
                .await
                .ok()?;
            Some((resp.status().as_u16(), ()))
        }
    })
    .await;
}
