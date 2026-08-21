//! Fast sitemap parser for ultimate-sitemap-parser.
//!
//! Wraps `quick-xml` and exposes two functions to Python:
//! - `parse_pages(bytes) -> list[str]`: pulls all <loc> from a
//!   pages-style (urlset) sitemap.
//! - `parse_index(bytes) -> list[str]`: pulls all <loc> from a
//!   sitemapindex.
//!
//! Both functions transparently handle gzipped input.

use pyo3::prelude::*;
use quick_xml::events::Event;
use quick_xml::Reader;

/// Decompress gzip if needed. Returns the raw bytes (or the input
/// unchanged if not gzipped).
fn maybe_gunzip(data: &[u8]) -> Vec<u8> {
    if data.len() >= 2 && data[0] == 0x1f && data[1] == 0x8b {
        let mut decoder = flate2::read::GzDecoder::new(data);
        let mut out = Vec::with_capacity(data.len() * 3);
        use std::io::Read;
        if decoder.read_to_end(&mut out).is_err() {
            // Fall back to raw on bad gzip
            return data.to_vec();
        }
        out
    } else {
        data.to_vec()
    }
}

/// Parse a pages-style (urlset) sitemap and return every <loc> value
/// in document order.
#[pyfunction]
fn parse_pages(data: &[u8]) -> PyResult<Vec<String>> {
    let raw = maybe_gunzip(data);
    let mut reader = Reader::from_reader(raw.as_slice());
    reader.config_mut().trim_text(true);

    let mut buf = Vec::new();
    let mut out: Vec<String> = Vec::with_capacity(512);
    let mut in_loc = false;

    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Start(e)) => {
                let name = e.name();
                let n = name.as_ref();
                let local = strip_ns(n);
                if local == b"loc" {
                    in_loc = true;
                }
            }
            Ok(Event::Text(e)) => {
                if in_loc {
                    if let Ok(s) = e.unescape() {
                        out.push(s.into_owned());
                    }
                }
            }
            Ok(Event::CData(e)) => {
                if in_loc {
                    if let Ok(s) = std::str::from_utf8(e.as_ref()) {
                        out.push(s.to_string());
                    }
                }
            }
            Ok(Event::End(_)) => {
                in_loc = false;
            }
            Ok(Event::Eof) => break,
            Ok(_) => {}
            Err(_) => break,
        }
        buf.clear();
    }
    Ok(out)
}

/// Parse a sitemap-index (sitemapindex) and return every <loc> value
/// in document order.
#[pyfunction]
fn parse_index(data: &[u8]) -> PyResult<Vec<String>> {
    let raw = maybe_gunzip(data);
    let mut reader = Reader::from_reader(raw.as_slice());
    reader.config_mut().trim_text(true);

    let mut buf = Vec::new();
    let mut out: Vec<String> = Vec::with_capacity(64);
    let mut in_loc = false;

    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Start(e)) => {
                let name = e.name();
                let n = name.as_ref();
                let local = strip_ns(n);
                if local == b"loc" {
                    in_loc = true;
                }
            }
            Ok(Event::Text(e)) => {
                if in_loc {
                    if let Ok(s) = e.unescape() {
                        out.push(s.into_owned());
                    }
                }
            }
            Ok(Event::CData(e)) => {
                if in_loc {
                    if let Ok(s) = std::str::from_utf8(e.as_ref()) {
                        out.push(s.to_string());
                    }
                }
            }
            Ok(Event::End(_)) => {
                in_loc = false;
            }
            Ok(Event::Eof) => break,
            Ok(_) => {}
            Err(_) => break,
        }
        buf.clear();
    }
    Ok(out)
}

#[inline(always)]
fn strip_ns(name: &[u8]) -> &[u8] {
    if let Some(idx) = name.iter().position(|&b| b == b'}') {
        &name[idx + 1..]
    } else {
        name
    }
}

/// Python module declaration.
#[pymodule]
fn usp_fast(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(parse_pages, m)?)?;
    m.add_function(wrap_pyfunction!(parse_index, m)?)?;
    Ok(())
}
