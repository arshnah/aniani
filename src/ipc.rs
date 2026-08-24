use std::time::{Duration, Instant};

#[cfg(unix)]
mod imp {
    use std::io::{BufRead, BufReader, Write};
    use std::os::unix::net::UnixStream;
    use std::time::{Duration, Instant};

    pub struct Conn {
        write: UnixStream,
        reader: BufReader<UnixStream>,
    }

    impl Conn {
        pub fn connect(path: &str) -> std::io::Result<Self> {
            let write = UnixStream::connect(path)?;
            write.set_read_timeout(Some(Duration::from_millis(200)))?;
            let read = write.try_clone()?;
            Ok(Conn { write, reader: BufReader::new(read) })
        }

        pub fn send_line(&mut self, s: &str) -> std::io::Result<()> {
            writeln!(self.write, "{s}")
        }

        pub fn read_line_until(&mut self, deadline: Instant) -> Option<String> {
            while Instant::now() < deadline {
                let mut line = String::new();
                match self.reader.read_line(&mut line) {
                    Ok(0) => return None,
                    Ok(_) => return Some(line),
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock || e.kind() == std::io::ErrorKind::TimedOut => continue,
                    Err(_) => return None,
                }
            }
            None
        }
    }
}

#[cfg(windows)]
mod imp {
    use std::fs::OpenOptions;
    use std::io::{Read, Write};
    use std::sync::mpsc::{channel, Receiver};
    use std::time::Instant;

    pub struct Conn {
        file: std::fs::File,
        rx: Receiver<String>,
        buf: String,
    }

    impl Conn {
        pub fn connect(path: &str) -> std::io::Result<Self> {
            let file = OpenOptions::new().read(true).write(true).open(path)?;
            let mut reader = file.try_clone()?;
            let (tx, rx) = channel::<String>();
            std::thread::spawn(move || {
                let mut pending = Vec::new();
                let mut chunk = [0u8; 4096];
                loop {
                    match reader.read(&mut chunk) {
                        Ok(0) | Err(_) => break,
                        Ok(n) => {
                            pending.extend_from_slice(&chunk[..n]);
                            while let Some(pos) = pending.iter().position(|&b| b == b'\n') {
                                let line: Vec<u8> = pending.drain(..=pos).collect();
                                if let Ok(s) = String::from_utf8(line) {
                                    if tx.send(s).is_err() {
                                        return;
                                    }
                                }
                            }
                        }
                    }
                }
            });
            Ok(Conn { file, rx, buf: String::new() })
        }

        pub fn send_line(&mut self, s: &str) -> std::io::Result<()> {
            self.buf.clear();
            self.buf.push_str(s);
            self.buf.push('\n');
            self.file.write_all(self.buf.as_bytes())
        }

        pub fn read_line_until(&mut self, deadline: Instant) -> Option<String> {
            let remaining = deadline.saturating_duration_since(Instant::now());
            self.rx.recv_timeout(remaining).ok()
        }
    }
}

pub struct Conn(imp::Conn);

impl Conn {
    pub fn connect(path: &str) -> Option<Self> {
        imp::Conn::connect(path).ok().map(Conn)
    }

    pub fn send_line(&mut self, s: &str) {
        let _ = self.0.send_line(s);
    }

    pub fn read_line_until(&mut self, deadline: Instant) -> Option<String> {
        self.0.read_line_until(deadline)
    }

    pub fn read_response(&mut self, request_id: i64, timeout: Duration) -> Option<serde_json::Value> {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            let line = self.read_line_until(deadline)?;
            let Ok(msg) = serde_json::from_str::<serde_json::Value>(&line) else { continue };
            if msg.get("request_id").and_then(|v| v.as_i64()) == Some(request_id) {
                if msg.get("error").and_then(|v| v.as_str()) == Some("success") {
                    return msg.get("data").cloned();
                }
                return None;
            }
        }
        None
    }
}
