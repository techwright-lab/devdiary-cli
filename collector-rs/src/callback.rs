//! The listener on 127.0.0.1 that the approval page redirects the browser to,
//! carrying the verifier. Anything on this machine can reach it, so it takes a
//! verifier only from a request that names this terminal's own approval code.
use std::{
    io::{Read, Write},
    net::{Ipv4Addr, SocketAddrV4, TcpListener, TcpStream},
    time::Duration,
};

pub(crate) struct Listener {
    socket: TcpListener,
    port: u16,
}

impl Listener {
    /// None when this machine will not let the CLI listen: the request then
    /// declares no port, and the person types the verifier instead.
    pub(crate) fn bind() -> Option<Self> {
        let port = if cfg!(debug_assertions) {
            std::env::var("DEVDIARY_DEBUG_CALLBACK_PORT")
                .ok()
                .and_then(|p| p.parse().ok())
                .unwrap_or(0)
        } else {
            0
        };
        let socket = TcpListener::bind(SocketAddrV4::new(Ipv4Addr::LOCALHOST, port)).ok()?;
        socket.set_nonblocking(true).ok()?;
        let port = socket.local_addr().ok()?.port();
        (port >= 1024).then_some(Self { socket, port })
    }
    pub(crate) fn port(&self) -> u16 {
        self.port
    }
    pub(crate) fn socket(&self) -> &TcpListener {
        &self.socket
    }
    /// The verifier from a callback that names `code`. Any other request is
    /// answered here and gives nothing.
    pub(crate) fn receive(&self, code: &str) -> Option<(TcpStream, String)> {
        let (mut stream, _) = self.socket.accept().ok()?;
        stream.set_nonblocking(false).ok()?;
        stream.set_read_timeout(Some(Duration::from_secs(2))).ok()?;
        stream
            .set_write_timeout(Some(Duration::from_secs(2)))
            .ok()?;
        let mut head = Vec::new();
        let mut chunk = [0; 1024];
        while !head.windows(4).any(|w| w == b"\r\n\r\n") && head.len() < 8192 {
            match stream.read(&mut chunk) {
                Ok(0) | Err(_) => break,
                Ok(n) => head.extend_from_slice(&chunk[..n]),
            }
        }
        let verifier = std::str::from_utf8(&head)
            .ok()
            .and_then(|head| verifier(head, self.port, code));
        match verifier {
            Some(verifier) => Some((stream, verifier)),
            None => {
                answer(stream, 404, "Not found.");
                None
            }
        }
    }
}

pub(crate) fn answer(mut stream: TcpStream, status: u16, page: &str) {
    let reason = if status == 200 { "OK" } else { "Not Found" };
    let _ = write!(
        stream,
        "HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {}\r\nCache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nX-Content-Type-Options: nosniff\r\nConnection: close\r\n\r\n{page}\n",
        page.len() + 1
    );
    let _ = stream.flush();
}

// The server redirects to exactly /callback?code=…&verifier=… on the literal
// host 127.0.0.1. A Host header naming another host is a page that rebound a
// DNS name to this address.
fn verifier(head: &str, port: u16, code: &str) -> Option<String> {
    let mut lines = head.split("\r\n");
    let target = lines
        .next()?
        .strip_prefix("GET ")?
        .strip_suffix(" HTTP/1.1")?;
    let host = lines
        .filter_map(|line| line.split_once(':'))
        .filter(|(name, _)| name.eq_ignore_ascii_case("host"))
        .map(|(_, value)| value.trim())
        .collect::<Vec<_>>();
    if host != [format!("127.0.0.1:{port}")] {
        return None;
    }
    let (named, verifier) = match target
        .strip_prefix("/callback?")?
        .split('&')
        .collect::<Vec<_>>()[..]
    {
        [named, verifier] => (
            named.strip_prefix("code=")?,
            verifier.strip_prefix("verifier=")?,
        ),
        _ => return None,
    };
    if crate::pairing::normalised(named)? != code {
        return None;
    }
    crate::pairing::normalised(verifier)
}

#[cfg(test)]
mod tests {
    use super::*;

    const OWN: &str = "KBRTWMPH";
    fn head(target: &str, host: &str) -> String {
        format!("GET {target} HTTP/1.1\r\nHost: {host}\r\nAccept: */*\r\n\r\n")
    }

    #[test]
    fn the_callback_that_names_this_request_gives_its_verifier() {
        let ok = head(
            "/callback?code=KBRTWMPH&verifier=BCDFGHJK",
            "127.0.0.1:4100",
        );
        assert_eq!(verifier(&ok, 4100, OWN).as_deref(), Some("BCDFGHJK"));
    }

    #[test]
    fn any_other_request_gives_nothing() {
        for (target, host) in [
            (
                "/callback?code=BCDFGHJK&verifier=BCDFGHJK",
                "127.0.0.1:4100",
            ),
            (
                "/callback?code=KBRTWMPH&verifier=BCDFGHJK",
                "localhost:4100",
            ),
            ("/callback?code=KBRTWMPH&verifier=BCDFGHJK", "evil.test"),
            (
                "/callback?code=KBRTWMPH&verifier=AEIOUAEI",
                "127.0.0.1:4100",
            ),
            (
                "/callback?verifier=BCDFGHJK&code=KBRTWMPH",
                "127.0.0.1:4100",
            ),
            ("/callback?code=KBRTWMPH", "127.0.0.1:4100"),
            (
                "/callback?code=KBRTWMPH&verifier=BCDFGHJK&x=1",
                "127.0.0.1:4100",
            ),
            ("/favicon.ico", "127.0.0.1:4100"),
        ] {
            assert_eq!(
                verifier(&head(target, host), 4100, OWN),
                None,
                "{target} {host}"
            );
        }
        let post = "POST /callback?code=KBRTWMPH&verifier=BCDFGHJK HTTP/1.1\r\nHost: 127.0.0.1:4100\r\n\r\n";
        assert_eq!(verifier(post, 4100, OWN), None);
        let two_hosts = "GET /callback?code=KBRTWMPH&verifier=BCDFGHJK HTTP/1.1\r\nHost: 127.0.0.1:4100\r\nHost: evil.test\r\n\r\n";
        assert_eq!(verifier(two_hosts, 4100, OWN), None);
    }
}
